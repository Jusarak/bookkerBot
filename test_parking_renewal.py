"""Offline HTTP requests, browser sessions and SMTP through the service entry points."""

import asyncio
import contextlib
import io
import json
import os
import selectors
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import parking_renewal
import parking_release
import test_parking_release

IDENTITY = "operator@example.test"
ORIGIN = "https://fixture.tailnet.ts.net:8765"
BOOKKER = "https://bookker.example.test/"
GOOD = {"cookies": [], "origins": [{"origin": BOOKKER, "localStorage": [{"name": "fixture", "value": "valid"}]}]}
BAD = {"cookies": [], "origins": []}
ENV = {
    "BOOKKER_TAILSCALE_LOGIN": IDENTITY,
    "BOOKKER_RENEWAL_URL": ORIGIN + "/",
    "BOOKKER_URL": BOOKKER,
    "BOOKKER_SMTP_HOST": "smtp.example.test",
    "BOOKKER_SMTP_FROM": "parking@example.test",
    "BOOKKER_SMTP_TO": "operator@gmail.com",
    "BOOKKER_SMTP_USER": "",
    "BOOKKER_SMTP_PASSWORD": "",
}


class OfflineConnection:
    def __init__(self, request):
        self.reader = io.BytesIO(request)
        self.response = bytearray()

    def settimeout(self, value):
        pass

    def makefile(self, *args, **kwargs):
        return self.reader

    def sendall(self, data):
        self.response.extend(data)


class ReplacementViewer:
    instances = []

    def __init__(self, base_url):
        assert base_url == BOOKKER
        self.closed = False
        self.instances.append(self)

    async def start(self):
        pass

    async def frame(self):
        return b"replacement frame"

    async def input(self, action):
        self.action = action

    async def candidate(self):
        return GOOD

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.sessions = []
        self.drivers = []
        self.login = Mock(url=BOOKKER, screenshot=AsyncMock(return_value=b"IdP frame"),
                          goto=AsyncMock(side_effect=self.redirect_login))
        self.login.touchscreen.tap = AsyncMock()
        self.login.keyboard.insert_text = AsyncMock(side_effect=self.complete_login)
        self.login_context = Mock(pages=[self.login], new_page=AsyncMock(return_value=self.login),
                                  storage_state=AsyncMock(side_effect=lambda **_: GOOD if self.signed_in else BAD),
                                  close=AsyncMock())
        self.signed_in = False
        self.verified_page = Mock(url=BOOKKER)
        self.verified_link = self.verified_page.get_by_role.return_value.or_.return_value.filter.return_value.first
        self.verified_link.click = AsyncMock()
        rendered_view = self.verified_page.locator.return_value.or_.return_value.filter.return_value
        self.verified_records = rendered_view.locator.return_value.or_.return_value.filter.return_value.first
        self.verified_records.wait_for = AsyncMock()
        self.verified_page.locator.return_value.or_.return_value.filter.return_value.or_.return_value.count = (
            AsyncMock(return_value=0)
        )
        self.verified_page.goto = AsyncMock()
        self.fresh = Mock(new_page=AsyncMock(return_value=self.verified_page), close=AsyncMock())
        self.login_browser = Mock(new_context=AsyncMock(return_value=self.login_context), close=AsyncMock())
        self.validation_browser = Mock(new_context=AsyncMock(side_effect=self.fresh_context), close=AsyncMock())

    async def redirect_login(self, *args, **kwargs):
        self.login.url = "https://idp.example.test/sign-in"

    async def complete_login(self, text):
        if text == "synthetic-mfa":
            self.signed_in = True
            self.login.url = BOOKKER

    async def fresh_context(self, *, storage_state, **kwargs):
        self.sessions.append(storage_state)
        self.verified_page.url = BOOKKER if storage_state == GOOD else "https://idp.example.test/sign-in"
        return self.fresh

    def driver(self):
        manager = AsyncMock()
        playwright = Mock()
        playwright.chromium.launch = AsyncMock(side_effect=lambda **kwargs: self.next_browser(kwargs))
        manager.start.return_value = playwright
        manager.__aenter__.return_value = playwright
        self.drivers.append(manager)
        return manager

    def next_browser(self, options):
        assert options["chromium_sandbox"]
        return self.login_browser if len(self.drivers) == 1 else self.validation_browser

    @property
    def modules(self):
        return {"playwright.async_api": SimpleNamespace(async_playwright=self.driver)}


class MobileEntryPointTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(work.cleanup)
        self.state = Path(work.name)
        self.session = self.state / "session.json"
        self.session.write_text(json.dumps(BAD))
        self.original = (self.session.stat().st_ino, self.session.read_bytes())
        ReplacementViewer.instances.clear()

    def request(self, method, path, data=None, *, identity=IDENTITY, origin=ORIGIN):
        request = f"{method} {path} HTTP/1.0\r\nHost: {ORIGIN.removeprefix('https://')}\r\n"
        if identity:
            request += f"Tailscale-User-Login: {identity}\r\n"
        if method == "POST":
            body = json.dumps(data or {}).encode()
            if origin:
                request += f"Origin: {origin}\r\n"
            request += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        else:
            body = b""
        connection = OfflineConnection(request.encode() + b"\r\n" + body)
        self.server.finish_request(connection, ("127.0.0.1", 43210))
        headers, raw = bytes(connection.response).split(b"\r\n\r\n", 1)
        self.assertIn(b"Cache-Control: no-store", headers)
        status = int(headers.split(b" ")[1])
        if raw and b"Content-Type: application/json" in headers:
            return status, json.loads(raw)
        return status, raw

    def wait_phase(self, phase):
        for _ in range(100):
            code, result = self.request("GET", "/status")
            self.assertEqual(code, 200)
            if result["phase"] == phase:
                return result
            time.sleep(0.01)
        self.fail(f"Expected {phase}, last phase: {result['phase']}")

    def run_mobile(self, scenario, *, viewer=None, fake=None, window=4):
        env = {**ENV, "BOOKKER_RENEWAL_VIEWER": viewer or "parking_viewer:BrowserViewer"}
        listener = Mock()
        listener.getsockname.return_value = ("127.0.0.1", 8765)
        listener.__enter__ = Mock(return_value=listener)
        listener.__exit__ = Mock()
        failures = []
        serve = parking_renewal.serve
        renewal = parking_renewal.Renewal
        asyncio_run = asyncio.run

        def handle(server):
            self.server = server
            try:
                scenario()
            except BaseException as exc:
                failures.append(exc)
                server.renewal.exit_required = True
            finally:
                server.last_request = float("-inf")

        def make_renewal(state, base, factory, validate):
            return renewal(state, base, factory, validate, window=window)

        async def serve_fixture(*args):
            await serve(*args, idle=0)

        class OfflineSelector(selectors.DefaultSelector):
            def select(self, timeout=None):
                # The sandbox blocks thread wakeups on asyncio's socketpair.
                return super().select(0.005 if timeout is None else min(timeout, 0.005))

        def run_fixture(coro):
            return asyncio_run(coro, loop_factory=lambda: asyncio.SelectorEventLoop(OfflineSelector()))

        journal = io.StringIO()
        with (
            patch.dict(os.environ, env),
            patch.dict(sys.modules, fake.modules if fake else {}),
            patch.object(parking_renewal, "activated_socket", return_value=listener),
            patch.object(parking_renewal, "Renewal", side_effect=make_renewal),
            patch.object(parking_renewal.MobileServer, "handle_request", handle),
            patch.object(parking_renewal, "serve", side_effect=serve_fixture),
            patch.object(asyncio, "run", side_effect=run_fixture),
            contextlib.redirect_stdout(journal),
        ):
            code = parking_renewal.main(["--state-dir", str(self.state)])
        if failures:
            raise failures[0]
        self.assertEqual(code, 0)
        listener.close.assert_called_once()
        if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
            print(journal.getvalue(), end="")
        return [json.loads(line) for line in journal.getvalue().splitlines()]

    def assert_original(self):
        self.assertEqual((self.session.stat().st_ino, self.session.read_bytes()), self.original)

    def test_read_only_preview_other_identity_and_replacement_viewer_cancellation(self):
        def scenario():
            self.assertEqual(self.request("HEAD", "/", identity=None)[0], 403)
            self.assertEqual(self.request("HEAD", "/"), (200, b""))
            self.assertIn(b"Start sign-in", self.request("GET", "/")[1])
            self.assertEqual(self.request("GET", "/status")[1]["phase"], "idle")
            self.assertEqual(self.request("POST", "/start", identity="other@example.test")[0], 403)
            self.assertEqual(self.request("POST", "/start", origin="https://elsewhere.test")[0], 403)
            self.assertEqual(self.request("GET", "/ui.js", identity=None)[0], 403)
            self.assertEqual(ReplacementViewer.instances, [])
            self.assertEqual(self.request("POST", "/start")[0], 200)
            attempt = self.wait_phase("sign_in")["attempt"]
            data = {"attempt": attempt}
            self.assertEqual(self.request("POST", "/frame", data, identity="other@example.test")[0], 403)
            self.assertEqual(self.request("POST", "/input", data, identity="other@example.test")[0], 403)
            self.assertEqual(self.request("POST", "/finish", data, identity="other@example.test")[0], 403)
            self.assertEqual(self.request("POST", "/frame", {"attempt": "old"})[0], 409)
            self.assertEqual(self.request("POST", "/frame", data), (200, b"replacement frame"))
            self.assertEqual(self.request("POST", "/input", {**data, "key": "Control+O"})[0], 400)
            self.assertEqual(self.request("POST", "/input", {**data, "tap": [10, 20]})[0], 200)
            self.assertEqual(ReplacementViewer.instances[0].action, {"tap": [10, 20]})
            self.assertEqual(self.request("POST", "/cancel", data)[1]["phase"], "cancelled")
            self.assertTrue(ReplacementViewer.instances[0].closed)
            self.assert_original()

        records = self.run_mobile(scenario, viewer="test_parking_renewal:ReplacementViewer")
        self.assertEqual(records[-1], {"event": "renewal_exit", "outcome": "cancelled", "cleanup": "closed"})

    def test_timeout_closes_browser_without_installing_state(self):
        fake = FakePlaywright()

        def scenario():
            self.request("POST", "/start")
            attempt = self.wait_phase("sign_in")["attempt"]
            self.assertEqual(self.request("POST", "/frame", {"attempt": attempt}), (200, b"IdP frame"))
            self.wait_phase("timed_out")
            self.assertEqual(self.request("POST", "/frame", {"attempt": attempt})[0], 409)
            self.assert_original()

        records = self.run_mobile(scenario, fake=fake, window=0.25)
        self.assertEqual(records[-1]["outcome"], "timed_out")
        fake.login_context.close.assert_awaited_once()
        fake.login_browser.close.assert_awaited_once()
        fake.drivers[0].__aexit__.assert_awaited_once()

    def test_failed_fresh_context_preserves_session(self):
        fake = FakePlaywright()

        def scenario():
            self.request("POST", "/start")
            attempt = self.wait_phase("sign_in")["attempt"]
            self.assertEqual(self.request("POST", "/finish", {"attempt": attempt})[1]["phase"], "verifying")
            self.wait_phase("sign_in_incomplete")
            self.assert_original()

        self.run_mobile(scenario, fake=fake)
        self.assertEqual(fake.sessions, [BAD])
        fake.fresh.close.assert_awaited_once()
        fake.login_context.close.assert_awaited_once()

    def test_cancel_during_validation_closes_both_contexts_without_installing(self):
        fake = FakePlaywright()
        fake.signed_in = True

        async def wait_for_records(**kwargs):
            await asyncio.sleep(30)

        fake.verified_records.wait_for.side_effect = wait_for_records

        def scenario():
            self.request("POST", "/start")
            attempt = self.wait_phase("sign_in")["attempt"]
            self.request("POST", "/finish", {"attempt": attempt})
            self.wait_phase("verifying")
            self.assertEqual(self.request("POST", "/cancel", {"attempt": attempt})[1]["phase"], "cancelled")
            self.assert_original()

        self.run_mobile(scenario, fake=fake)
        self.assertEqual(fake.sessions, [GOOD])
        fake.fresh.close.assert_awaited_once()
        fake.login_context.close.assert_awaited_once()

    def test_expiry_alert_then_phone_sign_in_and_next_scheduled_run(self):
        scheduled = test_parking_release.ScheduledEntryPointTests()
        scheduled.setUp()
        self.addCleanup(scheduled.doCleanups)
        self.state, self.session = scheduled.state, scheduled.session
        self.session.chmod(0o644)
        original_inode = self.session.stat().st_ino
        scheduled.expired = True
        smtp_factory = MagicMock()
        smtp = smtp_factory.return_value.__enter__.return_value
        with patch.dict(os.environ, ENV), patch("parking_notify.smtplib.SMTP_SSL", smtp_factory):
            for status in ("sent", "suppressed"):
                _, trace = scheduled.run_entry(notifications=True)
                self.assertEqual(trace[-2]["event"], "expiry_alert")
                self.assertEqual(trace[-2]["outcome"], status)
                self.assertEqual(trace[-1]["outcome"], "auth_required")
                if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
                    print(json.dumps(trace[-2]))
            smtp.send_message.assert_called_once()
            mail = smtp.send_message.call_args.args[0]
            self.assertEqual(mail["To"], "operator@gmail.com")
            self.assertIn("2026-11-30", mail["Subject"])
            self.assertIn("2026-11-23 00:00:30 CET", mail.get_content())
            self.assertIn(ORIGIN + "/", mail.get_content())
        self.assertFalse(scheduled.intent.exists())

        fake = FakePlaywright()

        def scenario():
            self.request("POST", "/start")
            attempt = self.wait_phase("sign_in")["attempt"]
            self.assertEqual(self.request("POST", "/frame", {"attempt": attempt}), (200, b"IdP frame"))
            self.assertEqual(self.request("POST", "/input", {"attempt": attempt, "tap": [12, 34]})[0], 200)
            self.assertEqual(self.request("POST", "/input", {"attempt": attempt, "text": "synthetic-mfa"})[0], 200)
            self.request("POST", "/finish", {"attempt": attempt})
            self.wait_phase("verified")

        self.run_mobile(scenario, fake=fake)
        fake.login.touchscreen.tap.assert_awaited_once_with(12, 34)
        fake.login_context.storage_state.assert_awaited_once_with(indexed_db=True)
        fake.fresh.close.assert_awaited_once()
        fake.verified_link.click.assert_awaited_once()
        fake.verified_records.wait_for.assert_awaited_once()
        fake.validation_browser.close.assert_awaited_once()
        fake.login_browser.close.assert_awaited_once()
        self.assertEqual(fake.sessions, [GOOD])
        self.assertEqual(stat.S_IMODE(self.session.stat().st_mode), 0o600)
        self.assertNotEqual(self.session.stat().st_ino, original_inode)

        def read_installed_state():
            scheduled.expired = json.loads(self.session.read_bytes()) != GOOD

        code, trace = scheduled.run_entry(during_launch=read_installed_state)
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertEqual(scheduled.form.clicks, 1)
        if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
            print(json.dumps(trace[-1]))

    def test_notification_command_uses_only_the_mail_transport(self):
        smtp_factory = MagicMock()
        smtp = smtp_factory.return_value.__enter__.return_value
        stdout = io.StringIO()
        with (patch.dict(os.environ, ENV), patch("parking_notify.smtplib.SMTP_SSL", smtp_factory),
              patch("parking_release.BookkerClient") as browser, contextlib.redirect_stdout(stdout)):
            self.assertEqual(parking_release.main(["notification-test"]), 0)
        browser.assert_not_called()
        self.assertIn(ORIGIN + "/", smtp.send_message.call_args.args[0].get_content())
        self.assertEqual(json.loads(stdout.getvalue())["outcome"], "sent")
