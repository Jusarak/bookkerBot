import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import run_bookker
from parking_booking import (
    NO_BOOKINGS, TORRES_SHIFTS, BookingProblem, ParkingForm, Selection, SpaceChoice, dates, intervals, period_covers,
    parking_dates, read_intent, verify_booking, write_json,
)


class RunDate(datetime):
    @classmethod
    def now(cls, zone):
        assert zone == ZoneInfo("Europe/Madrid")
        return cls(2026, 11, 20, tzinfo=zone)


class OfflineForm:
    """Model the Bookker views without opening a browser or reading session data."""

    day_first = False
    vehicle = "private fixture vehicle"

    def __init__(self):
        self.days = []
        self.bookings_reads = []
        self.existing = ["Type: Parking\n11/20/2026\n07:00–23:00"]
        self.after_submit = []
        self.clicks = 0
        self.timeout = False
        self.exact_dates = []

    def check_session(self):
        pass

    def bookings(self, base_url, day, *, reload):
        self.bookings_reads.append((day.day, reload))
        return self.after_submit if reload else self.existing

    def open(self, building, floor, vehicle):
        assert (building, floor) == ("NEWTON", "Parking Torres Quevedo")
        assert vehicle in (None, self.vehicle)
        return self.vehicle

    def location(self, label, value, *, select):
        assert not select
        assert (label, value) in (("Building", "NEWTON"), ("Floor", "Parking Torres Quevedo"))

    def selected_vehicle(self, vehicle):
        assert vehicle == self.vehicle
        return vehicle

    def check_repeat(self):
        pass

    def next_date(self, earliest, *, exact=False):
        self.days.append(earliest)
        self.day = earliest
        if exact:
            self.exact_dates.append(earliest)
            return earliest
        last = RunDate.now(ZoneInfo("Europe/Madrid")).date() + timedelta(days=3)
        return earliest if earliest <= last else None

    def check_date(self, day):
        assert self.day == day

    def whole_day(self, expected_shifts):
        assert expected_shifts == (TORRES_SHIFTS if self.day.day == 26 else ())
        if self.day == date(2026, 11, 21):
            return None  # One displayed shift has ended.
        return ("07:00", "23:00"), False

    def select_space(self, building, floor, span, spot_name, spot_id, *, require_material):
        assert span == ("07:00", "23:00")
        assert require_material == (self.day.day == 26)
        if self.day == date(2026, 11, 22):
            return None  # No space is available for the requested interval.
        return SpaceChoice("PTQ-123", material=require_material)

    def review(self, day, span, all_day, building, floor, choice, vehicle):
        assert vehicle == self.vehicle
        self.selection = Selection(day.isoformat(), *span, building, floor, choice.name, all_day)
        return self.selection, self

    def click(self):
        self.clicks += 1
        assert read_intent(self.intent) == self.selection
        assert json.loads(self.receipt.read_text())["outcome"] == "unknown"
        if self.timeout:
            raise TimeoutError("potentially sensitive browser error")


MATCH = """Type: Parking
November 23, 2026
7:00 AM – 11:00 PM
Building: NEWTON
Floor: Parking Torres Quevedo
Space: PTQ-123
Status: Confirmed
Confirmation reference: OFFLINE-123
Vehicle: private fixture vehicle
"""


class ParkingCommandTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(work.cleanup)
        work = Path(work.name)
        self.form = OfflineForm()
        self.form.intent = work / "intent.json"
        self.form.receipt = work / "booking.json"
        self.auth = work / "synthetic-session.json"
        self.args = [
            "run_bookker.py", "book", "--building", "NEWTON", "--floor", "Parking Torres Quevedo",
            "--from-date", "2026-11-19", "--whole-day", "--auth", str(self.auth),
            "--submission-state", str(self.form.intent), "--result-file", str(self.form.receipt),
        ]

    def run_command(self, *extra):
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", self.args + list(extra)),
            patch("parking_booking.datetime", RunDate),
            patch("parking_booking.ParkingForm", return_value=self.form),
            patch.object(run_bookker.BookkerClient, "open_with_session", autospec=True) as launch,
            contextlib.redirect_stdout(stdout),
        ):
            run_bookker.main()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[0].auth_state_path, self.auth)
        self.assertEqual(launch.call_args.kwargs, {"timezone_id": "Europe/Madrid"})
        self.assertNotIn(self.form.vehicle, stdout.getvalue())
        result = json.loads(self.form.receipt.read_text())
        self.assertEqual(result, json.loads(stdout.getvalue()))
        return result

    def test_dry_run_skips_existing_partial_and_unavailable_dates(self):
        result = self.run_command()
        self.assertEqual(self.form.days, [date(2026, 11, day) for day in (20, 21, 21, 22, 22, 23, 23)])
        self.assertEqual(self.form.bookings_reads, [(day, False) for day in range(20, 24)])
        self.assertEqual(self.form.clicks, 0)
        self.assertFalse(self.form.intent.exists())
        self.assertEqual(result["outcome"], "blocked")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["date"], "2026-11-23")

    def test_timed_out_click_reloads_and_blocks_duplicate_until_reconciled(self):
        self.form.timeout = True
        self.form.after_submit = [MATCH.replace("11:00 PM", "2:00 PM")]
        self.assertEqual(self.run_command("--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.bookings_reads[-2:], [(23, False), (23, True)])
        self.assertEqual(self.form.clicks, 1)
        self.assertNotIn(self.form.vehicle, self.form.intent.read_text())
        self.assertEqual(self.run_command("--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.clicks, 1)

        self.form.after_submit = [MATCH, MATCH]
        self.assertEqual(self.run_command("--confirm")["outcome"], "unknown")
        for status in ("Pending", "Rejected", ""):
            self.form.after_submit = [MATCH.replace("Status: Confirmed\n", f"Status: {status}\n" if status else "")]
            self.assertEqual(self.run_command("--confirm")["outcome"], "unknown")
            self.assertEqual(read_intent(self.form.intent), self.form.selection)
        self.form.after_submit = [MATCH]
        result = self.run_command("--confirm")
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(result["confirmation_reference"], "OFFLINE-123")
        self.assertEqual(result["start_time"], "07:00")
        self.assertEqual(result["end_time"], "23:00")
        self.assertEqual(result["space"], "PTQ-123")
        self.assertTrue(result["reconciled"])
        self.assertEqual(self.form.clicks, 1)

    def test_success_requires_a_reloaded_matching_booking(self):
        self.form.after_submit = [MATCH]
        result = self.run_command("--confirm")
        self.assertEqual(self.form.bookings_reads[-2:], [(23, False), (23, True)])
        self.assertEqual(self.form.clicks, 1)
        self.assertEqual(result["outcome"], "confirmed")

    def test_exact_date_checks_one_period_and_recovers_without_submitting_again(self):
        target = date(2026, 11, 26)
        args = ("--on-date", target.isoformat())
        preview = self.run_command(*args)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["date"], target.isoformat())
        self.assertFalse(self.form.intent.exists())
        self.assertEqual(self.form.exact_dates, [target, target])
        self.assertEqual(self.form.bookings_reads, [(26, False)])

        match = MATCH.replace("November 23", "November 26")
        self.form.after_submit = [match.replace("11:00 PM", "2:00 PM")]
        self.assertEqual(self.run_command(*args, "--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.clicks, 1)
        self.assertEqual(self.form.bookings_reads, [(26, False), (26, False), (26, True)])
        self.assertEqual(self.form.exact_dates, [target] * 4)

        self.form.after_submit = [match.replace("7:00 AM – 11:00 PM", "07:00–14:00\n14:00–23:00")]
        result = self.run_command(*args, "--confirm")
        self.assertEqual(result, {
            "outcome": "confirmed", "timezone": "Europe/Madrid", "date": "2026-11-26",
            "start_time": "07:00", "end_time": "23:00", "building": "NEWTON",
            "floor": "Parking Torres Quevedo", "space": "PTQ-123", "all_day": False,
            "confirmation_reference": "OFFLINE-123", "reconciled": True,
        })
        self.assertEqual(self.form.clicks, 1)
        self.assertEqual(self.form.exact_dates, [target] * 4)
        self.assertEqual(self.form.bookings_reads, [(26, False), (26, False), (26, True), (26, True)])

    def test_exact_date_stops_if_another_day_is_offered(self):
        with patch.object(self.form, "next_date", return_value=date(2026, 11, 27)) as choose:
            result = self.run_command("--on-date", "2026-11-26", "--confirm")
        choose.assert_called_once_with(date(2026, 11, 26), exact=True)
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("different date", result["reason"])
        self.assertEqual(self.form.bookings_reads, [])
        self.assertFalse(self.form.intent.exists())

    def test_exact_date_stops_at_an_existing_reservation(self):
        self.form.existing = [MATCH.replace("November 23", "November 26")]
        result = self.run_command("--on-date", "2026-11-26", "--confirm")
        self.assertEqual(result["outcome"], "unavailable")
        self.assertIn("already shows a parking booking", result["reason"])
        self.assertEqual(self.form.bookings_reads, [(26, False)])
        self.assertEqual(self.form.days, [date(2026, 11, 26)])
        self.assertFalse(self.form.intent.exists())

    def test_exact_date_stops_at_unavailable_shifts_or_spaces(self):
        for method, reason in (("whole_day", "07:00–14:00 and 14:00–23:00"),
                               ("select_space", "the full interval")):
            with self.subTest(method=method), patch.object(self.form, method, return_value=None):
                self.form.days.clear()
                self.form.bookings_reads.clear()
                result = self.run_command("--on-date", "2026-11-26", "--confirm")
                self.assertEqual(result["outcome"], "unavailable")
                self.assertIn(reason, result["reason"])
                self.assertEqual(self.form.days, [date(2026, 11, 26)] * 2)
                self.assertEqual(self.form.bookings_reads, [(26, False)])
                self.assertFalse(self.form.intent.exists())

    def test_exact_date_preserves_a_conflicting_intent(self):
        for day, end in (("2026-11-27", "23:00"), ("2026-11-26", "14:00")):
            with self.subTest(day=day, end=end):
                intent = {"version": 1, "date": day, "start_time": "07:00", "end_time": end,
                          "building": "NEWTON", "floor": "Parking Torres Quevedo", "space": "PTQ-123"}
                write_json(self.form.intent, intent)
                with (
                    patch.object(sys, "argv", self.args + ["--on-date", "2026-11-26", "--confirm"]),
                    patch.object(run_bookker.BookkerClient, "open_with_session") as launch,
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    run_bookker.main()
                launch.assert_not_called()
                self.assertEqual(json.loads(self.form.receipt.read_text())["outcome"], "unknown")
                self.assertEqual(json.loads(self.form.intent.read_text()), intent)

    def test_missing_auth_reports_sign_in_from_real_entry_point(self):
        command = [sys.executable, *self.args, "--confirm"]
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        receipt = json.loads(self.form.receipt.read_text())
        self.assertEqual(json.loads(result.stdout), receipt)
        self.assertEqual(receipt["outcome"], "blocked")
        self.assertIn("normal Bookker sign-in", receipt["next_action"])
        self.assertFalse(self.form.intent.exists())

    def test_email_first_login_reports_normal_sign_in_without_reading_bookings(self):
        page = Mock(url="https://bookker.test/#/login")
        self.form.check_session = ParkingForm(page).check_session
        result = self.run_command("--confirm")
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("normal Bookker sign-in", result["next_action"])
        self.assertEqual(self.form.bookings_reads, [])
        self.assertFalse(self.form.intent.exists())
        self.assertTrue(any("app-login input" in call.args[0] for call in page.locator.call_args_list))

    def test_offered_window_can_end_without_creating_intent(self):
        result = self.run_command("--from-date", "2026-11-24", "--confirm")
        self.assertEqual(result["outcome"], "unavailable")
        self.assertEqual(self.form.clicks, 0)
        self.assertFalse(self.form.intent.exists())

    def test_intent_is_never_overwritten(self):
        write_json(self.form.intent, {"outcome": "fixture"}, exclusive=True)
        with self.assertRaises(FileExistsError):
            write_json(self.form.intent, {"outcome": "replacement"}, exclusive=True)
        self.assertEqual(json.loads(self.form.intent.read_text()), {"outcome": "fixture"})

    def test_displayed_madrid_date_and_shift_formats(self):
        self.assertEqual(dates("jueves, 19 de noviembre de 2026", True), {date(2026, 11, 19)})
        self.assertEqual(dates("11/19/2026", False), {date(2026, 11, 19)})
        self.assertEqual(intervals("7:00 AM - 2:00 PM\n2:00 PM – 11:00 PM"),
                         {("07:00", "14:00"), ("14:00", "23:00")})

    def test_bookings_navigation_reloads_then_sets_period_before_reading_empty_state(self):
        page = Mock(url="https://bookker.test/")
        link = page.get_by_role.return_value.or_.return_value.filter.return_value
        link.click.side_effect = lambda **kwargs: setattr(page, "url", "https://bookker.test/#/bookings")
        self.assertTrue(NO_BOOKINGS.fullmatch("info You do not have any bookings for the selected period yet"))
        form = ParkingForm(page)
        day = date(2026, 11, 23)  # Next week in Madrid.
        period = page.get_by_label.return_value.filter.return_value
        period.evaluate.side_effect = [False]
        period.inner_text.side_effect = ["This week", "Next week", "Next week"]
        option = Mock()
        option.inner_text.return_value = "Next week"
        page.get_by_role.return_value.or_.return_value.filter.return_value.all.return_value = [option]
        page.locator.return_value.filter.return_value.count.return_value = 0
        page.locator.return_value.and_.return_value.filter.return_value.evaluate_all.return_value = []
        empty = page.get_by_text.return_value.and_.return_value.filter.return_value.first
        empty.count.return_value = 1
        empty.inner_text.return_value = "info You do not have any bookings for the selected period yet"
        with patch("parking_booking.datetime", RunDate), patch.object(form, "check_session"), patch.object(
            form, "label", return_value=Mock(**{"count.return_value": 0})
        ):
            page.get_by_role.return_value.filter.return_value.count.return_value = 0
            page.get_by_role.return_value.filter.return_value.first.count.return_value = 0
            self.assertEqual(form.bookings("https://bookker.test/", day, reload=True), [])
        navigation = next(call for call in page.get_by_role.call_args_list if call.args == ("link",))
        self.assertRegex("calendar_today Bookings", navigation.kwargs["name"])
        page.reload.assert_called_once()
        period.click.assert_called_once()
        option.click.assert_called_once()

    def test_bookings_card_wrapping_the_period_and_empty_state(self):
        page = Mock(url="https://bookker.test/#/bookings")
        view = page.locator.return_value.filter.return_value
        view.count.return_value = 1
        period = view.get_by_label.return_value.filter.return_value
        period.evaluate.return_value = False
        period.inner_text.return_value = "This week"
        empty = view.get_by_text.return_value.and_.return_value.filter.return_value.first
        empty.count.return_value = 1
        empty.inner_text.return_value = "info You do not have any bookings for the selected period yet"
        view.get_by_role.return_value.filter.return_value.first.count.return_value = 0
        cards = view.locator.return_value.and_.return_value.filter.return_value
        wrapper = f"Period\nThis week\n{empty.inner_text.return_value}"
        cards.evaluate_all.return_value = [(wrapper, "card")]
        form = ParkingForm(page)
        day = date(2026, 11, 20)

        with patch("parking_booking.datetime", RunDate), patch.object(form, "check_session"), patch.object(
            form, "label", return_value=Mock(**{"count.return_value": 0})
        ):
            records = form.bookings("https://bookker.test/", day, reload=False)
            self.assertEqual(parking_dates(records, form), set())
            page.get_by_text.assert_not_called()
            view.get_by_text.assert_called_with(NO_BOOKINGS)

            for rows in ([(wrapper, "layout"), (MATCH, "row")], [(f"{wrapper}\n{MATCH}", "card")]):
                with self.subTest(rows=rows):
                    cards.evaluate_all.return_value = rows
                    with self.assertRaisesRegex(BookingProblem, "alongside its Bookings empty state"):
                        form.bookings("https://bookker.test/", day, reload=False)

            empty.count.return_value = 0
            cards.evaluate_all.return_value = [("Period\nThis week", "card")]
            with self.assertRaisesRegex(BookingProblem, "did not show a readable Bookings list or empty state"):
                form.bookings("https://bookker.test/", day, reload=False)
            cards.evaluate_all.return_value = [(MATCH, "card")]
            self.assertEqual(form.bookings("https://bookker.test/", day, reload=False), [MATCH])

    def test_bookings_callback_keeps_explicit_rows_separate_inside_a_card(self):
        node = shutil.which("node")
        playwright = importlib.util.find_spec("playwright")
        if not node and playwright and playwright.origin:
            driver = Path(playwright.origin).parent / "driver" / "node"
            if driver.is_file():
                node = str(driver)
        if not node:
            self.skipTest("JavaScript runtime is unavailable")

        page = Mock(url="https://bookker.test/#/bookings")
        view = page.locator.return_value.filter.return_value
        view.count.return_value = 1
        empty = view.get_by_text.return_value.and_.return_value.filter.return_value.first
        empty.count.return_value = 0
        view.get_by_role.return_value.filter.return_value.first.count.return_value = 0
        cards = view.locator.return_value.and_.return_value.filter.return_value
        period = Mock()
        period.inner_text.return_value = "This week"
        day = date(2026, 11, 23)
        selection = Selection(day.isoformat(), "07:00", "23:00", "NEWTON",
                              "Parking Torres Quevedo", "PTQ-123")

        def evaluate(callback, selector, sibling, extra):
            # Run the actual browser callback over a synthetic card and two rows.
            fixture = r"""
                const [callback, selector, first, second, extra] =
                    JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
                const card = {
                    innerText: ['Period', 'This week', first, second, extra].join('\n'),
                    parentElement: null,
                    matches: () => false,
                };
                const row = innerText => ({
                    innerText, parentElement: card, matches: test => test === selector,
                    contains(other) { return other === this; },
                });
                console.log(JSON.stringify(eval(callback)([card, row(first), row(second)], selector)));
            """
            result = subprocess.run(
                [node, "-e", fixture], input=json.dumps([callback, selector, MATCH, sibling, extra]),
                check=True, capture_output=True, text=True, timeout=10,
            )
            return json.loads(result.stdout)

        sibling = MATCH.replace("OFFLINE-123", "OFFLINE-124")
        form = ParkingForm(page)
        with patch.object(form, "check_session"), patch.object(
            form, "select_booking_period", return_value=period
        ), patch.object(form, "label", return_value=Mock(**{"count.return_value": 0})):
            cards.evaluate_all.side_effect = lambda *args: evaluate(*args, sibling, "")
            records = form.bookings("about:blank", day, reload=False)
            self.assertEqual(records, [MATCH.strip(), sibling.strip()])
            result = verify_booking(records, selection, self.form, recovered=True)
            self.assertEqual(result["outcome"], "unknown")
            self.assertIn("multiple matching reservations", result["reason"])

            rejected = sibling.replace("Status: Confirmed", "Status: Rejected")
            cards.evaluate_all.side_effect = lambda *args: evaluate(*args, rejected, "")
            records = form.bookings("about:blank", day, reload=False)
            self.assertEqual(records, [MATCH.strip()])
            self.assertEqual(parking_dates(records, self.form), {day})

            cards.evaluate_all.side_effect = lambda *args: evaluate(*args, sibling, "Status: Pending")
            with self.assertRaisesRegex(BookingProblem, "did not expose separate reservation rows"):
                form.bookings("about:blank", day, reload=False)

    def test_named_and_explicit_bookings_periods_cover_the_candidate(self):
        today = date(2026, 11, 20)
        day = date(2026, 11, 23)
        self.assertFalse(period_covers("This week", day, today, False))
        self.assertTrue(period_covers("Next week", day, today, False))
        self.assertTrue(period_covers("Nov 23, 2026 – Nov 29, 2026", day, today, False))
        self.assertFalse(period_covers("This week · Nov 19, 2026 – Nov 21, 2026", day, today, False))

    def test_material_shift_chips_bound_the_whole_day(self):
        page = Mock()
        chips = page.locator.return_value.filter.return_value
        morning, evening = Mock(), Mock()
        morning.inner_text.return_value = "07:00–14:00"
        evening.inner_text.return_value = "14:00–23:00"
        chips.or_.return_value.filter.return_value.all.return_value = [morning, evening]
        form = ParkingForm(page)
        with patch.object(form, "label", return_value=Mock(**{"count.return_value": 0})):
            self.assertEqual(form.whole_day(), (("07:00", "23:00"), False))
            morning.click.assert_called_once()
            evening.click.assert_called_once()
            morning.inner_text.return_value += "\nShift ended"
            self.assertIsNone(form.whole_day())

    def test_exact_shifts_must_both_stay_selected(self):
        page = Mock()
        chips = page.locator.return_value.filter.return_value
        morning, evening = Mock(), Mock()
        morning.inner_text.return_value = "07:00–14:00"
        evening.inner_text.return_value = "14:00–23:00"
        morning.evaluate.return_value = True
        evening.evaluate.side_effect = [False, True]
        chips.or_.return_value.filter.return_value.all.return_value = [morning, evening]
        form = ParkingForm(page)
        with patch.object(form, "label"):
            self.assertEqual(form.whole_day(TORRES_SHIFTS), (("07:00", "23:00"), False))
            morning.click.assert_not_called()
            evening.click.assert_called_once()
            evening.evaluate.side_effect = [False, False]
            with self.assertRaisesRegex(BookingProblem, "did not retain both"):
                form.whole_day(TORRES_SHIFTS)
            evening.inner_text.return_value = "14:00–22:00"
            self.assertIsNone(form.whole_day(TORRES_SHIFTS))

    def test_exact_calendar_skips_unrequested_cells_even_if_available(self):
        page, control = Mock(), Mock()
        calendar = page.locator.return_value.or_.return_value.filter.return_value.first
        cells = calendar.locator.return_value.filter.return_value
        target = date(2026, 11, 26)

        def cell_for(day, enabled):
            cell = Mock()
            attributes = {"data-date": day, "data-outside-month": None}
            cell.get_attribute.side_effect = attributes.__getitem__
            cell.is_enabled.return_value = enabled
            cell.evaluate.return_value = False
            return cell

        requested = cell_for("2026-11-26", False)
        other = cell_for("2026-11-27", True)
        cells.all.return_value = [requested, other]
        form = ParkingForm(page)
        self.assertIsNone(form.calendar_date(control, target, exact=True))
        requested.click.assert_not_called()
        other.click.assert_not_called()
        self.assertEqual(form.calendar_date(control, target), date(2026, 11, 27))
        other.click.assert_called_once()
        requested.is_enabled.return_value = True
        self.assertEqual(form.calendar_date(control, target, exact=True), target)
        requested.click.assert_called_once()

    def test_home_opens_parking_and_reads_the_observed_material_controls(self):
        page = Mock(url="https://bookker.test/#/home")
        route = Mock()
        route.filter.return_value.count.return_value = 1
        fields = Mock()
        captions = Mock()
        page.locator.side_effect = {
            'a[href$="/home"]': route, "mat-form-field": fields, "mat-label, label": captions,
        }.__getitem__
        links, parking, option = Mock(), Mock(), Mock()
        links.filter.return_value.count.return_value = 2
        option.filter.return_value.count.return_value = 1
        page.get_by_role.side_effect = lambda role, **kwargs: {
            "link": links, "button": parking, "option": option,
        }[role]
        by_caption = {}
        controls = {}
        for label, current in {
            "Type": "Parking", "Building": "NEWTON", "Floor": "Other floor", "Vehicle": self.form.vehicle,
        }.items():
            caption = Mock()
            caption.filter.return_value.first.count.return_value = 1
            field = Mock()
            by_caption[caption.and_.return_value] = field
            control = field.locator.return_value.filter.return_value
            control.count.return_value = 1
            control.evaluate.return_value = current
            controls[label] = caption, field, control
        heading = Mock()
        page.get_by_text.side_effect = lambda name, **kwargs: (
            heading if name == "Parking space booking" else controls[name][0]
        )
        fields.filter.side_effect = lambda *, has: by_caption[has]
        floor = controls["Floor"][2]
        floor.evaluate.side_effect = ["Other floor", False, "Parking Torres Quevedo"]
        form = ParkingForm(page)
        with patch.object(form, "check_session"):
            self.assertEqual(form.open("NEWTON", "Parking Torres Quevedo"), self.form.vehicle)
        route.filter.return_value.click.assert_called_once()
        links.or_.assert_not_called()
        parking.filter.return_value.click.assert_called_once()
        button = next(call for call in page.get_by_role.call_args_list if call.args == ("button",))
        self.assertRegex("add Add a Parking booking", button.kwargs["name"])
        heading.filter.return_value.first.wait_for.assert_called_once()
        for _, field, _ in controls.values():
            field.locator.assert_called_with("mat-select")
        floor.click.assert_called_once()
        option.filter.return_value.first.click.assert_called_once()
        controls["Vehicle"][2].click.assert_not_called()

    def test_form_blocker_reports_the_first_failed_operation_at_the_cli(self):
        for stage, reason, action in (
            ("home", "Home navigation link", "Open Home from Bookker's sidebar"),
            ("parking", "Add a Parking booking button", "Check that the Parking card"),
            ("heading", "Parking space booking form", "Open the Parking card on Home"),
            ("building", "parking Building control could not be read", "Check the Building dropdown"),
            ("vehicle", "parking Vehicle control could not be read", "Select the intended vehicle"),
        ):
            with self.subTest(stage=stage):
                page = Mock(url="https://bookker.test/#/home")
                form = ParkingForm(page)
                secret = "private browser error and vehicle"
                error = RuntimeError(secret)
                home = page.locator.return_value.filter.return_value
                home.count.return_value = 1
                parking = page.get_by_role.return_value.filter.return_value
                heading = page.get_by_text.return_value.filter.return_value.first
                heading.count.return_value = 0
                form.check_session = Mock()
                form.control = Mock(side_effect=lambda label: {
                    "Building": Mock(evaluate=Mock(return_value="NEWTON")),
                    "Floor": Mock(evaluate=Mock(return_value="Parking Torres Quevedo")),
                }[label])
                if stage in ("building", "vehicle"):
                    control = form.control.side_effect
                    form.control.side_effect = lambda label: (
                        Mock(evaluate=Mock(side_effect=error)) if label.casefold() == stage else control(label)
                    )
                else:
                    {"home": home.click, "parking": parking.click, "heading": heading.wait_for}[stage].side_effect = error
                with patch.object(self.form, "open", side_effect=form.open):
                    result = self.run_command("--confirm")
                self.assertEqual(result["outcome"], "blocked")
                self.assertIn(reason, result["reason"])
                self.assertIn(action, result["next_action"])
                self.assertNotIn(secret, self.form.receipt.read_text())
                self.assertFalse(self.form.intent.exists())
                if stage == "home":
                    parking.click.assert_not_called()
                if stage == "parking":
                    heading.wait_for.assert_not_called()

    def test_space_search_continues_past_an_unmarked_row(self):
        page = Mock()
        unknown, available = Mock(), Mock()
        unknown.inner_text.return_value = "Space: PTQ-100"
        available.inner_text.return_value = "Space: PTQ-123\nAvailable"
        available.get_by_role.return_value.filter.return_value.count.return_value = 1
        rows = page.locator.return_value.or_.return_value.or_.return_value.filter.return_value
        rows.all.return_value = [unknown, available]
        page.get_by_role.return_value.filter.return_value.count.return_value = 0
        page.get_by_role.return_value.or_.return_value.filter.return_value.count.return_value = 0
        page.locator.return_value.filter.return_value.all.return_value = []
        form = ParkingForm(page)
        with patch.object(form, "location"), patch.object(form, "label", return_value=Mock(**{"count.return_value": 0})):
            self.assertEqual(form.select_space("NEWTON", "Parking Torres Quevedo", ("07:00", "23:00"),
                                               None, None), SpaceChoice("PTQ-123"))
        available.get_by_role.return_value.filter.return_value.click.assert_called_once()
        unknown.click.assert_not_called()

    def test_material_list_opens_the_verified_resource_and_checks_its_dialog(self):
        page = Mock()
        page.locator.return_value.get_attribute.return_value = "en"
        switch, search, list_role, dialog_element = Mock(), Mock(), Mock(), Mock()
        switch.filter.return_value.count.return_value = 1
        switch.filter.return_value.get_attribute.return_value = "false"
        search_button = search.filter.return_value
        list_button = list_role.or_.return_value.filter.return_value
        list_button.count.return_value = 1
        dialog = dialog_element.filter.return_value
        dialog_text = "PTQ-123\nNEWTON / Parking Torres Quevedo\nBook"
        dialog.inner_text.return_value = dialog_text
        submit = dialog.get_by_role.return_value.filter.return_value
        submit.count.return_value = 1
        heading = dialog.locator.return_value.filter.return_value
        heading.count.return_value = 1
        heading.inner_text.return_value = "PTQ-123"

        def role(kind, name=None):
            if kind == "switch":
                return switch
            return search if name.search("Search parking spaces") else list_role

        page.get_by_role.side_effect = role
        rows = Mock()
        base_locator = page.locator.return_value
        page.locator.side_effect = lambda selector: {
            "app-resources article.list-container mat-list-item": rows,
            "mat-dialog-container": dialog_element,
        }.get(selector, base_locator)

        def resource_row(name, hours, location):
            row = Mock()
            row.inner_text.return_value = f"{name}\n{hours}\n{location}"
            resource = row.locator.return_value.filter.return_value
            resource.count.return_value = 1
            names, clickable = Mock(), Mock()
            names.filter.return_value.count.return_value = 1
            names.filter.return_value.inner_text.return_value = name
            clickable.filter.return_value.count.return_value = 1
            resource.locator.side_effect = {".resource-name": names, ".resource.clickable": clickable}.__getitem__
            return row, clickable.filter.return_value

        partial, partial_button = resource_row("PTQ-101", "07:00–14:00", "NEWTON / Parking Torres Quevedo")
        elsewhere, elsewhere_button = resource_row("PTQ-102", "07:00–23:00", "NEWTON / Visitor Parking")
        full, full_button = resource_row("PTQ-123", "07:00–23:00", "NEWTON / Parking Torres Quevedo")
        rows.filter.return_value.all.return_value = [partial, elsewhere, full]
        form = ParkingForm(page)
        building, floor, span = "NEWTON", "Parking Torres Quevedo", ("07:00", "23:00")
        with patch.object(form, "location"), patch.object(form, "label", return_value=Mock(**{"count.return_value": 0})):
            choice = form.select_space(building, floor, span, None, None, require_material=True)
        for control in (search_button, list_button):
            control.focus.assert_called_once()
            control.press.assert_called_once_with("Enter", timeout=10_000)
            control.click.assert_not_called()
        partial_button.click.assert_not_called()
        elsewhere_button.click.assert_not_called()
        full_button.click.assert_called_once()
        self.assertEqual(choice, SpaceChoice("PTQ-123", material=True))

        args = (date(2026, 11, 26), span, False, building, floor, choice, self.form.vehicle)
        selection, control = form.review(*args)
        self.assertEqual(selection.space, "PTQ-123")
        self.assertIs(control, submit)
        for detail in ("November 27, 2026", "Date: 11/27", "07:00–14:00", "Vehicle: another vehicle",
                       "Building: OTHER", "Location: NEWTON / Visitor Parking", "Space: PTQ-124"):
            with self.subTest(detail=detail):
                dialog.inner_text.return_value = f"{dialog_text}\n{detail}"
                with self.assertRaises(BookingProblem):
                    form.review(*args)
        dialog.inner_text.return_value = dialog_text
        heading.inner_text.return_value = "PTQ-124"
        with self.assertRaises(BookingProblem):
            form.review(*args)
        submit.click.assert_not_called()

    def test_bookings_all_day_label_reconciles_an_explicit_all_day_selection(self):
        record = MATCH.replace("7:00 AM – 11:00 PM", "Time: All day")
        selection = Selection("2026-11-23", "07:00", "23:00", "NEWTON",
                              "Parking Torres Quevedo", "PTQ-123", all_day=True)
        result = verify_booking([record], selection, self.form, recovered=True)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual((result["start_time"], result["end_time"]), ("07:00", "23:00"))

    def test_review_rejects_partial_interval_and_multiple_spaces(self):
        page = Mock()
        dialog = page.get_by_role.return_value.or_.return_value.filter.return_value.last
        submit = dialog.get_by_role.return_value.filter.return_value
        submit.count.return_value = 1
        submit.is_enabled.return_value = True
        form = ParkingForm(page)
        args = (date(2026, 11, 23), ("07:00", "23:00"), False,
                "NEWTON", "Parking Torres Quevedo", SpaceChoice("PTQ-123"), self.form.vehicle)
        dialog.inner_text.return_value = MATCH
        selection, control = form.review(*args)
        self.assertEqual(selection.space, "PTQ-123")
        self.assertIs(control, submit)
        for text in (MATCH.replace("11:00 PM", "2:00 PM"), MATCH + "Space: PTQ-124\n"):
            dialog.inner_text.return_value = text
            with self.assertRaises(BookingProblem):
                form.review(*args)
        dialog.inner_text.return_value = MATCH.replace("11:00 PM", "2:00 PM")
        with self.assertRaises(BookingProblem):
            form.review(args[0], ("", ""), True, *args[3:])


if __name__ == "__main__":
    unittest.main()
