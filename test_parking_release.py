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
from parking_booking import (
    BOOK_ACTION, BOOKING_LOADING, FINAL_ACTION, FINAL_ATTEMPT, READY_BUTTON, SIGN_IN, SUBMISSION_VIEW,
    WATCH_SPACE_SEARCH, BookingProblem, ParkingForm, read_intent, write_json,
)
from parking_schedule import EXAMPLE, PRODUCTION, Schedule, ZONE
from test_parking_booking import MATCH, OfflineForm, locator


SEARCH_COMPLETION_FIXTURE = r"""async (source, loading) => {
    const button = {contains: el => el === button};
    const cached = {textContent: 'Space: STALE-144'};
    const loader = inResults => ({
        closest: selector => inResults && selector === 'app-resources',
        getClientRects: () => [1]
    });
    const listeners = new Map();
    let loaders = [];
    let notify;
    let complete = false;
    const document = {
        documentElement: {},
        querySelectorAll: selector => selector === loading ? loaders : [],
        addEventListener: (type, listener) => listeners.set(type, listener),
        removeEventListener: type => listeners.delete(type)
    };
    class MutationObserver {
        constructor(callback) { this.callback = callback; }
        observe() { notify = this.callback; }
        disconnect() { notify = undefined; }
    }
    const create = new Function('document', 'MutationObserver', 'getComputedStyle',
                                `return (${source});`);
    const watch = create(document, MutationObserver, () => ({visibility: 'visible'}));
    watch(button, loading).completed.then(() => { complete = true; });
    const click = (target, isTrusted) => listeners.get('click')?.({isTrusted, composedPath: () => [target]});
    click({}, true);
    click(button, false);
    if (notify) throw new Error('An unrelated click armed the search');
    click(button, true);
    const mutate = async (nextLoaders, rows = [cached]) => {
        loaders = nextLoaders;
        notify?.([{type: 'childList', addedNodes: rows}]);
        await Promise.resolve();
    };
    await mutate([loader(false)]);
    await mutate([], [{...cached}]);
    if (complete) throw new Error('An unrelated loader or a cached render completed the search');
    const searchLoader = loader(true);
    await mutate([searchLoader]);
    await mutate([searchLoader], [{...cached}]);
    if (complete) throw new Error('Cached inventory completed the search while it was loading');
    await mutate([loader(false)], [{...cached}]);
    if (!complete || notify) throw new Error('The completed search did not release its results');
}"""


class SearchCompletionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_observer_waits_for_the_dispatched_search_to_finish(self):
        script = """const [fixture, source, loading] = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
        eval(fixture)(source, loading).catch(error => { console.error(error); process.exitCode = 1; });"""
        result = subprocess.run(["node", "-e", script], input=json.dumps([
            SEARCH_COMPLETION_FIXTURE, WATCH_SPACE_SEARCH, BOOKING_LOADING,
        ]), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


class FixtureClock:
    def __init__(self, now):
        self.start = now
        self.at = now
        self.sleeps = []

    def now(self):
        return self.at

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)

    def monotonic(self):
        return (self.at - self.start).total_seconds()

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

    def bookings(self, base_url, day, *, reload, include_inactive=False):
        if reload and self.fixture.expired_on_reload:
            raise BookingProblem("Bookker requires sign-in.", SIGN_IN)
        return super().bookings(base_url, day, reload=reload, include_inactive=include_inactive)

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

    def wait_for_submission(self, before, *, watch_final, verify_prompt, scheduled):
        assert scheduled
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
        self.search_complete = False
        self.empty = locator(0)
        self.empty.all.return_value = []
        self.label = Mock(return_value=self.empty)
        self.empty_message = locator(0)
        self.empty_message.first = self.empty_message
        self.empty_message.count.side_effect = lambda: int(
            self.fixture.unavailable if self.search_complete else self.fixture.stale_empty
        )
        self.search = locator()
        self.search.first = self.search
        self.search.press.side_effect = self.dispatch_search
        self.search.evaluate_handle.side_effect = self.watch_search
        self.watch = Mock()
        self.day_loader = locator()
        self.day_loader.first = self.day_loader
        self.day_loader.wait_for.side_effect = self.shifts_loaded
        self.shifts = locator()
        self.shifts.first = self.shifts
        self.shifts.all.side_effect = self.available_shifts
        self.shifts_loaded_at = None
        self.rows = locator()
        self.rows.first = self.rows
        self.rows.all.side_effect = self.available_rows
        self.rows.count.side_effect = lambda: len(self.available_rows())
        self.rows.or_.side_effect = lambda other: (
            other if other is self.empty_message and not self.rows.count() else self.rows
        )
        self.stale_row = locator(text="Space: STALE-144\nAvailable\n07:00–23:00")
        self.cached_copy = locator(text=self.stale_row.inner_text.return_value)
        self.list_button = locator(0)
        self.list_button.first = self.list_button
        self.list_button.or_.side_effect = lambda other: other if other is self.rows else self.list_button
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
        self.choose.click.side_effect = lambda **kwargs: self.events.append("resource")
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
        page.get_by_text.return_value = self.empty_message
        page.get_by_role.side_effect = self.role
        page.wait_for_function.side_effect = self.finish_search
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

    def set_date(self, value, *, timeout):
        assert self.fixture.clock.now() >= self.fixture.occurrence.release
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
            shift.evaluate.side_effect = lambda script, interval=interval, **kwargs: interval in self.selected
            shift.click.side_effect = lambda interval=interval, **kwargs: self.selected.add(interval)
            controls.append(shift)
        return controls

    def dispatch_search(self, key, *, timeout):
        assert key == "Enter" and self.selected == {"07:00–14:00", "14:00–23:00"}
        assert self.fixture.clock.now() >= self.fixture.occurrence.release
        self.events.append("search")
        self.submitted_search = True
        if self.fixture.stale_results or self.fixture.stale_empty:
            self.events.append("cached_rerender")

    def watch_search(self, script, loading, *, timeout):
        assert not self.submitted_search and script == WATCH_SPACE_SEARCH and loading == BOOKING_LOADING
        if self.fixture.stale_results:
            assert self.rows.all() == [self.stale_row]
        return self.watch

    def finish_search(self, script, *, arg, timeout):
        assert self.submitted_search and not self.search_complete
        assert arg is self.watch and script == "watch => watch.completed"
        if self.fixture.stale_results:
            assert self.rows.all() == [self.cached_copy]
        if self.fixture.stale_empty:
            assert self.empty_message.count() == 1
        seconds = timeout / 1000
        if self.fixture.result_seconds > seconds:
            self.fixture.clock.advance(seconds)
            raise TimeoutError("results did not refresh")
        self.events.append("results")
        self.fixture.clock.advance(self.fixture.result_seconds)
        self.search_complete = True

    def available_rows(self):
        if self.search_complete:
            return [] if self.fixture.unavailable else [self.row]
        if self.fixture.stale_results:
            return [self.cached_copy if self.submitted_search else self.stale_row]
        return []

    def submit_ready(self, **kwargs):
        assert "resource" in self.events
        self.fixture.clock.advance(0.007)
        self.fixture.review_at = self.fixture.clock.now()

    def review(self, *args, **kwargs):
        self.selection, submit = ParkingForm.review(self, *args, **kwargs)
        self.events.append("review")
        return self.selection, submit


class ReleaseConfirmation(ReleaseControls):
    final_prompt = ParkingForm.final_prompt
    submission_text = ParkingForm.submission_text
    wait_for_submission = ParkingForm.wait_for_submission
    verify_final_prompt = ParkingForm.verify_final_prompt

    def __init__(self, testcase, page):
        super().__init__(testcase, page)
        self.final = locator()
        self.final.is_enabled.side_effect = lambda **kwargs: self.final_ready
        self.final.click.side_effect = self.confirm
        self.dialog.get_by_role.side_effect = self.dialog_action
        self.first_text = self.dialog.inner_text.return_value
        self.dialog.inner_text.side_effect = self.view_text
        self.final_ready = False
        self.final_clicked = False
        self.prompt_at = None
        body = locator()
        body.inner_text.side_effect = self.view_text
        base_locators = page.locator.side_effect
        page.locator.side_effect = lambda selector: body if selector == "body" else base_locators(selector)
        page.wait_for_function.side_effect = self.wait_for_change

    def dialog_action(self, role, name):
        if name == FINAL_ACTION:
            return self.final if self.clicks and not self.final_clicked else self.empty
        if name == BOOK_ACTION and self.clicks:
            return self.empty
        return self.submit

    def view_text(self, **kwargs):
        if self.final_clicked:
            return "Booking confirmed"
        if self.clicks:
            return self.fixture.confirmed_record + "Confirm booking"
        return self.first_text

    def click(self, *, timeout):
        assert timeout > 0 and read_intent(self.intent) == self.selection
        self.clicks += 1
        self.events.append("book")
        self.prompt_at = self.fixture.clock.now() + timedelta(seconds=self.fixture.final_seconds)

    def wait_for_change(self, script, *, arg, timeout):
        if script != SUBMISSION_VIEW:
            return self.finish_search(script, arg=arg, timeout=timeout)
        if arg["before"] is not None:
            seconds = (self.prompt_at - self.fixture.clock.now()).total_seconds()
            if seconds > timeout / 1000:
                self.fixture.clock.advance(timeout / 1000)
                raise TimeoutError("final control did not become enabled")
            self.fixture.clock.advance(seconds)
            self.final_ready = True
        return Mock(**{"json_value.return_value": [self.view_text(), "0" if self.final_ready else ""]})

    def confirm(self, *, timeout):
        assert timeout > 0 and self.fixture.clock.now() == self.prompt_at
        claim = json.loads(self.intent.with_suffix(FINAL_ATTEMPT).read_text())
        assert claim["action"] == "confirm-reviewed-booking-once"
        self.events.append("final")
        self.final_clicked = True
        self.after_submit = [self.fixture.confirmed_record]


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
        self.stale_results = False
        self.stale_empty = False
        self.expired = False
        self.expired_on_reload = False
        self.prep_seconds = 2
        self.review_seconds = 0
        self.result_seconds = 0.035
        self.final_seconds = 0.010

    def run_entry(self, *args, schedule=EXAMPLE, during_launch=None, notifications=False):
        stdout = io.StringIO()

        def launch(client, *, storage_state, timezone_id, wait_timeout):
            self.assertEqual(Path(storage_state), self.session)
            self.assertEqual(timezone_id, "Europe/Madrid")
            self.assertGreater(wait_timeout(30_000), 0)
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

    def test_search_results_wait_uses_only_remaining_release_time(self):
        self.form = ReleaseControls(self, self.page)
        self.stale_results = True
        self.clock.at = self.occurrence.release + timedelta(seconds=56.942)
        self.result_seconds = 0.2
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(self.fixture_time(trace, "refresh_dispatch"),
                         self.occurrence.release + timedelta(seconds=59.960))
        self.page.wait_for_function.assert_called_once_with("watch => watch.completed", arg=self.form.watch, timeout=40)
        self.assertEqual(trace[-1]["outcome"], "missed")
        self.assertEqual(trace[-1]["offset_ms"], 60000)
        self.assertNotIn("resource", self.form.events)
        self.form.cached_copy.get_by_role.assert_not_called()
        self.assertFalse(self.intent.exists())

    def test_window_closing_during_intent_persistence_reports_missed_without_click(self):
        def persist(*args, **kwargs):
            write_json(*args, **kwargs)
            self.clock.at = self.occurrence.close

        with patch("parking_booking.write_json", side_effect=persist):
            code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "missed")
        self.assertNotIn("submit_dispatch", {row["event"] for row in trace})
        self.assertEqual(self.form.clicks, 0)
        self.assertEqual(read_intent(self.intent), self.form.selection)

    def test_rerendered_cached_rows_are_ignored_until_the_search_completes(self):
        self.form = ReleaseControls(self, self.page)
        self.stale_results = True
        self.result_seconds = 0.150
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertEqual(self.form.events[4:8], ["search", "cached_rerender", "results", "resource"])
        self.assertEqual(self.fixture_time(trace, "submit_dispatch"),
                         self.occurrence.release + timedelta(milliseconds=175))
        self.assertEqual(self.fixture_time(trace, "submit_dispatch"), self.review_at)
        self.assertEqual(self.clock.sleeps, [57])
        self.assertEqual(read_intent(self.intent).space, "PTQ-123")
        self.form.stale_row.get_by_role.assert_not_called()
        self.form.cached_copy.get_by_role.assert_not_called()
        if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
            for row in trace:
                print(json.dumps(row))

    def test_cached_empty_message_is_ignored_until_the_search_completes(self):
        self.form = ReleaseControls(self, self.page)
        self.stale_empty = True
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertEqual(self.form.events[4:8], ["search", "cached_rerender", "results", "resource"])

    def test_completed_search_with_empty_inventory_exits_without_an_intent(self):
        self.form = ReleaseControls(self, self.page)
        self.unavailable = True
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "unavailable")
        self.assertEqual(self.form.events[-2:], ["search", "results"])
        self.assertFalse(self.intent.exists())

    def test_reload_confirms_the_saved_full_day_from_contiguous_shifts(self):
        self.form = ReleaseControls(self, self.page)
        self.confirmed_record = self.confirmed_record.replace("7:00 AM – 11:00 PM", "07:00–14:00\n14:00–23:00")
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertEqual(trace[-2]["event"], "confirmation_observed")
        selection = read_intent(self.intent)
        self.assertEqual((selection.start_time, selection.end_time), ("07:00", "23:00"))
        self.assertEqual(self.form.clicks, 1)
        if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
            for row in trace:
                print(json.dumps(row))

    def test_separate_confirmation_dispatches_as_soon_as_enabled(self):
        self.form = ReleaseConfirmation(self, self.page)
        code, trace = self.run_entry()
        self.assertEqual(code, 0)
        self.assertEqual(trace[-1]["outcome"], "confirmed")
        self.assertEqual(self.fixture_time(trace, "final_submit_dispatch"), self.form.prompt_at)
        self.assertEqual(self.fixture_time(trace, "final_submit_dispatch")
                         - self.fixture_time(trace, "submit_dispatch"), timedelta(milliseconds=10))
        self.page.wait_for_timeout.assert_not_called()
        self.assertEqual(self.form.events[-3:], ["book", "final", "reload"])
        if os.environ.get("BOOKKER_OFFLINE_TRACE") == "1":
            for row in trace:
                print(json.dumps(row))

    def test_release_close_during_confirmation_still_allows_bounded_readback(self):
        self.form = ReleaseConfirmation(self, self.page)
        self.clock.at = self.occurrence.release + timedelta(seconds=56.935)
        readback = self.form.bookings

        def bookings(*args, **kwargs):
            if kwargs["reload"]:
                self.assertEqual(self.clock.now(), self.occurrence.close)
                self.assertEqual(self.form.wait_timeout(), 20_000)
                self.clock.advance(20)
            return readback(*args, **kwargs)

        with patch.object(self.form, "bookings", side_effect=bookings):
            code, trace = self.run_entry()
        self.assertEqual(code, 1)
        self.assertEqual(trace[-1]["offset_ms"], 80000)
        self.assertEqual(trace[-1]["outcome"], "uncertain")
        self.assertIn("readback limit", trace[-1]["reason"])
        self.assertNotIn("final_submit_dispatch", {row["event"] for row in trace})
        self.assertFalse(self.intent.with_suffix(FINAL_ATTEMPT).exists())
        self.assertEqual(self.form.events[-2:], ["book", "reload"])

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

    def test_expiry_after_ambiguous_click_alerts_once_and_stays_uncertain(self):
        self.expired_on_reload = True
        self.confirmed = False
        mailer = Mock()
        with patch.object(parking_release, "RenewalMail", return_value=mailer):
            for notice in ("sent", "suppressed"):
                code, trace = self.run_entry(notifications=True)
                self.assertEqual(code, 1)
                self.assertEqual(trace[-1]["outcome"], "uncertain")
                self.assertEqual(next(row["outcome"] for row in trace if row["event"] == "expiry_alert"), notice)
                self.assertEqual(read_intent(self.intent).date, self.occurrence.target.isoformat())
        mailer.send.assert_called_once()
        self.assertEqual(self.form.clicks, 1)

    def test_external_idp_redirect_is_auth_required(self):
        form = ParkingForm(Mock(url="https://login.example.test/sso"))
        with self.assertRaises(BookingProblem) as error:
            form.check_authenticated("https://bookker.example.test/")
        self.assertEqual(error.exception.next_action, SIGN_IN)


if __name__ == "__main__":
    unittest.main()
