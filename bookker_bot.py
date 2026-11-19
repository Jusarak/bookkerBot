# bookker_bot.py  — Edge-first Playwright helper for Bookker
from __future__ import annotations
import os, re, json, time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from parking_booking import book_parking, superficie_request
from parking_cancellation import cancel_parking

if TYPE_CHECKING:
    from playwright.sync_api import Browser, BrowserContext, Page

@dataclass
class Spot:
    id: str
    name: Optional[str]
    available: Optional[bool]
    raw: Dict[str, Any]

def _coerce_bool(v: Any) -> Optional[bool]:
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return bool(v)
    if isinstance(v, str): return v.strip().upper() in {"AVAILABLE","FREE","OPEN","TRUE","YES"}
    return None

def _coerce_spot(item: Dict[str, Any]) -> Spot:
    sid = item.get("id") or item.get("uid") or item.get("spaceId") or item.get("resourceId") or ""
    name = item.get("name") or item.get("code") or item.get("label")
    avail = None
    for k in ("available","isAvailable","isFree","free","status","state"):
        if k in item:
            avail = _coerce_bool(item[k]); break
    return Spot(id=str(sid), name=name, available=avail, raw=item)

class BookkerClient:
    """
    Edge-first client:
      - save_session(): headful login (incl. 2FA) → saves auth_state.json
      - open_with_session(): reuse saved session (no 2FA while valid)
      - read_availability(): nudge UI, capture API JSON → list[Spot]
      - book(): UI-based booking helper (dry-run unless confirm_click=True)
    """

    def __init__(
        self,
        base_url: str = os.environ.get("BOOKKER_URL", "https://webapp.bookkercorp.com/"),
        auth_state_path: Path | str = "auth_state.json",
        api_host_hint: Optional[str] = os.environ.get("BOOKKER_API_HOST_HINT", "execute-api.eu-west-1.amazonaws.com"),
        headless: bool = True,
        slow_mo_ms: int = 0,
        channel: Optional[str] = os.environ.get("PLAYWRIGHT_CHANNEL", "msedge"),
    ):
        self.base_url = base_url
        self.auth_state_path = Path(auth_state_path)
        self.api_host_hint = api_host_hint
        self.headless = headless
        self.slow_mo_ms = slow_mo_ms
        self.channel = channel  # e.g., "msedge" | "chrome"

        self._pw = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        # Heuristics to match useful API calls
        self.url_matchers: List[re.Pattern] = [
            re.compile(r"/availability", re.I),
            re.compile(r"/spaces?", re.I),
            re.compile(r"/resources?", re.I),
            re.compile(r"/parking", re.I),
            re.compile(r"/reservations?", re.I),
        ]
        self.host_hint = re.compile(re.escape(self.api_host_hint), re.I) if self.api_host_hint else None

        self.json_paths = [["items"], ["data","items"], ["data","spaces"], ["spaces"], ["results"], []]

    # ---------- lifecycle ----------

    def _launch(self, storage_state: Optional[str] = None, force_headful: bool = False,
                timezone_id: Optional[str] = None):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # Use Edge (Chromium) via channel to avoid downloading bundled Chromium
        self.browser = self._pw.chromium.launch(
            channel=self.channel,
            headless=(self.headless and not force_headful),
            slow_mo=self.slow_mo_ms,
        )
        self.context = self.browser.new_context(storage_state=storage_state, timezone_id=timezone_id)
        self.page = self.context.new_page()

    def close(self):
        # Make closing safe to call multiple times and in any order.
        try:
            if self.context:
                try:
                    self.context.close()
                except Exception:
                    pass
        finally:
            self.context = None

        try:
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
        finally:
            self.browser = None

        try:
            if self._pw:
                try:
                    self._pw.stop()
                except Exception:
                    pass
        finally:
            self._pw = None
            self.page = None


    # ---------- session management ----------

    def save_session(self):
        """
        Opens Edge headful, navigates to base_url.
        You log in (incl. 2FA) manually, then press ENTER here to save auth_state.json
        """
        self._launch(force_headful=True)
        assert self.page and self.context
        print(f"[bookker] Opening {self.base_url} in Edge. Log in (2FA), then return to terminal.")
        self.page.goto(self.base_url)
        try:
            input("Press ENTER after login + 2FA completes... ")
        except KeyboardInterrupt:
            print("\n[bookker] Aborted."); self.close(); return
        self.page.wait_for_load_state("networkidle")
        self.context.storage_state(path=str(self.auth_state_path))
        print(f"[bookker] Saved auth to {self.auth_state_path.resolve()}")
        self.close()

    def open_with_session(self, *, timezone_id: Optional[str] = None, wait_for_idle: bool = True):
        """
        Opens Edge reusing stored session (no 2FA while valid).
        """
        if not self.auth_state_path.exists():
            raise FileNotFoundError(f"Auth state not found: {self.auth_state_path}. Run save_session() first.")
        self._launch(storage_state=str(self.auth_state_path), timezone_id=timezone_id)
        assert self.page
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        if wait_for_idle:
            self.page.wait_for_load_state("networkidle", timeout=15_000)

    # ---------- availability capture ----------

    def _dig(self, obj: Any, path: List[str]) -> Any:
        cur = obj
        for k in path:
            if isinstance(cur, dict) and k in cur: cur = cur[k]
            else: return None
        return cur

    def _maybe_list(self, j: Any) -> Optional[List[Dict[str, Any]]]:
        if isinstance(j, list) and (not j or isinstance(j[0], dict)): return j  # type: ignore
        for p in self.json_paths:
            node = j if not p else self._dig(j, p)
            if isinstance(node, list) and (not node or isinstance(node[0], dict)): return node  # type: ignore
        return None

    def _attach_capture(self, captures_dir: Path):
        captures_dir.mkdir(parents=True, exist_ok=True)
        latest_spots: List[Spot] = []

        def handler(resp):
            url = resp.url
            if self.host_hint and not self.host_hint.search(url): return
            if not any(p.search(url) for p in self.url_matchers): return
            try:
                ct = (resp.headers or {}).get("content-type","")
                if "application/json" not in ct.lower(): return
                data = resp.json()
            except Exception:
                return

            ts = int(time.time()*1000)
            fp = captures_dir / f"{ts}.json"
            try:
                fp.write_text(json.dumps({"url": url, "json": data}, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[capture] {url} -> {fp}")
            except Exception:
                pass

            arr = self._maybe_list(data)
            if arr:
                latest = [_coerce_spot(x) for x in arr if isinstance(x, dict)]
                latest = [s for s in latest if s.id or s.name]
                if latest:
                    nonlocal latest_spots
                    latest_spots = latest

        assert self.page
        self.page.on("response", handler)

        def get_latest() -> List[Spot]:
            return latest_spots
        return get_latest

    def read_availability(
        self,
        floor_label: Optional[str] = os.environ.get("BOOKKER_FLOOR_LABEL"),
        captures_dir: Path | str = "captures",
        wait_ms: int = 2500,
    ) -> List[Spot]:
        """
        Nudges UI to trigger API calls, captures JSON, returns best-effort list of spots.
        Dumps raw payloads to captures_dir/ for inspection/refinement.
        """
        assert self.page
        get_latest = self._attach_capture(Path(captures_dir))

        # Try obvious navigation controls (tenant-specific text may vary)
        for role in ("link","button"):
            try:
                el = self.page.get_by_role(role, name=re.compile(r"parking|aparcamiento|reserve|reserva|search|buscar", re.I)).first()
                if el and el.is_visible():
                    el.click(); self.page.wait_for_timeout(500)
            except Exception: pass

        # Floor selector (<mat-select>)
        if floor_label:
            try:
                combo = self.page.get_by_role("combobox", name=re.compile(r"floor|planta", re.I)).first()
                if combo and combo.is_visible():
                    combo.click()
                    self.page.get_by_role("option", name=re.compile(re.escape(floor_label), re.I)).click()
                    self.page.wait_for_timeout(500)
            except Exception: pass

        # Many UIs have a bottom-right FAB opening list view (fetches JSON)
        try:
            fab = self.page.get_by_role("button", name=re.compile(r"list|lista|view|menu", re.I)).first()
            if fab and fab.is_visible(): fab.click()
        except Exception: pass

        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(wait_ms)
        return get_latest()

    # ---------- booking (UI; dry-run unless confirm_click=True) ----------

    def book(
        self,
        spot_name: Optional[str] = None,
        spot_id: Optional[str] = None,
        confirm_click: bool = False,
        *,
        building: Optional[str] = None,
        floor: Optional[str] = None,
        from_date: Optional[date] = None,
        on_date: Optional[date] = None,
        whole_day: bool = False,
        submission_state: Optional[Path] = None,
        result_file: Optional[Path] = None,
    ) -> Dict[str, Any]:
        result = book_parking(
            self, building=building, floor=floor, from_date=from_date, on_date=on_date,
            whole_day=whole_day, spot_name=spot_name, spot_id=spot_id,
            confirm_click=confirm_click, submission_state=submission_state,
            result_file=result_file,
        )
        return {**superficie_request(on_date, building, floor), **result}

    def cancel(self, *, target, confirm, state, show_preview):
        return cancel_parking(self, target=target, confirm=confirm, state=state, show_preview=show_preview)
