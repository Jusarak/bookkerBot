import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import parking_release
from parking_booking import BOOKING_LOADING, READY_BUTTON, SIGN_IN, BookingProblem, ParkingForm, read_intent
from parking_schedule import EXAMPLE, PRODUCTION, Schedule, ZONE
from test_parking_booking import MATCH, OfflineForm, locator


class FixtureClock:
    def __init__(self, now):
        self.at = now
        self.sleeps = []

    def now(self):
        return self.at

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)


class ReleaseForm(OfflineForm):
    def __init__(self, testcase, page):
        super().__init__()
        self.fixture = testcase
        self.page = page
        self.existing = []
        self.intent = testcase.intent

    def check_authenticated(self, base_url):
        self.events.append("auth")
        if self.fixture.expired:
            raise BookingProblem("Bookker requires sign-in.", SIGN_IN)
        self.fixture.clock.advance(1)

    def open(self, building, floor, vehicle, *, release_ready=False):
        assert release_ready == self.page.reload.called
        self.events.append("form")
        self.fixture.clock.advance(self.fixture.prep_seconds)
        return super().open(building, floor, vehicle)

    def next_date(self, earliest, *, exact, on_dispatch):
        self.events.append("date")
        assert self.fixture.clock.now() >= self.fixture.occurrence.release
        assert exact and earliest == self.fixture.occurrence.target
        if self.fixture.stale_form and not self.page.reload.called:
            return None
        on_dispatch("date_dispatch")
        self.fixture.clock.advance(0.008)
        return super().next_date(earliest, exact=exact)

    def whole_day(self, expected_shifts, *, verify_all=False, release_ready=False):
        assert verify_all and release_ready and not expected_shifts
        self.fixture.clock.advance(0.015)
        return self.span, False

    def select_space(self, building, floor, span, spot_name, spot_id, *, require_material, on_dispatch):
        self.events.append("search")
        assert self.fixture.clock.now() >= self.fixture.occurrence.release
        on_dispatch("refresh_dispatch")
        self.fixture.clock.advance(0.035)
        if self.fixture.unavailable:
            return None
        return super().select_space(building, floor, span, spot_name, spot_id, require_material=require_material)

    def review(self, day, span, all_day, building, floor, choice, vehicle, *, verify_all=False,
               release_ready=False):
        assert verify_all and release_ready
        self.events.append("review")
        self.fixture.clock.advance(self.fixture.review_seconds)
        self.fixture.review_at = self.fixture.clock.now()
        return super().review(day, span, all_day, building, floor, choice, vehicle)

    def wait_for_submission(self, before, *, watch_final, verify_prompt):
        self.fixture.clock.advance(0.020)
        return super().wait_for_submission(before, watch_final=watch_final)

    def click(self, *, timeout):
        self.clicks += 1
        self.events.append("book")
        assert timeout > 0 and self.fixture.clock.now() >= self.fixture.occurrence.release
        assert read_intent(self.intent) == self.selection
        if self.fixture.confirmed:
            self.after_submit = [self.fixture.confirmed_record]
        else:
            raise TimeoutError("private fixture browser error")


class ReleaseControls(ReleaseForm, ParkingForm):
    """Bookker locator doubles with readiness times and the production form methods."""

    next_date = ParkingForm.next_date
    check_date = ParkingForm.check_date
    whole_day = ParkingForm.whole_day
    select_space = ParkingForm.select_space

    def __init__(self, testcase, page):
        super().__init__(testcase, page)
        self.date_input = locator()
        self.date_input.first = self.date_input
        self.date_input.locator.return_value = self.date_input
        self.date_input.fill.side_effect = self.set_date
        self.day = None
        self.selected = set()
        self.submitted_search = False
        self.empty = locator(0)
        self.empty.all.return_value = []
        self.label = Mock(return_value=self.empty)
        self.search = locator()
        self.search.first = self.search
        self.search.press.side_effect = self.dispatch_search
        self.day_loader = locator()
        self.day_loader.first = self.day_loader
        self.day_loader.wait_for.side_effect = self.shifts_loaded
        self.shifts = locator()
        self.shifts.first = self.shifts
        self.shifts.all.side_effect = self.available_shifts
        self.shifts_loaded_at = None
        self.rows = locator()
        self.rows.first = self.rows
        self.rows.wait_for.side_effect = self.results_loaded
        self.rows.all.side_effect = self.available_rows
        self.list_button = locator(0)
        self.list_button.first = self.list_button
        self.list_button.or_.side_effect = lambda other: other if other is self.rows else self.list_button
        self.list_button.wait_for.side_effect = self.results_loaded
        self.dialog = locator(text=MATCH.replace("November 23", "November 30") + "Book")
        self.dialog.last = self.dialog
        self.submit = locator()
        self.submit.first = self.submit
        self.submit.wait_for.side_effect = self.submit_ready
        self.submit.click.side_effect = self.click
        self.dialog.get_by_role.return_value = self.submit
        row = locator(text="Space: PTQ-123\nAvailable\n07:00–23:00")
        self.choose = locator()
        row.get_by_role.return_value = self.choose
        self.choose.click.side_effect = lambda: self.events.append("resource")
        self.row = row
        self.empty.or_.side_effect = lambda other: other if other is self.rows else self.empty
        page.locator.side_effect = {
            "input[type=date]": locator(), "mat-calendar": locator(0), "mat-chip-option": self.shifts,
            BOOKING_LOADING: self.day_loader,
            ":not(mat-chip-option *)": locator(), READY_BUTTON: locator(),
            "button": locator(0), "tbody tr, mat-list-option": self.rows,
            "app-resources article.list-container mat-list-item": self.empty,
            "mat-dialog-container, ion-modal": self.dialog,
        }.__getitem__
        page.get_by_role.side_effect = self.role
        page.wait_for_load_state.side_effect = lambda *args, **kwargs: testcase.fail("Post-release network idle")

    def role(self, role, name=None):
        if role == "dialog":
            return self.dialog
        if role == "button" and name.search("Search parking spaces"):
            return self.search
        if role == "button" and name.search("Show list"):
            return self.list_button
        return self.empty

    def control(self, label):
        assert label == "Date"
        return self.date_input

    def value(self, control, label):
        assert control is self.date_input and label == "Date"
        return self.day.isoformat()

    def set_date(self, value):
        assert self.fixture.clock.now() == self.fixture.occurrence.release
        self.day = date.fromisoformat(value)
        assert self.day == self.fixture.occurrence.target
        self.events.append("date")
        self.fixture.clock.advance(0.008)

    def shifts_loaded(self, **kwargs):
        assert self.day is not None
        self.fixture.clock.advance(0.010)
        self.shifts_loaded_at = self.fixture.clock.now()

    def available_shifts(self):
        assert self.shifts_loaded_at is not None
        controls = []
        for interval in ("07:00–14:00", "14:00–23:00"):
            shift = locator(text=interval)
            shift.locator.return_value = self.empty
            shift.evaluate.side_effect = lambda script, interval=interval: interval in self.selected
            shift.click.side_effect = lambda interval=interval: self.selected.add(interval)
            controls.append(shift)
        return controls

    def dispatch_search(self, key, *, timeout):
        assert key == "Enter" and self.selected == {"07:00–14:00", "14:00–23:00"}
        self.events.append("search")
        self.submitted_search = True

    def results_loaded(self, **kwargs):
        assert self.submitted_search
        if "results" not in self.events:
            self.events.append("results")
            self.fixture.clock.advance(0.035)

    def available_rows(self):
        assert "results" in self.events
        return [self.row]

    def submit_ready(self, **kwargs):
        assert "resource" in self.events
        self.fixture.clock.advance(0.007)
        self.fixture.review_at = self.fixture.clock.now()

    def review(self, *args, **kwargs):
        self.selection, submit = ParkingForm.review(self, *args, **kwargs)
        self.events.append("review")
        return self.selection, submit


class ScheduleTests(unittest.TestCase):
    def test_midnight_wake_uses_next_day_and_filters_parking_weekdays(self):
        now = datetime(2026, 11, 22, 23, 59, 30, tzinfo=ZONE)
        occurrence = EXAMPLE.active(now)
        self.assertEqual(occurrence.target, date(2026, 11, 30))
        self.assertEqual(occurrence.release.astimezone(ZONE).isoformat(), "2026-11-23T00:00:30+01:00")
        self.assertIsNone(EXAMPLE.active(now + timedelta(days=1)))
        self.assertIsNone(EXAMPLE.active(occurrence.close))
        self.assertEqual(EXAMPLE.active(occurrence.release + timedelta(seconds=59)), occurrence)
        self.assertEqual(EXAMPLE.calendars(), ["Wed,Sun *-*-* 23:59:30 Europe/Madrid"])

    def test_wall_clock_and_calendar_week_across_dst(self):
        policy = Schedule(time(7), (6,), "NEWTON", "Parking Torres Quevedo")
        spring = policy.occurrence(date(2026, 3, 22))
        autumn = policy.occurrence(date(2026, 10, 18))
        self.assertEqual(spring.target, date(2026, 3, 29))
        self.assertEqual(autumn.target, date(2026, 10, 25))
        self.assertEqual(policy.occurrence(spring.target).release - spring.release, timedelta(days=7, hours=-1))
        self.assertEqual(policy.occurrence(autumn.target).release - autumn.release, timedelta(days=7, hours=1))

        repeated = Schedule(time(2, 15), (6,), "NEWTON", "Parking Torres Quevedo")
        self.assertIsNone(repeated.occurrence(spring.target))
        self.assertEqual(repeated.occurrence(autumn.target).release.hour, 0)

        crossing = Schedule(time(3), (6,), "NEWTON", "Parking Torres Quevedo")
        self.assertEqual(crossing.occurrence(spring.target).wake.isoformat(), "2026-03-29T00:59:00+00:00")
        self.assertEqual(crossing.occurrence(autumn.target).wake.isoformat(), "2026-10-25T01:59:00+00:00")
        self.assertIn("Sun *-03~01..07 00:59:00 UTC", crossing.calendars())
        self.assertIn("Sun *-10~01..07 01:59:00 UTC", crossing.calendars())

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is unavailable")
    def test_example_timer_next_wake(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(parking_release.main(["timer", "--example"]), 0)
        timer = stdout.getvalue()
        calendar = next(line.partition("=")[2] for line in timer.splitlines() if line.startswith("OnCalendar="))
        result = subprocess.run(["systemd-analyze", "calendar", "--base-time=2026-11-22 22:00:00 UTC", calendar],
                                check=True, capture_output=True, text=True, env={**os.environ, "TZ": "UTC"})
        self.assertIn("Sun 2026-11-22 22:59:30 UTC", result.stdout)
        self.assertIn("AccuracySec=1us\nRandomizedDelaySec=0\nPersistent=false", timer)


class ScheduledEntryPointTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(work.cleanup)
        self.state = Path(work.name) / "state"
        self.state.mkdir()
        self.session = self.state / "session.json"
        self.session.write_text('{"synthetic": true}')
        self.occurrence = EXAMPLE.occurrence(date(2026, 11, 23))
        self.intent = self.state / f"{self.occurrence.target}.intent.json"
        self.clock = FixtureClock(self.occurrence.wake)
        self.page = Mock()
        self.form = ReleaseForm(self, self.page)
        self.confirmed_record = MATCH.replace("November 23", "November 30")
        self.confirmed = True
        self.unavailable = False
        self.stale_form = False
        self.expired = False
        self.prep_seconds = 2
        self.review_seconds = 0

    def run_entry(self, *args, schedule=EXAMPLE, during_launch=None, notifications=False):
        stdout = io.StringIO()

        def launch(client, *, storage_state, timezone_id):
            self.assertEqual(Path(storage_state), self.session)
            self.assertEqual(timezone_id, "Europe/Madrid")
            client.page = self.page
            client.browser = browser
            client.context = context
            if during_launch:
                during_launch()

        browser, context = Mock(), Mock()
        mail = contextlib.nullcontext() if notifications else patch.object(parking_release, "RenewalMail",
                                                                           side_effect=KeyError)
        with (
            mail,
            patch.object(parking_release, "PRODUCTION", schedule),
            patch.object(parking_release, "WallClock", return_value=self.clock),
            patch("parking_booking.ParkingForm", return_value=self.form) as form,
            patch.object(parking_release.BookkerClient, "_launch", autospec=True, side_effect=launch) as start,
            contextlib.redirect_stdout(stdout),
        ):
            code = parking_release.main(["run", "--state-dir", str(self.state), *args])
        if start.called:
            start.assert_called_once()
            form.assert_called_once_with(self.page)
            browser.close.assert_called_once()
            context.close.assert_called_once()
        out = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertNotIn(self.form.vehicle, stdout.getvalue())
        self.assertNotIn("OFFLINE-123", stdout.getvalue())
        return code, out

    def test_one_context_prepare_release_submit_verify_and_exit(self):
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(self.form.events, ["auth", "read", "form", "date", "search", "review", "book",
                                            "observe", "reload"])
        self.assertEqual(self.clock.sleeps, [57])
        offsets = {line["event"]: line["offset_ms"] for line in trace}
        self.assertEqual(offsets, {"wake": -60000, "prepared": -57000, "release_open": 0,
                                   "date_dispatch": 0, "refresh_dispatch": 23, "submit_dispatch": 58,
                                   "confirmation_observed": 78, "exit": 78})
        self.assertEqual(self.fixture_time(trace, "submit_dispatch"), self.review_at)
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertEqual(self.form.clicks, 1)
        if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
            for row in trace:
                print(json.dumps(row))

    def fixture_time(self, trace, event):
        return datetime.fromisoformat(next(row["at"] for row in trace if row["event"] == event))

    def test_scheduled_form_submits_at_first_verified_control(self):
        self.form = ReleaseControls(self, self.page)
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertEqual(self.form.events, ["auth", "read", "form", "date", "search", "results", "resource",
                                            "review", "book", "observe", "reload"])
        self.assertEqual(self.clock.sleeps, [57])
        self.assertEqual(self.fixture_time(trace, "submit_dispatch"), self.review_at)
        self.assertEqual({row["event"]: row["offset_ms"] for row in trace},
                         {"wake": -60000, "prepared": -57000, "release_open": 0, "date_dispatch": 0,
                          "refresh_dispatch": 18, "submit_dispatch": 60, "confirmation_observed": 80, "exit": 80})
        self.page.wait_for_load_state.assert_not_called()
        if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
            for row in trace:
                print(json.dumps(row))

    def test_late_start_and_preparation_overrun(self):
        self.clock.at = self.occurrence.release + timedelta(seconds=55)
        self.prep_seconds = 5
        _, trace = self.run_entry()
        self.assertEqual(trace[-1]["outcome"], "missed")
        self.assertEqual(self.clock.sleeps, [])
        self.assertNotIn("search", self.form.events)
        self.assertFalse(self.intent.exists())

    def test_missed_and_disabled_exit_without_browser_or_state(self):
        shutil.rmtree(self.state)
        self.clock.at = self.occurrence.close
        for schedule, status in ((EXAMPLE, "missed"), (PRODUCTION, "disabled")):
            with self.subTest(status=status):
                code, trace = self.run_entry(schedule=schedule)
                self.assertEqual(code, 0)
                self.assertEqual(trace[-1]["outcome"], status)
                self.assertFalse(self.state.exists())

    def test_valid_late_wake_can_still_submit(self):
        self.clock.at = self.occurrence.release + timedelta(seconds=5)
        _, trace = self.run_entry()
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(next(row["offset_ms"] for row in trace if row["event"] == "date_dispatch"), 8000)

    def test_window_closing_after_fresh_search_blocks_intent_and_submit(self):
        self.clock.at = self.occurrence.release + timedelta(seconds=56)
        self.review_seconds = 1
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertIn("refresh_dispatch", [row["event"] for row in trace])
        self.assertNotIn("submit_dispatch", [row["event"] for row in trace])
        self.assertEqual(trace[-1]["outcome"], "missed")
        self.assertEqual(self.form.clicks, 0)
        self.assertFalse(self.intent.exists())

    def test_existing_parking_skips_preparation_and_submission(self):
        self.form.existing = [self.confirmed_record]
        _, trace = self.run_entry()
        self.assertEqual(trace[-1]["outcome"], "already_booked")
        self.assertEqual(self.form.events, ["auth", "read"])
        self.assertFalse(self.intent.exists())

    def test_stale_calendar_reopens_once_and_unavailable_space_exits(self):
        self.stale_form = self.unavailable = True
        _, trace = self.run_entry()
        self.page.reload.assert_called_once_with(wait_until="domcontentloaded", timeout=5000)
        self.assertEqual(self.form.events.count("search"), 1)
        self.assertEqual(trace[-1]["outcome"], "unavailable")
        self.assertFalse(self.intent.exists())

    def test_preview_and_auth_failure_create_no_intent(self):
        _, trace = self.run_entry("--dry-run")
        self.assertEqual(trace[-1]["outcome"], "preview")
        self.expired = True
        _, trace = self.run_entry()
        self.assertEqual(trace[-1]["outcome"], "auth_required")
        self.session.unlink()
        _, trace = self.run_entry()
        self.assertEqual(trace[-1]["outcome"], "auth_required")
        self.assertFalse(self.intent.exists())
        self.assertEqual(self.form.clicks, 0)

    def test_concurrent_trigger_and_ambiguous_restart_reconcile_without_another_click(self):
        self.confirmed = False
        competing = io.StringIO()

        def second_trigger():
            with contextlib.redirect_stdout(competing):
                self.assertEqual(parking_release.main(["run", "--state-dir", str(self.state)]), 0)

        code, trace = self.run_entry(during_launch=second_trigger)
        self.assertEqual(json.loads(competing.getvalue().splitlines()[-1])["outcome"], "competing")
        self.assertEqual(code, 1)
        self.assertEqual(trace[-1]["outcome"], "uncertain")
        intent = self.intent.read_bytes()
        code, trace = self.run_entry()
        self.assertEqual(code, 1)
        self.assertEqual(trace[-1]["outcome"], "uncertain")
        self.form.after_submit = [self.confirmed_record]
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertTrue(next(row for row in trace if row["event"] == "confirmation_observed")["reconciled"])
        self.assertEqual(self.form.clicks, 1)
        self.assertEqual(self.intent.read_bytes(), intent)
        if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
            print(competing.getvalue(), end="")
            for row in trace:
                print(json.dumps(row))

    def test_external_idp_redirect_is_auth_required(self):
        form = ParkingForm(Mock(url="https://login.example.test/sso"))
        with self.assertRaises(BookingProblem) as error:
            form.check_authenticated("https://bookker.example.test/")
        self.assertEqual(error.exception.next_action, SIGN_IN)


if __name__ == "__main__":
    unittest.main()
