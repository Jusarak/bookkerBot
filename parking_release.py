"""Scheduled parking oneshot and systemd timer generator."""

import argparse
import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from bookker_bot import BookkerClient
from parking_booking import BookingProblem, ReleaseClosed, book_parking
from parking_notify import RenewalMail, alert_expiry, session_generation
from parking_schedule import EXAMPLE, PRODUCTION, WARMUP, ZONE, render_timer


class WallClock:
    def now(self):
        return datetime.now(timezone.utc)

    def sleep(self, seconds):
        time.sleep(seconds)

    def monotonic(self):
        return time.monotonic()


def journal(record):
    print(json.dumps(record, ensure_ascii=False), flush=True)


class ReleaseWindow:
    def __init__(self, occurrence, clock, emit):
        self.occurrence = occurrence
        self.clock = clock
        self.emit = emit

    @property
    def local_today(self):
        return self.clock.now().astimezone(ZONE).date()

    def note(self, event, *, at=None, **fields):
        at = at if at is not None else self.clock.now()
        release = self.occurrence.release
        self.emit({"event": event, "at": at.isoformat(timespec="microseconds"),
                   "release_at": release.astimezone(ZONE).isoformat(),
                   "target_date": self.occurrence.target.isoformat(),
                   "offset_ms": round((at - release).total_seconds() * 1000, 3), **fields})

    def wait(self):
        at = self.clock.now()
        self.note("prepared", at=at)
        seconds = (self.occurrence.release - at).total_seconds()
        if seconds > WARMUP.total_seconds():
            raise ReleaseClosed
        if seconds > 0:
            self.clock.sleep(seconds)
        self.dispatch("release_open")

    def remaining(self, *, at=None):
        at = at if at is not None else self.clock.now()
        if at < self.occurrence.release:
            raise ReleaseClosed
        return self.preparation_remaining(at=at)

    def preparation_remaining(self, *, at=None):
        at = at if at is not None else self.clock.now()
        seconds = (self.occurrence.close - at).total_seconds()
        if seconds <= 0:
            raise ReleaseClosed
        return seconds

    def closed(self):
        return self.clock.now() >= self.occurrence.close

    def readback_budget(self):
        deadline = self.clock.monotonic() + 20

        def remaining():
            seconds = deadline - self.clock.monotonic()
            if seconds <= 0:
                raise BookingProblem("Reloaded Bookings could not be verified within the readback limit.")
            return seconds

        return remaining

    def dispatch(self, event):
        at = self.clock.now()
        milliseconds = min(5000, int(self.remaining(at=at) * 1000))
        if milliseconds < 1:
            raise ReleaseClosed
        self.note(event, at=at)
        return milliseconds


def run_release(schedule, clock, state, make_client, emit, *, dry_run):
    if not schedule.enabled:
        emit({"event": "exit", "outcome": "disabled", "reason": "Production release settings are incomplete."})
        return "disabled"
    at = clock.now()
    occurrence = schedule.active(at)
    if occurrence is None:
        emit({"event": "exit", "at": at.isoformat(), "outcome": "missed",
              "reason": "No selected Madrid release is in its warm-up or late-start window."})
        return "missed"

    window = ReleaseWindow(occurrence, clock, emit)
    window.note("wake", at=at, dry_run=dry_run)
    target = occurrence.target.isoformat()
    try:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = state / f"{target}.lock"
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)) as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                window.note("exit", outcome="competing", reason="A worker already owns this target date.")
                return "competing"
            session = state / "session.json"
            generation = session_generation(session)
            client = make_client(session)
            try:
                result = book_parking(
                    client, building=schedule.building, floor=schedule.floor, from_date=None,
                    on_date=occurrence.target, whole_day=True, spot_name=None, spot_id=None,
                    confirm_click=not dry_run, submission_state=state / f"{target}.intent.json",
                    result_file=None, release=window,
                )
                if result["outcome"] == "confirmed":
                    window.note("confirmation_observed", reconciled=bool(result.get("reconciled")))
            finally:
                client.close()
            status = {"blocked": "error", "unknown": "uncertain"}.get(result["outcome"], result["outcome"])
            if result.get("dry_run"):
                status = "preview"
            message = result.get("reason", "Reloaded Bookings verified the reservation.")
            if (status == "auth_required" or result.get("session_expired")) and not dry_run:
                try:
                    notice = alert_expiry(session, generation, occurrence, RenewalMail)
                except OSError:
                    notice = "state_error"
                window.note("expiry_alert", outcome=notice)
    except Exception:
        status, message = "error", "The worker could not access its local state or browser runtime."
    window.note("exit", outcome=status, reason=message)
    return status


def make_bookker_client(session):
    return BookkerClient(base_url=os.environ.get("BOOKKER_URL", "https://webapp.bookkercorp.com/"),
                         auth_state_path=session, channel=os.environ.get("PLAYWRIGHT_CHANNEL") or None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run once within the current production release window")
    run.add_argument("--state-dir", type=Path, default=Path("/var/lib/bookker-parking"))
    run.add_argument("--dry-run", action="store_true", help="Verify the final selection without submitting")
    timer = commands.add_parser("timer", help="Render a systemd calendar timer")
    timer.add_argument("--example", action="store_true", help="Render a sample timer for offline inspection")
    commands.add_parser("notification-test", help="Send a test email without opening Bookker")
    args = parser.parse_args(argv)

    if args.command == "timer":
        try:
            print(render_timer(EXAMPLE if args.example else PRODUCTION), end="")
        except ValueError as error:
            parser.error(str(error))
        return 0
    if args.command == "notification-test":
        try:
            mailer = RenewalMail()
            mailer.send(mailer.message())
        except Exception:
            journal({"event": "notification_test", "outcome": "delivery_unverified"})
            return 1
        journal({"event": "notification_test", "outcome": "sent"})
        return 0
    status = run_release(PRODUCTION, WallClock(), args.state_dir, make_bookker_client, journal, dry_run=args.dry_run)
    return int(status in {"error", "uncertain"})


if __name__ == "__main__":
    raise SystemExit(main())
