"""Socket-activated, tailnet-only mobile renewal for a saved Bookker session."""

import argparse
import asyncio
import json
import os
import signal
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import import_module
from pathlib import Path
from socketserver import BaseServer
from time import monotonic
from uuid import uuid4

from parking_booking import write_json
from parking_notify import renewal_origin
from parking_viewer import HEIGHT, KEYS, WIDTH, SignInIncomplete, validate_session

FILES = {
    "/": ("text/html; charset=utf-8", "index.html"),
    "/ui.js": ("text/javascript; charset=utf-8", "ui.js"),
}
ACTIONS = {"/start", "/cancel", "/finish", "/frame", "/input"}
RUNNING = {"starting", "sign_in", "verifying", "closing"}
SIGN_IN_SECONDS = 300
IDLE_SECONDS = 15


class RequestError(Exception):
    def __init__(self, status):
        self.status = status


class Renewal:
    def __init__(self, state, base_url, make_viewer, validate, *, window=SIGN_IN_SECONDS):
        self.session = state / "session.json"
        self.base_url = base_url
        self.make_viewer = make_viewer
        self.validate = validate
        self.window = window
        self.phase = "idle"
        self.attempt = None
        self.task = None
        self.viewer = None
        self.viewer_lock = asyncio.Lock()
        self.exit_required = False

    def status(self):
        return {"phase": self.phase, "attempt": self.attempt}

    def start(self):
        if self.exit_required or self.phase in RUNNING:
            raise RequestError(409)
        self.attempt = uuid4().hex
        self.phase = "starting"
        self.finish = asyncio.Event()
        self.task = asyncio.create_task(self.run())
        return self.status()

    async def run(self):
        result = "failed"
        try:
            async with asyncio.timeout(self.window) as limit:
                self.viewer = self.make_viewer(self.base_url)
                await self.viewer.start()
                self.phase = "sign_in"
                await self.finish.wait()
                async with self.viewer_lock:
                    candidate = await self.viewer.candidate()
                if not isinstance(candidate, dict) or not all(
                    isinstance(candidate.get(key), list) for key in ("cookies", "origins")
                ):
                    raise SignInIncomplete
                await self.validate(self.base_url, candidate)
                if asyncio.get_running_loop().time() >= limit.when():
                    raise TimeoutError
                write_json(self.session, candidate)
                result = "verified"
        except SignInIncomplete:
            result = "sign_in_incomplete"
        except TimeoutError:
            result = "timed_out"
        except asyncio.CancelledError:
            result = "cancelled"
        except Exception:
            result = "failed"
        finally:
            self.phase = "closing"
            try:
                async with asyncio.timeout(5):
                    async with self.viewer_lock:
                        if self.viewer is not None:
                            await self.viewer.close()
            except Exception:
                # Exiting lets systemd reap anything the viewer could not close.
                self.exit_required = True
            self.viewer = None
            self.phase = result
            print(json.dumps({"event": "renewal_exit", "outcome": result,
                              "cleanup": "systemd" if self.exit_required else "closed"}), flush=True)

    async def stop(self):
        if self.task and self.phase in RUNNING:
            if self.phase != "closing":
                self.phase = "closing"
                self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                # A task cancelled before it starts has no viewer to close.
                self.phase = "cancelled"

    async def action(self, path, data):
        if path == "/start":
            return self.start()
        if self.attempt is None or data.get("attempt") != self.attempt:
            raise RequestError(409)
        if path == "/cancel" and self.phase in RUNNING:
            await self.stop()
            return self.status()
        if self.phase != "sign_in":
            raise RequestError(409)
        if path == "/finish":
            self.phase = "verifying"
            self.finish.set()
            return self.status()
        async with self.viewer_lock:
            if self.phase != "sign_in":
                raise RequestError(409)
            async with asyncio.timeout(2.5):
                if path == "/frame":
                    frame = await self.viewer.frame()
                    if self.phase == "sign_in":
                        return frame
                    raise RequestError(409)
                await self.viewer.input(viewer_input(data))
        return self.status()


def viewer_input(data):
    action = {key: value for key, value in data.items() if key != "attempt"}
    if len(action) == 1:
        key, value = next(iter(action.items()))
        if key == "tap" and isinstance(value, list) and len(value) == 2 and all(
            type(point) is int and 0 <= point < bound for point, bound in zip(value, (WIDTH, HEIGHT))
        ):
            return action
        if key == "scroll" and type(value) is int and -2000 <= value <= 2000:
            return action
        if key == "key" and isinstance(value, str) and value in KEYS:
            return action
        if key == "text" and isinstance(value, str) and 0 < len(value) <= 1024 and value.isprintable():
            return action
    raise RequestError(400)


class MobileRequest(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(3)
        super().setup()

    def log_message(self, *args):
        # Request headers, paths and viewer content can contain credentials.
        pass

    def send(self, status, data, content_type=None):
        if content_type is None:
            content_type = "application/json; charset=utf-8"
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; img-src blob:; "
                         "form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def allowed(self):
        if (self.headers.get_all("Tailscale-User-Login") != [self.server.identity]
                or self.headers.get_all("Host") != [self.server.origin.removeprefix("https://")]):
            self.send(403, {"error": "Tailnet identity required"})
            return False
        self.server.last_request = monotonic()
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.path in FILES:
            content_type, name = FILES[self.path]
            self.send(200, (Path(__file__).with_name("renewal") / name).read_bytes(), content_type)
        elif self.path == "/status":
            self.send(200, self.server.renewal.status())
        else:
            self.send(404, {"error": "Unknown page"})

    do_HEAD = do_GET

    def do_POST(self):
        if not self.allowed():
            return
        if (self.headers.get_all("Origin") != [self.server.origin]
                or self.headers.get_all("Content-Type") != ["application/json"]):
            self.send(403, {"error": "Open the renewal page to use this action"})
            return
        if self.path not in ACTIONS:
            self.send(404, {"error": "Unknown action"})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get("Transfer-Encoding"):
                raise ValueError
            length = int(lengths[0])
            if not 0 < length <= 8192:
                raise ValueError
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, ValueError):
            self.send(400, {"error": "Invalid request"})
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self.server.renewal.action(self.path, data), self.server.loop)
            response = future.result(timeout=7)
        except RequestError as exc:
            self.send(exc.status, {"error": "The requested viewer action is unavailable"})
        except Exception:
            self.send(503, {"error": "The viewer did not respond. Retry or cancel."})
        else:
            if isinstance(response, bytes):
                self.send(200, response, "image/jpeg")
            else:
                self.send(200, response)


class MobileServer(HTTPServer):
    timeout = 0.5

    def __init__(self, listener, identity, origin, renewal):
        BaseServer.__init__(self, listener.getsockname(), MobileRequest)
        self.socket = listener
        self.identity, self.origin, self.renewal = identity, origin, renewal
        self.last_request = monotonic()

    def handle_error(self, request, client_address):
        # Never include browser, request or credential details in the journal.
        pass


async def serve(listener, identity, origin, renewal, *, idle=IDLE_SECONDS):
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stopping.set)
    try:
        with MobileServer(listener, identity, origin, renewal) as server:
            server.loop = loop
            while not stopping.is_set() and not renewal.exit_required:
                await asyncio.to_thread(server.handle_request)
                if renewal.phase not in RUNNING and monotonic() - server.last_request >= idle:
                    break
            await renewal.stop()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def activated_socket():
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise ValueError("Run from the systemd renewal socket")
    listener = socket.socket(fileno=3)
    os.set_inheritable(listener.fileno(), False)
    if listener.family != socket.AF_INET or listener.getsockname()[0] != "127.0.0.1":
        listener.close()
        raise ValueError("Expected the private loopback listener")
    return listener


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/bookker-parking"))
    args = parser.parse_args(argv)
    try:
        identity = os.environ["BOOKKER_TAILSCALE_LOGIN"].strip()
        if not identity:
            raise ValueError("Missing Tailscale login")
        origin = renewal_origin()
        module, sep, factory = os.environ.get("BOOKKER_RENEWAL_VIEWER", "parking_viewer:BrowserViewer").partition(":")
        if not module or not sep or not factory:
            raise ValueError("Invalid renewal viewer")

        def make_viewer(base_url):
            return getattr(import_module(module), factory)(base_url)

        base_url = os.environ.get("BOOKKER_URL", "https://webapp.bookkercorp.com/")
        renewal = Renewal(args.state_dir, base_url, make_viewer, validate_session)
        with activated_socket() as listener:
            asyncio.run(serve(listener, identity, origin, renewal))
    except Exception:
        print(json.dumps({"event": "renewal_server_error"}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
