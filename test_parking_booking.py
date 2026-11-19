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
    BookingProblem, ParkingForm, Selection, dates, intervals, read_intent, verify_booking, write_json,
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

    def bookings(self, base_url, *, reload):
        self.bookings_reads.append(reload)
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
        self.assertEqual(self.form.days, [date(2026, 11, day) for day in (20, 21, 22, 23)])
        self.assertEqual(self.form.bookings_reads, [False])
        self.assertEqual(self.form.clicks, 0)
        self.assertFalse(self.form.intent.exists())
        self.assertEqual(result["outcome"], "blocked")
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["date"], "2026-11-23")

    def test_timed_out_click_reloads_and_blocks_duplicate_until_reconciled(self):
        self.form.timeout = True
        self.form.after_submit = [MATCH.replace("11:00 PM", "2:00 PM")]
        self.assertEqual(self.run_command("--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.bookings_reads, [False, True])
        self.assertEqual(self.form.clicks, 1)
        self.assertNotIn(self.form.vehicle, self.form.intent.read_text())
        self.assertEqual(self.run_command("--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.clicks, 1)

        self.form.after_submit = [MATCH, MATCH]
        self.assertEqual(self.run_command("--confirm")["outcome"], "unknown")
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
        self.assertEqual(self.form.bookings_reads, [False, True])
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


if __name__ == "__main__":
    unittest.main()
