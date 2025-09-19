#!/usr/bin/env python3
# capture_flow.py — robust capture for HTTP + WebSocket (Edge-first)
import os, json, time, re
from pathlib import Path
from contextlib import contextmanager
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("BOOKKER_URL", "https://webapp.bookkercorp.com/")
AUTH_STATE = os.environ.get("BOOKKER_AUTH", "auth_state.json")
CHANNEL = os.environ.get("PLAYWRIGHT_CHANNEL", "msedge")  # Edge by default
OUT_DIR = Path("captures_flow")

# Optional: narrow by backend host once we see where calls go
HOST_HINT = os.environ.get("BOOKKER_API_HOST_HINT")  # e.g. execute-api.eu-west-1.amazonaws.com
host_re = re.compile(re.escape(HOST_HINT), re.I) if HOST_HINT else None

def now_ms() -> int: return int(time.time() * 1000)

def safe_write(path: Path, data: str | bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
    with open(path, mode, encoding=None if mode == "wb" else "utf-8") as f:
        f.write(data)

class StepCapture:
    def __init__(self, context, page):
        self.context = context
        self.page = page
        self.current_dir: Path | None = None
        self.http_manifest: list[dict] = []
        self.ws_manifest: list[dict] = []
        self._attached = False

    def _should_keep(self, url: str) -> bool:
        return True if not host_re else bool(host_re.search(url))

    def _on_response(self, resp):
        try:
            url = resp.url
            if not self._should_keep(url): return
            status = resp.status
            method = resp.request.method
            headers = resp.headers or {}
            ct = (headers.get("content-type") or "").lower()
            ts = now_ms()

            # Try to get body; if it's big, still try
            body_bytes = b""
            try:
                body_bytes = resp.body()
            except Exception:
                pass

            # Decide filename stem
            stem = f"{ts}_{status}_{method}"
            # Sanitize URL for metadata
            meta = {"t": ts, "url": url, "status": status, "method": method, "content_type": ct}

            # Try JSON first regardless of content-type
            saved_as = None
            if body_bytes:
                try:
                    parsed = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    fp = self.current_dir / f"{stem}.json"
                    safe_write(fp, json.dumps({"url": url, "json": parsed}, ensure_ascii=False, indent=2))
                    saved_as = fp.name
                except Exception:
                    # Not JSON -> save as text
                    fp = self.current_dir / f"{stem}.txt"
                    safe_write(fp, body_bytes)
                    saved_as = fp.name
            else:
                # no body -> record headers only
                fp = self.current_dir / f"{stem}._no_body.txt"
                safe_write(fp, f"{url}\n{headers}\n")
                saved_as = fp.name

            self.http_manifest.append({**meta, "file": saved_as})
            print(f"[{self.current_dir.name}] HTTP {method} {status} {url} -> {saved_as}")
        except Exception:
            pass

    def _on_websocket(self, ws):
        if not self._should_keep(ws.url): return
        # one file per socket
        if not self.current_dir: return
        base = self.current_dir / f"ws_{now_ms()}"

        meta = {"t": now_ms(), "url": ws.url, "file_prefix": base.name}
        self.ws_manifest.append(meta)
        print(f"[{self.current_dir.name}] WS OPEN {ws.url} -> {base.name}*.log")

        def on_frame_received(data):
            ts = now_ms()
            try:
                # Try JSON frame
                try:
                    parsed = json.loads(data)
                    fp = Path(str(base) + "_frames.jsonl")
                    safe_write(fp, "") if not fp.exists() else None
                    with open(fp, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"t": ts, "json": parsed}, ensure_ascii=False) + "\n")
                    print(f"[{self.current_dir.name}] WS frame JSON -> {fp.name}")
                except Exception:
                    # Save raw
                    fp = Path(str(base) + "_frames.txt")
                    safe_write(fp, "") if not fp.exists() else None
                    with open(fp, "a", encoding="utf-8") as f:
                        f.write(f"[{ts}] {data}\n")
                    print(f"[{self.current_dir.name}] WS frame text -> {fp.name}")
            except Exception:
                pass

        def on_frame_sent(_):
            # usually not needed; uncomment if useful
            pass

        def on_close(_):
            print(f"[{self.current_dir.name}] WS CLOSE {ws.url}")

        ws.on("framereceived", on_frame_received)
        ws.on("framesent", on_frame_sent)
        ws.on("close", on_close)

    def attach_once(self):
        if self._attached: return
        self.page.on("response", self._on_response)
        self.context.on("websocket", self._on_websocket)
        self._attached = True

    @contextmanager
    def step(self, step_dir: Path):
        # swap active dir and fresh manifests for this step
        prev_dir = self.current_dir
        prev_http = self.http_manifest
        prev_ws = self.ws_manifest
        self.current_dir = step_dir
        self.http_manifest = []
        self.ws_manifest = []
        try:
            yield
        finally:
            # write manifests
            try:
                self.current_dir.mkdir(parents=True, exist_ok=True)
                safe_write(self.current_dir / "_manifest_http.json", json.dumps(self.http_manifest, indent=2))
                safe_write(self.current_dir / "_manifest_ws.json", json.dumps(self.ws_manifest, indent=2))
            except Exception:
                pass
            self.current_dir = prev_dir
            self.http_manifest = prev_http
            self.ws_manifest = prev_ws

def main():
    if not Path(AUTH_STATE).exists():
        print(f"Missing auth state: {AUTH_STATE}. Run save-session first.")
        return

    OUT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=CHANNEL, headless=False)
        context = browser.new_context(storage_state=AUTH_STATE)
        page = context.new_page()
        page.goto(BASE_URL)
        page.wait_for_load_state("networkidle")

        cap = StepCapture(context, page)
        cap.attach_once()

        print("\n=== Step 1: Choose 'Parking' on the landing screen ===")
        with cap.step(OUT_DIR / "step_01_landing_parking"):
            input("Press ENTER here when Step 1 is complete... ")

        print("\n=== Step 2: Set Date/Time ===")
        with cap.step(OUT_DIR / "step_02_datetime"):
            input("Press ENTER here when Step 2 is complete... ")

        print("\n=== Step 3: Choose Area/Zone/Floor ===")
        with cap.step(OUT_DIR / "step_03_area"):
            input("Press ENTER here when Step 3 is complete... ")

        print("\n=== Step 4: Choose Vehicle/Car (if required) ===")
        with cap.step(OUT_DIR / "step_04_vehicle"):
            input("Press ENTER here when Step 4 is complete... ")

        print("\n=== Step 5: Click 'Search/Continue' to fetch availability ===")
        with cap.step(OUT_DIR / "step_05_search"):
            input("Press ENTER here when Step 5 is complete... ")

        print("\n=== Step 6: Results / Availability visible ===")
        with cap.step(OUT_DIR / "step_06_results"):
            input("Press ENTER to finish the capture... ")

        context.close()
        browser.close()
        print(f"\nDone. See: {OUT_DIR.resolve()}")
        print("Tip: if still empty, do the entire flow again without HOST_HINT set (capture everything).")

if __name__ == "__main__":
    main()
