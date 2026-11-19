import contextlib
import io
import json
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
    NO_BOOKINGS, BookingProblem, ParkingForm, Selection, dates, intervals, period_covers,
    read_intent, verify_booking, write_json,
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

    def next_date(self, earliest):
        self.days.append(earliest)
        self.day = earliest
        last = RunDate.now(ZoneInfo("Europe/Madrid")).date() + timedelta(days=3)
        return earliest if earliest <= last else None

    def whole_day(self):
        if self.day == date(2026, 11, 21):
            return None  # One displayed shift has ended.
        return ("07:00", "23:00"), False

    def select_space(self, floor, span, spot_name, spot_id):
        assert span == ("07:00", "23:00")
        if self.day == date(2026, 11, 22):
            return None  # No space is available for the requested interval.
        return "PTQ-123"

    def review(self, day, span, all_day, building, floor, space, vehicle):
        assert vehicle == self.vehicle
        self.selection = Selection(day.isoformat(), *span, building, floor, space, all_day)
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
        period.inner_text.side_effect = ["This week", "Next week"]
        option = Mock()
        option.inner_text.return_value = "Next week"
        page.get_by_role.return_value.or_.return_value.filter.return_value.all.return_value = [option]
        page.locator.return_value.filter.return_value.evaluate_all.return_value = []
        with patch("parking_booking.datetime", RunDate), patch.object(form, "check_session"), patch.object(
            form, "label", side_effect=[Mock(**{"count.return_value": 0}), Mock(**{"count.return_value": 1})]
        ):
            page.get_by_role.return_value.filter.return_value.count.return_value = 0
            page.get_by_role.return_value.filter.return_value.first.count.return_value = 0
            self.assertEqual(form.bookings("https://bookker.test/", day, reload=True), [])
        navigation = next(call for call in page.get_by_role.call_args_list if call.args == ("link",))
        self.assertRegex("calendar_today Bookings", navigation.kwargs["name"])
        page.reload.assert_called_once()
        period.click.assert_called_once()
        option.click.assert_called_once()

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

    def test_home_opens_parking_with_the_add_booking_button(self):
        page = Mock()
        form = ParkingForm(page)
        with patch.object(form, "check_session"), patch.object(form, "label"), patch.object(
            form, "location"
        ), patch.object(form, "selected_vehicle", return_value=self.form.vehicle):
            self.assertEqual(form.open("NEWTON", "Parking Torres Quevedo"), self.form.vehicle)
        page.get_by_role.return_value.click.assert_called_once()
        self.assertRegex("Add parking booking", page.get_by_role.call_args.kwargs["name"])

    def test_space_search_continues_past_an_unmarked_row(self):
        page = Mock()
        unknown, available = Mock(), Mock()
        unknown.inner_text.return_value = "Space: PTQ-100"
        available.inner_text.return_value = "Space: PTQ-123\nAvailable"
        available.get_by_role.return_value.filter.return_value.count.return_value = 1
        rows = page.locator.return_value.or_.return_value.or_.return_value.filter.return_value
        rows.all.return_value = [unknown, available]
        page.get_by_role.return_value.filter.return_value.first.count.return_value = 0
        page.get_by_role.return_value.or_.return_value.filter.return_value.first.count.return_value = 0
        form = ParkingForm(page)
        with patch.object(form, "location"), patch.object(form, "label", return_value=Mock(**{"count.return_value": 0})):
            self.assertEqual(form.select_space("Parking Torres Quevedo", ("07:00", "23:00"), None, None), "PTQ-123")
        available.get_by_role.return_value.filter.return_value.click.assert_called_once()
        unknown.click.assert_not_called()

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
                "NEWTON", "Parking Torres Quevedo", "PTQ-123", self.form.vehicle)
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
