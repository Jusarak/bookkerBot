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
from hashlib import sha256
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import run_bookker
from parking_booking import (
    BOOKING_ARTICLE, BOOKING_LIST_ROWS, BOOKING_LOADING, FINAL_ATTEMPT, NO_BOOKINGS, RETRY_ATTEMPT,
    RETRY_FINAL_ATTEMPT, RETRY_PERMISSION, SUPERFICIE_SATURDAY_SHIFTS, TORRES_SHIFTS, BookingProblem,
    ParkingForm, Selection, SpaceChoice, dates, intervals, parking_dates, period_covers, read_intent, verify_booking,
    write_json,
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
        self.events = []
        self.after_book = ("Bookker showed no recognized response within the wait period.", None)
        self.floor = "Parking Torres Quevedo"
        self.space = "PTQ-123"
        self.span = ("07:00", "23:00")
        self.details = None
        self.linked = {}
        self.detail_reads = []

    def check_session(self):
        pass

    def bookings(self, base_url, day, *, reload, include_inactive=False):
        self.bookings_reads.append((day.day, reload))
        self.events.append("reload" if reload else "read")
        return self.after_submit if reload else self.existing

    def open(self, building, floor, vehicle):
        assert (building, floor) == ("NEWTON", self.floor)
        assert vehicle in (None, self.vehicle)
        return self.vehicle

    def location(self, label, value, *, select):
        assert not select
        assert (label, value) in (("Building", "NEWTON"), ("Floor", self.floor))

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

    def whole_day(self, expected_shifts, *, verify_all=False):
        assert verify_all == (self.floor == "Parking Superficie C")
        if verify_all:
            assert expected_shifts == (SUPERFICIE_SATURDAY_SHIFTS if self.day.day == 21 else ())
        else:
            assert expected_shifts == (TORRES_SHIFTS if self.day.day == 26 else ())
        if not verify_all and self.day == date(2026, 11, 21):
            return None  # One displayed shift has ended.
        return self.span, False

    def select_space(self, building, floor, span, spot_name, spot_id, *, require_material):
        assert span == self.span
        assert require_material == (self.day.day == 26 or self.floor == "Parking Superficie C")
        if self.day == date(2026, 11, 22):
            return None  # No space is available for the requested interval.
        return SpaceChoice(self.space, material=require_material)

    def booking_details(self, base_url, day, record, space):
        assert record in self.after_submit and space == self.space
        self.events.append("details")
        self.detail_reads.append(record)
        details = self.linked.get(record, self.details)
        if details is None:
            raise BookingProblem("The matching Bookings row did not expose one booking details link.")
        return details

    def review(self, day, span, all_day, building, floor, choice, vehicle):
        assert vehicle == self.vehicle
        self.selection = Selection(day.isoformat(), *span, building, floor, choice.name, all_day)
        return self.selection, self

    def submission_text(self):
        return self.vehicle

    def final_prompt(self):
        return None

    def wait_for_submission(self, before, *, watch_final):
        self.events.append("observe")
        assert before == self.vehicle
        return self.after_book if watch_final else (self.after_book[0], None)

    def click(self, *, timeout):
        self.clicks += 1
        self.events.append("book")
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
EXACT_ARGS = ("--on-date", "2026-11-26")
EXACT_MATCH = MATCH.replace("November 23", "November 26")


def locator(count=1, text=""):
    element = Mock(**{"count.return_value": count, "inner_text.return_value": text,
                      "is_enabled.return_value": True, "get_attribute.return_value": None})
    element.filter.return_value = element
    element.or_.return_value = element
    element.and_.return_value = element
    return element


class CancellationFixture:
    day_first = False

    def __init__(self):
        self.target = EXACT_MATCH.strip()
        self.neighbor = self.target.replace("PTQ-123", "PTQ-124").replace("OFFLINE-123", "OFFLINE-124")
        self.records = [self.target, self.neighbor]
        self.reads = []
        self.linked = {}
        self.detail_reads = []
        self.titles = {}
        self.page = Mock()
        self.row = locator(text=self.target)
        self.trigger = locator()
        self.trigger.get_attribute.return_value = "row-menu"
        self.row.get_by_role.return_value = self.trigger
        self.menu = locator(0)
        self.menu.get_attribute.return_value = "row-menu"
        self.dialog = locator(0)
        self.dialog.wait_for.side_effect = TimeoutError()
        self.page.get_by_role.side_effect = {"menu": self.menu, "dialog": self.dialog}.__getitem__
        self.only_this = locator(0)
        self.radio = locator(0)
        self.delete = locator()
        self.final_delete = locator()
        self.menu.get_by_role.side_effect = lambda role, name=None: self.delete if name else self.only_this
        self.dialog.get_by_role.side_effect = lambda role, name: (
            self.radio if role == "radio" else self.final_delete if name.fullmatch("Delete") else self.only_this
        )
        self.trigger.click.side_effect = lambda **kwargs: setattr(self.menu.count, "return_value", 1)
        self.delete.click.side_effect = self.remove
        self.final_delete.click.side_effect = self.remove

    def bookings(self, base_url, day, *, reload):
        assert reload
        self.reads.append(day)
        return self.records.copy()

    def booking_row(self, base_url, day, record):
        assert record in self.records
        row = self.row if record == self.target else locator(text=record)
        row.locator.return_value = locator()
        row.locator.return_value.all_inner_texts.return_value = self.titles.get(record, [])
        return row

    def booking_details(self, base_url, day, record, space):
        self.detail_reads.append(record)
        if record not in self.linked:
            raise BookingProblem("The matching Bookings row did not expose one booking details link.")
        return self.linked[record]

    def settled(self):
        pass

    def remove(self, **kwargs):
        self.records.remove(self.target)
        self.menu.count.return_value = 0

    def prompt(self, text):
        def open_dialog(**kwargs):
            self.menu.count.return_value = 0
            self.dialog.count.return_value = 1
            self.dialog.inner_text.return_value = text
            self.dialog.wait_for.side_effect = None
        self.delete.click.side_effect = open_dialog


class CancellationCommandTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(work.cleanup)
        self.state = Path(work.name) / "cancel.json"
        self.form = CancellationFixture()
        self.selector = ("--on-date", "2026-11-26", "--building", "NEWTON",
                         "--floor", "Parking Torres Quevedo", "--spot-name", "PTQ-123")
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def run_command(self, *args):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["run_bookker.py", "cancel", *args]),
            patch("parking_cancellation.ParkingForm", return_value=self.form),
            patch.object(run_bookker.BookkerClient, "open_with_session", autospec=True) as launch,
            contextlib.redirect_stdout(self.stdout),
            contextlib.redirect_stderr(self.stderr),
        ):
            run_bookker.main()
        launch.assert_called_once()
        self.assertEqual(launch.call_args.kwargs, {"timezone_id": "Europe/Madrid"})
        self.assertNotIn("private fixture vehicle", self.stdout.getvalue() + self.stderr.getvalue())
        return json.loads(self.stdout.getvalue())

    def test_preview_and_confirm_single_occurrence_leave_neighbor(self):
        self.form.target = self.form.records[0] = self.form.target + "\nRecurring series"
        self.form.row.inner_text.return_value = self.form.target
        result = self.run_command(*self.selector)
        self.assertEqual(result["outcome"], "preview")
        self.assertEqual(result["confirmation_reference"], "OFFLINE-123")
        self.form.trigger.click.assert_not_called()
        self.assertFalse(self.state.exists())
        self.form.prompt("Delete booking?\nRecurring series\nOnly this occurrence\nDelete")
        self.form.delete.get_attribute.return_value = "dialog"
        self.form.radio.count.return_value = 1
        self.form.radio.is_checked.side_effect = [False, True]

        def remove(**kwargs):
            self.assertEqual(json.loads(self.stderr.getvalue())["space"], "PTQ-123")
            self.assertEqual(json.loads(self.state.read_text())["reservation"]["space"], "PTQ-123")
            self.form.remove()

        self.form.final_delete.click.side_effect = remove
        self.assertEqual(self.run_command(*self.selector, "--confirm", "--submission-state", str(self.state))[
            "outcome"], "cancelled")
        self.assertEqual(self.form.records, [self.form.neighbor])
        self.assertEqual(self.form.reads, [date(2026, 11, 26)] * 3)
        self.form.radio.check.assert_called_once()
        self.form.final_delete.click.assert_called_once()

    def test_recurring_row_cannot_use_generic_dialog_confirmation(self):
        self.form.target = self.form.records[0] = self.form.target + "\nRecurring series"
        self.form.row.inner_text.return_value = self.form.target
        self.form.delete.get_attribute.return_value = "dialog"
        self.form.prompt("Delete booking?\nDelete")
        args = (*self.selector, "--confirm", "--submission-state", str(self.state))
        result = self.run_command(*args)
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("one occurrence", result["reason"])
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.assertEqual(self.form.records, [self.form.target, self.form.neighbor])
        self.form.delete.click.assert_called_once()
        self.form.final_delete.click.assert_not_called()

    def test_sparse_row_resolves_for_preview_and_cancellation_readback(self):
        sparse = "November 26, 2026\nSpace: PTQ-123\nReference: OFFLINE-123"
        linked = "Building\nNEWTON, Parking Torres Quevedo\n07:00–23:00"
        self.form.records[0] = self.form.target = sparse
        self.form.row.inner_text.return_value = sparse
        self.form.linked[sparse] = linked
        for selector in (self.selector, ("--reference", "OFFLINE-123")):
            with self.subTest(selector=selector):
                result = self.run_command(*selector)
                self.assertEqual(result["outcome"], "preview")
                self.assertEqual((result["date"], result["building"], result["floor"], result["space"]),
                                 ("2026-11-26", "NEWTON", "Parking Torres Quevedo", "PTQ-123"))
        self.form.trigger.click.assert_not_called()
        # After the interrupted action, the same reservation can still be
        # present in a card that no longer displays the reference.
        readback = "November 26, 2026\nPTQ-123"
        self.form.titles[readback] = ["PTQ-123"]
        self.form.linked[readback] = linked
        self.form.delete.click.side_effect = lambda **kwargs: self.form.records.__setitem__(0, readback)
        args = ("--reference", "OFFLINE-123", "--confirm", "--submission-state", str(self.state))
        for _ in range(2):
            self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.assertEqual(self.form.detail_reads, [sparse] * 3 + [readback] * 4)
        self.form.delete.click.assert_called_once()
        self.assertEqual(self.form.records, [readback, self.form.neighbor])

        duplicate = readback + "\nStatus: Confirmed"
        self.form.records.append(duplicate)
        self.form.titles[duplicate] = ["PTQ-123"]
        self.form.linked[duplicate] = linked
        self.assertIn("More than one", self.run_command(*args)["reason"])
        self.form.delete.click.assert_called_once()

    def test_reference_only_in_linked_details(self):
        row = "November 26, 2026\nSpace: PTQ-123\nNEWTON, Parking Torres Quevedo"
        self.form.records[0] = self.form.target = row
        self.form.linked[row] = "Booking reference: OFFLINE-123"
        self.assertEqual(self.run_command("--reference", "OFFLINE-123")["outcome"], "preview")
        self.assertEqual(self.form.detail_reads, [row])
        self.form.trigger.click.assert_not_called()

    def test_linked_reference_checks_card_hours_before_cancelling(self):
        row = ("November 26, 2026\nPTQ-123\n07:00–14:00\n"
               "NEWTON, Parking Torres Quevedo")
        self.form.records[0] = self.form.target = row
        self.form.row.inner_text.return_value = row
        details = "Space: PTQ-123\nReference: OFFLINE-123\n"
        args = ("--reference", "OFFLINE-123", "--confirm", "--submission-state", str(self.state))
        self.form.linked[row] = details + "14:00–23:00"
        result = self.run_command(*args)
        self.assertEqual(result["outcome"], "blocked")
        self.assertIn("different hours", result["reason"])
        self.assertEqual(self.form.records, [row, self.form.neighbor])
        self.assertFalse(self.state.exists())
        self.form.trigger.click.assert_not_called()
        self.form.delete.click.assert_not_called()

        self.form.linked[row] = details + "07:00–10:00\n10:00–14:00"
        self.assertEqual(self.run_command("--reference", "OFFLINE-123")["outcome"], "preview")
        self.form.linked[row] = details + "7:00 AM–2:00 PM"
        self.assertEqual(self.run_command(*args)["outcome"], "cancelled")
        self.assertEqual(self.run_command(*args)["outcome"], "cancelled")
        self.assertEqual(self.form.records, [self.form.neighbor])
        self.form.delete.click.assert_called_once()

    def test_sparse_resource_title_must_agree_with_linked_space(self):
        sparse = "November 26, 2026\nPTQ-124"
        self.form.records = [sparse]
        self.form.target = sparse
        self.form.row.inner_text.return_value = sparse
        self.form.titles[sparse] = ["PTQ-124"]
        linked = "NEWTON, Parking Torres Quevedo\nReference: OFFLINE-123"
        args = ("--reference", "OFFLINE-123")
        for layout_index, layout in enumerate((f"Space: {{space}}\n{linked}", f"{{space}}\n{linked}")):
            self.form.linked[sparse] = layout.format(space="PTQ-124")
            self.assertEqual(self.run_command(*args)["space"], "PTQ-124")
            self.form.linked[sparse] = layout.format(space="PTQ-123")
            for selector_index, selector in enumerate((args, self.selector)):
                with self.subTest(layout=layout, selector=selector):
                    self.form.records = [sparse]
                    state = self.state.with_name(f"cancel-{layout_index}-{selector_index}.json")
                    result = self.run_command(*selector, "--confirm", "--submission-state", str(state))
                    self.assertEqual(result["outcome"], "blocked")
                    self.assertIn("conflicting spaces", result["reason"])
                    self.assertFalse(state.exists())
        self.form.trigger.click.assert_not_called()

    def test_linked_reference_requires_a_verified_sparse_card_title(self):
        for index, title in enumerate(("PTQ-124\n", "")):
            row = f"November 26, 2026\n{title}View details"
            with self.subTest(title=title):
                state = self.state.with_name(f"cancel-{index}.json")
                self.form.target = row
                self.form.row.inner_text.return_value = row
                self.form.records = [row]
                self.form.linked[row] = "PTQ-123\nNEWTON, Parking Torres Quevedo\nReference: OFFLINE-123"
                args = ("--reference", "OFFLINE-123", "--confirm", "--submission-state", str(state))
                self.assertEqual(self.run_command(*args)["outcome"], "blocked")
                self.assertFalse(state.exists())
        self.form.trigger.click.assert_not_called()

        row = "November 26, 2026\nPTQ-123\nView details"
        self.form.records = [row]
        self.form.linked[row] = "PTQ-123\nNEWTON, Parking Torres Quevedo\nReference: OFFLINE-123"
        self.assertEqual(self.run_command("--reference", "OFFLINE-123")["outcome"], "preview")

    def test_reference_lookup_and_readback_allow_readable_reference_free_neighbor(self):
        neighbor = self.form.neighbor.replace("\nConfirmation reference: OFFLINE-124", "")
        self.form.records[1] = neighbor
        self.form.linked[neighbor] = "Space: PTQ-124\nNEWTON, Parking Torres Quevedo"
        args = ("--reference", "OFFLINE-123")
        self.assertEqual(self.run_command(*args)["outcome"], "preview")
        args += ("--confirm", "--submission-state", str(self.state))
        self.assertEqual(self.run_command(*args)["outcome"], "cancelled")
        self.assertEqual(self.run_command(*args)["outcome"], "cancelled")

        self.form.linked[neighbor] = ""
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.form.linked.clear()
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.assertEqual(self.form.records, [neighbor])
        self.form.delete.click.assert_called_once()

    def test_missing_space_on_saved_cancellation_is_resolved_before_reporting_absence(self):
        row = "November 26, 2026\nNEWTON, Parking Torres Quevedo"
        self.form.linked[row] = "Space: PTQ-123"
        self.form.delete.click.side_effect = lambda **kw: self.form.records.__setitem__(0, row)
        args = (*self.selector, "--confirm", "--submission-state", str(self.state))
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.form.linked.clear()
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.form.delete.click.assert_called_once()

    def test_cancellation_readback_keeps_conflicting_title_and_label_unresolved(self):
        target = self.form.target.replace("\nConfirmation reference: OFFLINE-123", "")
        self.form.records[0] = self.form.target = target
        self.form.row.inner_text.return_value = target
        readback = "PTQ-123\n" + target.replace("Space: PTQ-123", "Space: PTQ-125")
        self.form.delete.click.side_effect = lambda **kw: self.form.records.__setitem__(0, readback)
        args = (*self.selector, "--confirm", "--submission-state", str(self.state))
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.run_command(*args)
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn("conflicting spaces", result["reason"])
                self.assertEqual(self.form.records, [readback, self.form.neighbor])
        self.form.trigger.click.assert_called_once()
        self.form.delete.click.assert_called_once()

    def test_sparse_location_match_still_checks_linked_ambiguity(self):
        sparse = "November 26, 2026\nPTQ-123"
        self.form.records.append(sparse)
        self.form.linked[sparse] = "NEWTON, Parking Superficie C"
        self.assertEqual(self.run_command(*self.selector)["outcome"], "preview")
        self.form.linked[sparse] = "NEWTON, Parking Torres Quevedo"
        self.assertIn("More than one", self.run_command(*self.selector)["reason"])
        self.form.linked[sparse] += "\nSpace: PTQ-124"
        self.assertIn("conflicting spaces", self.run_command(*self.selector)["reason"])
        self.form.trigger.click.assert_not_called()

    def test_elapsed_intent_cannot_use_absence_in_upcoming_bookings(self):
        page = Mock(url="https://bookker.test/#/bookings")
        view = page.locator.return_value.filter.return_value
        view.count.return_value = 1
        view.get_by_role.return_value.filter.return_value.count.return_value = 0
        upcoming = locator()
        form = ParkingForm(page)
        reads = self.form.bookings

        def bookings(base_url, day, *, reload):
            if self.form.target in self.form.records:
                return reads(base_url, day, reload=reload)
            return form.bookings(base_url, day, reload=reload)

        args = (*self.selector, "--confirm", "--submission-state", str(self.state))
        with (patch.object(self.form, "bookings", side_effect=bookings),
              patch.object(form, "label", return_value=upcoming),
              patch.object(form, "check_session"),
              patch("parking_booking.datetime") as clock):
            clock.now.return_value = datetime(2026, 11, 27, tzinfo=ZoneInfo("Europe/Madrid"))
            for _ in range(2):
                result = self.run_command(*args)
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn("period could not be verified", result["reason"])
        self.form.delete.click.assert_called_once()
        upcoming.click.assert_not_called()
        self.assertEqual(self.form.records, [self.form.neighbor])

    def test_cancel_by_reference_reconciles_interruption_without_second_delete(self):
        args = ("--reference", "OFFLINE-123", "--confirm", "--submission-state", str(self.state))

        def interrupt(**kwargs):
            self.assertTrue(self.state.is_file())
            self.form.remove()
            raise KeyboardInterrupt

        self.form.delete.click.side_effect = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_command(*args)
        self.assertEqual(self.run_command(*args)["outcome"], "cancelled")
        self.assertEqual(self.form.records, [self.form.neighbor])
        self.assertEqual(self.form.reads, [None, date(2026, 11, 26)])
        self.form.delete.click.assert_called_once()

    def test_cancel_by_location_checks_saved_reference_on_readback_and_retry(self):
        args = (*self.selector, "--confirm", "--submission-state", str(self.state))
        changed = self.form.target.replace("PTQ-123", "PTQ-125")
        self.form.delete.click.side_effect = lambda **kwargs: self.form.records.__setitem__(0, changed)
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.assertEqual(json.loads(self.state.read_text())["reservation"]["confirmation_reference"], "OFFLINE-123")
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")

        sparse = changed.replace("\nConfirmation reference: OFFLINE-123", "")
        self.form.records[0] = sparse
        self.form.linked[sparse] = "Space: PTQ-125\nReference: OFFLINE-123"
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")

        self.form.records.remove(sparse)
        self.assertEqual(self.run_command(*args)["outcome"], "cancelled")
        self.form.delete.click.assert_called_once()
        self.assertEqual(self.form.records, [self.form.neighbor])

    def test_cancel_readback_preserves_saved_reference_with_changed_date(self):
        for index, selector in enumerate((self.selector, ("--reference", "OFFLINE-123"))):
            with self.subTest(selector=selector):
                self.form = CancellationFixture()
                state = self.state.with_name(f"cancel-{index}.json")
                args = (*selector, "--confirm", "--submission-state", str(state))
                changed = self.form.target.replace("November 26", "November 27")
                self.form.delete.click.side_effect = lambda **kwargs: self.form.records.__setitem__(0, changed)
                for _ in range(2):
                    result = self.run_command(*args)
                    self.assertEqual(result["outcome"], "unknown")
                    self.assertIn("different date", result["reason"])
                    self.assertEqual(self.form.records, [changed, self.form.neighbor])
                unrelated = changed.replace("OFFLINE-123", "OFFLINE-456")
                self.form.records[0] = unrelated
                self.assertEqual(self.run_command(*args)["outcome"], "cancelled")
                self.assertEqual(self.form.records, [unrelated, self.form.neighbor])
                self.form.trigger.click.assert_called_once()
                self.form.delete.click.assert_called_once()

    def test_cancel_readback_checks_linked_reference_on_changed_date(self):
        args = (*self.selector, "--confirm", "--submission-state", str(self.state))
        changed = self.form.target.replace("November 26", "November 27")
        card = changed.replace("\nConfirmation reference: OFFLINE-123", "")
        self.form.linked[card] = changed
        self.form.delete.click.side_effect = lambda **kwargs: self.form.records.__setitem__(0, card)
        for _ in range(2):
            result = self.run_command(*args)
            self.assertEqual(result["outcome"], "unknown")
            self.assertIn("different date", result["reason"])
        self.assertEqual(self.form.detail_reads, [card, card])

        self.form.linked[card] = card
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.form.linked[card] = changed.replace("OFFLINE-123", "OFFLINE-456")
        self.assertEqual(self.run_command(*args)["outcome"], "cancelled")
        self.assertEqual(self.form.records, [card, self.form.neighbor])
        self.form.trigger.click.assert_called_once()
        self.form.delete.click.assert_called_once()

    def test_wrong_ambiguous_and_absent_selectors_do_not_open_a_menu(self):
        confirm = ("--confirm", "--submission-state", str(self.state))
        for selector in (
            ("--reference", "WRONG-123"),
            self.selector[:-1] + ("PTQ-999",),
            self.selector[:5] + ("Parking Superficie C",) + self.selector[6:],
        ):
            with self.subTest(selector=selector):
                self.assertEqual(self.run_command(*selector, *confirm)["outcome"], "absent")
        self.form.records.append(self.form.target.replace("OFFLINE-123", "OFFLINE-456"))
        self.assertEqual(self.run_command(*self.selector, *confirm)["outcome"], "blocked")
        self.form.records = [self.form.neighbor]
        self.assertEqual(self.run_command(*self.selector, *confirm)["outcome"], "absent")
        self.assertFalse(self.state.exists())
        self.form.trigger.click.assert_not_called()

    def test_conflicting_confirmation_and_rerun_never_delete_a_replacement(self):
        args = (*self.selector, "--confirm", "--submission-state", str(self.state))
        self.form.prompt("Delete this booking?\nNovember 27, 2026\nSpace: PTQ-124")
        result = self.run_command(*args)
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("different reservation", result["reason"])
        self.assertEqual(self.form.records, [self.form.target, self.form.neighbor])
        self.form.final_delete.click.assert_not_called()

        self.form.records[0] = self.form.target.replace("OFFLINE-123", "NEW-123")
        self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.form.delete.click.assert_called_once()
        self.form.final_delete.click.assert_not_called()

    def test_conflicting_row_location_blocks_cancellation_before_menu(self):
        conflicts = (
            "schedule 07:00–23:00location_on NEWTON, Parking Superficie C",
            "NEWTON, Parking Superficie C",
            "NEWTON (Parking Superficie C)",
            "Parking Torres Quevedo (OTHER BUILDING)",
            "schedule 07:00–23:00 NEWTON (Parking Superficie C)",
            "Schedule: 07:00–23:00 NEWTON (Parking Superficie C)",
        )
        for layout_index, location in enumerate(conflicts):
            for selector_index, selector in enumerate((self.selector, ("--reference", "OFFLINE-123"))):
                with self.subTest(location=location, selector=selector):
                    self.form = CancellationFixture()
                    row = f"{self.form.target}\n{location}"
                    self.form.records[0] = self.form.target = row
                    self.form.row.inner_text.return_value = row
                    state = self.state.with_name(f"cancel-{layout_index}-{selector_index}.json")
                    result = self.run_command(*selector, "--confirm", "--submission-state", str(state))
                    self.assertEqual(result["outcome"], "blocked")
                    self.assertIn("conflicting or incomplete details", result["reason"])
                    self.assertEqual(self.form.records, [row, self.form.neighbor])
                    self.assertFalse(state.exists())
                    self.form.trigger.click.assert_not_called()

    def test_parenthesized_row_location_resolves_for_reference_selector(self):
        for prefix in ("", "schedule 07:00–23:00 ", "Schedule: 07:00–23:00 "):
            with self.subTest(prefix=prefix):
                row = f"November 26, 2026\nPTQ-123\n{prefix}NEWTON (Parking Torres Quevedo)\nReference: OFFLINE-123"
                self.form.records[0] = self.form.target = row
                result = self.run_command("--reference", "OFFLINE-123")
                self.assertEqual(result["outcome"], "preview")
                self.assertEqual((result["building"], result["floor"]), ("NEWTON", "Parking Torres Quevedo"))
        self.form.trigger.click.assert_not_called()

    def test_confirmation_checks_schedule_and_location_text(self):
        dialog_cases = (
            ("schedule 07:00–23:00location_on NEWTON, Parking Superficie C", "unknown"),
            ("schedule 07:00–23:00location_on OTHER BUILDING, Parking Torres Quevedo", "unknown"),
            ("schedule 07:00–23:00location_on NEWTON, Parking Superficie C\n"
             "Building: NEWTON\nFloor: Parking Torres Quevedo", "unknown"),
            ("schedule 07:00–23:00location_on NEWTON, Parking Torres Quevedo", "cancelled"),
            ("schedule 07:00–23:00 NEWTON (Parking Superficie C)\n"
             "Building: NEWTON\nFloor: Parking Torres Quevedo", "unknown"),
            ("Schedule: 07:00–23:00 NEWTON (Parking Superficie C)\n"
             "Building: NEWTON\nFloor: Parking Torres Quevedo", "unknown"),
            ("Schedule: 07:00–23:00 NEWTON (Parking Torres Quevedo)", "cancelled"),
            ("schedule 07:00–23:00 Parking Torres Quevedo (OTHER BUILDING)", "unknown"),
            ("schedule 07:00–23:00 NEWTON (Parking Torres Quevedo)", "cancelled"),
            ("location_on NEWTON, Parking Superficie C", "unknown"),
            ("location_on\nNEWTON, Parking Superficie C", "unknown"),
            ("location_on NEWTON, Parking Torres Quevedo", "cancelled"),
            ("NEWTON, Parking Superficie C", "unknown"),
            ("NEWTON, Parking Superficie C\nBuilding: NEWTON\nFloor: Parking Torres Quevedo", "unknown"),
            ("NEWTON, Parking Torres Quevedo", "cancelled"),
            ("NEWTON (Parking Superficie C)\nBuilding: NEWTON\nFloor: Parking Torres Quevedo", "unknown"),
            ("Parking Torres Quevedo (OTHER BUILDING)", "unknown"),
            ("NEWTON (Parking Torres Quevedo)", "cancelled"),
            ("PTQ-124", "unknown"),
            ("PTQ-124\nSpace: PTQ-123", "unknown"),
            ("PTQ-123\nSpace: PTQ-123", "cancelled"),
        )
        for index, (details, expected) in enumerate(dialog_cases):
            with self.subTest(details=details):
                self.form = CancellationFixture()
                self.form.prompt(f"Delete this booking?\n{details}\nDelete")
                result = self.run_command(*self.selector, "--confirm", "--submission-state",
                                          str(self.state.with_name(f"cancel-{index}.json")))
                self.assertEqual(result["outcome"], expected)
                if expected == "unknown":
                    self.assertIn("different reservation", result["reason"])
                    self.form.final_delete.click.assert_not_called()
                    self.assertEqual(self.form.records, [self.form.target, self.form.neighbor])
                else:
                    self.form.final_delete.click.assert_called_once()
                    self.assertEqual(self.form.records, [self.form.neighbor])


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

    def superficie_args(self, day="2026-11-21"):
        self.form.floor = "Parking Superficie C"
        self.form.space = "PSC-17"
        return "--floor", self.form.floor, "--on-date", day

    def test_superficie_saturday_checks_bookings_first_and_reconciles_without_replay(self):
        args = self.superficie_args()
        dry_run = self.run_command(*args)
        self.assertTrue(dry_run["dry_run"])
        self.assertEqual(dry_run["requested_shifts"], [["07:00", "14:00"], ["14:00", "23:00"]])
        self.assertEqual(dry_run["space"], "PSC-17")
        self.assertEqual(self.form.events, ["read"])
        self.assertFalse(self.form.intent.exists())

        self.form.timeout = True
        first = self.run_command(*args, "--confirm")
        self.assertEqual(first["outcome"], "unknown")
        self.assertEqual(first["space"], "PSC-17")
        original_intent = self.form.intent.read_bytes()
        intent = read_intent(self.form.intent)
        self.assertEqual((intent.date, intent.floor, intent.start_time, intent.end_time),
                         ("2026-11-21", self.form.floor, "07:00", "23:00"))
        write_json(self.form.intent.with_suffix(RETRY_PERMISSION), {
            "version": 1, "action": "retry-identical-selection-once",
            "original_intent_sha256": sha256(original_intent).hexdigest(),
            "at": "offline fixture", "basis": "offline fixture",
        })
        self.assertEqual(self.run_command(*args, "--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.clicks, 1)
        self.form.after_submit = ["November 21, 2026\nPSC-17"]
        self.form.details = "PSC-17\nBuilding\nNEWTON, Parking Superficie C\n07:00–14:00\n14:00–23:00"
        recovered = self.run_command(*args, "--confirm")
        self.assertEqual(recovered["outcome"], "confirmed")
        self.assertTrue(recovered["reconciled"])
        self.assertNotIn("confirmation_reference", recovered)
        self.assertEqual(self.form.events[-2:], ["reload", "details"])
        self.assertEqual(self.form.clicks, 1)
        self.assertEqual(self.form.intent.read_bytes(), original_intent)

    def test_superficie_other_dates_use_displayed_shifts(self):
        args = self.superficie_args("2026-11-24")
        self.form.span = ("08:00", "21:00")
        result = self.run_command(*args)
        self.assertTrue(result["dry_run"])
        self.assertEqual((result["date"], result["start_time"], result["end_time"]),
                         ("2026-11-24", "08:00", "21:00"))
        self.assertNotIn("requested_shifts", result)

    def test_superficie_statusless_card_needs_linked_details_and_no_conflict(self):
        self.superficie_args()
        selection = Selection("2026-11-21", "07:00", "23:00", "NEWTON", self.form.floor, self.form.space)
        card = "November 21, 2026\nPSC-17\n07:00–23:00\nNEWTON, Parking Superficie C"
        self.form.after_submit = [card]
        for details in (
            None,
            card.replace("PSC-17", "PSC-18"),
            card.replace("23:00", "22:00"),
            card.replace("November 21", "November 22"),
            card.replace("Parking Superficie C", "Parking Torres Quevedo"),
            card + "\nStatus: Pending approval",
            card + "\nStatus: Failed",
        ):
            with self.subTest(details=details):
                self.form.details = details
                result = verify_booking([card], selection, self.form, recovered=True, base_url="about:blank")
                self.assertEqual(result["outcome"], "unknown")
        for records in ([card + "\nPending confirmation"], [card, card + "\nStatus: Rejected"]):
            with self.subTest(records=records):
                result = verify_booking(records, selection, self.form, recovered=True, base_url="about:blank")
                self.assertEqual(result["outcome"], "unknown")
        refusal = "Bookker reported a booking date or booking window restriction."
        result = verify_booking([], selection, self.form, recovered=False, base_url="about:blank",
                                observation={"book": refusal})
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["reason"], refusal)

    def test_superficie_cli_corroborates_confirmed_card_with_linked_details(self):
        args = self.superficie_args()
        card = ("November 21, 2026\nPSC-17\n07:00–23:00\n"
                "NEWTON, Parking Superficie C\nStatus: Confirmed\nReference: OFFLINE-C17")
        self.form.after_submit = [card]
        self.form.details = card.replace("Confirmed", "Failed")
        self.assertEqual(self.run_command(*args, "--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.events[-2:], ["reload", "details"])
        self.form.details = None
        self.assertEqual(self.run_command(*args, "--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.events[-2:], ["reload", "details"])
        self.form.details = card.replace("OFFLINE-C17", "OFFLINE-C18")
        self.assertEqual(self.run_command(*args, "--confirm")["outcome"], "unknown")
        self.form.details = card
        self.assertEqual(self.run_command(*args, "--confirm")["outcome"], "confirmed")
        self.assertEqual(self.form.clicks, 1)

    def test_superficie_cli_checks_other_rows_with_corroborated_reference(self):
        args = self.superficie_args() + ("--confirm",)
        card = "November 21, 2026\nPSC-17\n07:00–23:00\nNEWTON, Parking Superficie C"
        reference = "\nReference: OFFLINE-C17"
        self.form.details = card + reference
        conflicts = (
            card.replace("Parking Superficie C", "Parking Torres Quevedo") + "\nStatus: Failed",
            card.replace("Parking Superficie C", "Parking Torres Quevedo"),
            card.replace("November 21", "November 22"),
            card.replace("PSC-17", "PSC-18"),
            "07:00–14:00",
            "Status: Failed",
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                self.form.after_submit = [card, conflict + reference]
                result = self.run_command(*args)
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn("same reference", result["reason"])
        self.form.after_submit = [card, conflicts[0] + "\nReference: OFFLINE-C18"]
        result = self.run_command(*args)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(result["confirmation_reference"], "OFFLINE-C17")
        self.assertEqual(self.form.clicks, 1)

    def test_superficie_cli_checks_linked_reference_on_conflicting_card(self):
        args = self.superficie_args() + ("--confirm",)
        card = "November 21, 2026\nPSC-17\n07:00–23:00\nNEWTON, Parking Superficie C"
        conflict = card.replace("Parking Superficie C", "Parking Torres Quevedo") + "\nStatus: Failed"
        reference = "\nReference: OFFLINE-C17"
        self.form.after_submit = [card, conflict]
        self.form.linked = {card: card + reference, conflict: conflict + reference}
        for _ in range(2):
            result = self.run_command(*args)
            self.assertEqual(result["outcome"], "unknown")
            self.assertIn("same reference", result["reason"])
        self.assertEqual(self.form.detail_reads, [card, conflict, card, conflict])

        for details in (None, conflict):
            with self.subTest(details=details):
                self.form.linked[conflict] = details
                self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.form.linked[conflict] = conflict + "\nReference: OFFLINE-C18"
        result = self.run_command(*args)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(result["confirmation_reference"], "OFFLINE-C17")
        self.assertEqual(self.form.clicks, 1)

    def test_superficie_cli_requires_consistent_location_on_card_and_linked_details(self):
        args = self.superficie_args() + ("--confirm",)
        card = ("November 21, 2026\nPSC-17\nBuilding: NEWTON\nFloor: Parking Superficie C\n"
                "schedule 07:00–23:00location_on NEWTON, Parking Superficie C")
        conflicting = card.replace("location_on NEWTON, Parking Superficie C",
                                   "location_on NEWTON, Parking Torres Quevedo")
        conflicting_tail = card + "\nschedule 07:00–23:00location_on OTHER, Parking Superficie C"
        standalone = card + "\nNEWTON, Parking Torres Quevedo"
        parenthesized = card + "\nNEWTON (Parking Torres Quevedo)"
        reverse_pair = card + "\nParking Superficie C (OTHER BUILDING)"
        scheduled_pair = card + "\nschedule 07:00–23:00 NEWTON (Parking Torres Quevedo)"
        labeled_pair = card + "\nSchedule: 07:00–23:00 NEWTON (Parking Torres Quevedo)"
        for record, details in ((card, conflicting), (conflicting, card), (card, conflicting_tail),
                                (card, standalone), (standalone, card), (card, parenthesized),
                                (parenthesized, card), (card, reverse_pair), (reverse_pair, card),
                                (card, scheduled_pair), (scheduled_pair, card),
                                (card, labeled_pair), (labeled_pair, card)):
            with self.subTest(record=record, details=details):
                self.form.after_submit = [record]
                self.form.details = details
                self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        refusal = "Bookker reported a booking date or booking window restriction."
        selection = read_intent(self.form.intent)
        result = verify_booking([parenthesized], selection, self.form, recovered=False, base_url="about:blank",
                                observation={"book": refusal})
        self.assertEqual(result["outcome"], "unknown")
        self.form.after_submit = [card + "\nNEWTON (Parking Superficie C)"]
        self.form.details = "PSC-17\nSchedule: 07:00–23:00 Parking Superficie C (NEWTON)"
        self.assertEqual(self.run_command(*args)["outcome"], "confirmed")
        self.assertEqual(self.form.clicks, 1)

    def test_superficie_cli_checks_all_readback_space_names(self):
        args = self.superficie_args() + ("--confirm",)
        card = "November 21, 2026\nPSC-17\n07:00–23:00\nNEWTON, Parking Superficie C\nSpace: PSC-17"
        conflicting = card.replace("\nPSC-17\n", "\nPSC-18\n")
        for record, details in ((card, conflicting), (conflicting, card), (card, card + "\nPSC-18")):
            with self.subTest(record=record, details=details):
                self.form.after_submit = [record]
                self.form.details = details
                self.assertEqual(self.run_command(*args)["outcome"], "unknown")
        self.form.after_submit = [card]
        self.form.details = card
        self.assertEqual(self.run_command(*args)["outcome"], "confirmed")
        self.assertEqual(self.form.clicks, 1)

    def test_superficie_cli_checks_title_and_labeled_space_before_final_click(self):
        page = Mock()
        page.locator.return_value.get_attribute.return_value = "en"
        prompt_form = ParkingForm(page)
        args = self.superficie_args() + ("--confirm",)
        booking_details = "November 21, 2026\n07:00–23:00\nNEWTON, Parking Superficie C"
        card = f"PSC-17\n{booking_details}"
        cases = (("PSC-18", "PSC-17", "unknown"), ("PSC-17", "PSC-18", "unknown"),
                 ("PSC-17", "PSC-17", "confirmed"))
        for index, (title, label, expected) in enumerate(cases):
            with self.subTest(title=title, label=label):
                self.form.intent = self.form.intent.with_name(f"intent-{index}.json")
                self.form.after_submit = []
                self.form.details = card
                prompt = (f"Please confirm your booking\n{title}\nSpace: {label}\n"
                          f"{booking_details}\nRepetir\nNunca\nConfirm booking")
                self.form.after_book = ("Bookker displayed a separate final confirmation step.", prompt)
                action = Mock()

                def confirm(**kwargs):
                    self.form.after_submit = [card]

                action.click.side_effect = confirm
                with (patch.object(prompt_form, "final_prompt", return_value=(action, prompt)),
                      patch.object(self.form, "verify_final_prompt", prompt_form.verify_final_prompt, create=True)):
                    result = self.run_command(*args, "--submission-state", str(self.form.intent))
                self.assertEqual(result["outcome"], expected)
                if expected == "unknown":
                    action.click.assert_not_called()
                    self.assertFalse(self.form.intent.with_suffix(FINAL_ATTEMPT).exists())
                else:
                    action.click.assert_called_once()
                    self.assertTrue(self.form.intent.with_suffix(FINAL_ATTEMPT).exists())

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

    def authorize_retry(self):
        # Include formatting absent from write_json to test binding to file bytes.
        intent = {
            "version": 1, "date": "2026-11-26", "start_time": "07:00", "end_time": "23:00",
            "building": "NEWTON", "floor": "Parking Torres Quevedo", "space": "PTQ-123", "all_day": False,
        }
        original = (" \n" + json.dumps(intent) + "\n\n").encode()
        self.form.intent.write_bytes(original)
        marker = self.form.intent.with_suffix(RETRY_PERMISSION)
        write_json(marker, {
            "version": 1, "action": "retry-identical-selection-once",
            "original_intent_sha256": sha256(original).hexdigest(), "at": "2026-11-20T10:00:00+01:00",
            "basis": "offline reconciliation fixture",
        })
        return original, marker

    def test_retry_checks_bookings_searches_saved_space_and_waits_once(self):
        original, marker = self.authorize_retry()
        marking = marker.read_bytes()
        claim = self.form.intent.with_suffix(RETRY_ATTEMPT)
        select = self.form.select_space
        click = self.form.click

        def check_claim(**kwargs):
            saved = json.loads(claim.read_text())
            self.assertEqual(saved["original_intent_sha256"], sha256(original).hexdigest())
            click(**kwargs)

        self.form.after_book = ("Bookker reported a booking date or booking window restriction.", None)
        with patch.object(self.form, "select_space", wraps=select) as choose, patch.object(self.form, "click", check_claim):
            result = self.run_command(*EXACT_ARGS, "--confirm")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["submission_observation"]["book"], self.form.after_book[0])
        choose.assert_called_once_with("NEWTON", "Parking Torres Quevedo", ("07:00", "23:00"),
                                       "PTQ-123", None, require_material=True)
        self.assertEqual(self.form.events, ["reload", "book", "observe", "reload"])
        self.assertEqual(self.form.bookings_reads, [(26, True), (26, True)])
        self.assertEqual(self.form.intent.read_bytes(), original)
        self.assertEqual(marker.read_bytes(), marking)
        self.form.events.clear()
        self.assertEqual(self.run_command(*EXACT_ARGS, "--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.events, ["reload"])
        self.form.after_submit = [EXACT_MATCH]
        self.assertEqual(self.run_command(*EXACT_ARGS, "--confirm")["outcome"], "confirmed")
        self.assertEqual(self.form.clicks, 1)

    def test_retry_requires_confirm_exact_date_and_permission_for_original_bytes(self):
        original, marker = self.authorize_retry()
        self.assertEqual(self.run_command(*EXACT_ARGS)["outcome"], "unknown")
        self.assertEqual(self.run_command("--confirm")["outcome"], "unknown")
        self.form.intent.write_bytes(original + b" ")
        self.assertEqual(self.run_command(*EXACT_ARGS, "--confirm")["outcome"], "unknown")
        self.form.intent.write_bytes(original)
        marker.write_text("broken JSON")
        self.assertEqual(self.run_command(*EXACT_ARGS, "--confirm")["outcome"], "unknown")
        self.assertEqual(self.form.days, [])
        self.assertEqual(self.form.bookings_reads, [(26, True)] * 4)
        self.assertFalse(self.form.intent.with_suffix(RETRY_ATTEMPT).exists())

    def test_retry_stops_for_a_competing_or_unreadable_booking(self):
        self.authorize_retry()
        for row, reason in (("Type: Desk\nNovember 26, 2026", "already showed a booking"),
                            ("Type: Parking\nunreadable date", "local date could not be verified")):
            with self.subTest(row=row):
                self.form.after_submit = [row]
                result = self.run_command(*EXACT_ARGS, "--confirm")
                self.assertEqual(result["outcome"], "unknown")
                self.assertIn(reason, result["reason"])
        self.assertEqual(self.form.days, [])
        self.assertFalse(self.form.intent.with_suffix(RETRY_ATTEMPT).exists())

    def test_retry_preserves_intent_on_changed_selection_or_claim_race(self):
        original, _ = self.authorize_retry()
        with patch.object(self.form, "select_space", return_value=SpaceChoice("PTQ-124", material=True)):
            result = self.run_command(*EXACT_ARGS, "--confirm")
        self.assertIn("reviewed selection differed", result["reason"])
        claim = self.form.intent.with_suffix(RETRY_ATTEMPT)
        self.assertFalse(claim.exists())

        def competing_claim(path, data, *, exclusive=False):
            if path == claim:
                write_json(claim, {"claimed": True}, exclusive=True)
            return write_json(path, data, exclusive=exclusive)

        with patch("parking_booking.write_json", competing_claim):
            result = self.run_command(*EXACT_ARGS, "--confirm")
        self.assertIn("Another run", result["reason"])
        self.assertEqual(self.form.clicks, 0)
        self.assertEqual(self.form.intent.read_bytes(), original)

    def test_retry_claim_bounds_a_distinct_verified_final_action(self):
        self.authorize_retry()
        self.form.after_book = ("Bookker displayed a separate final confirmation step.", EXACT_MATCH)
        final_claim = self.form.intent.with_suffix(RETRY_FINAL_ATTEMPT)

        def confirm(**kwargs):
            self.form.events.append("confirm")
            self.assertTrue(final_claim.is_file())
            self.assertNotIn(self.form.vehicle, final_claim.read_text())
            self.form.after_book = ("Bookker displayed a booking acknowledgement.", None)
            self.form.after_submit = [EXACT_MATCH]

        final = Mock()
        final.click.side_effect = confirm
        with patch.object(self.form, "verify_final_prompt", create=True, return_value=final) as verify:
            result = self.run_command(*EXACT_ARGS, "--confirm")
            verify.assert_called_once_with(self.form.selection, self.form.vehicle, EXACT_MATCH)
            self.assertEqual(self.form.events, ["reload", "book", "observe", "confirm", "observe", "reload"])
            self.assertEqual(result["outcome"], "confirmed")
            self.assertEqual(result["submission_observation"]["final_confirmation"], self.form.after_book[0])
            self.run_command(*EXACT_ARGS, "--confirm")
            final.click.assert_called_once()
            self.assertEqual(self.form.clicks, 1)

    def test_retry_does_not_click_an_unverified_final_prompt(self):
        self.authorize_retry()
        self.form.after_book = ("Bookker displayed a separate final confirmation step.", "Confirm booking")
        with patch.object(self.form, "verify_final_prompt", create=True, return_value=None):
            result = self.run_command(*EXACT_ARGS, "--confirm")
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("could not be verified", result["submission_observation"]["final_confirmation"])
        self.assertEqual(self.form.events, ["reload", "book", "observe", "reload"])
        self.assertFalse(self.form.intent.with_suffix(RETRY_FINAL_ATTEMPT).exists())

    def test_retry_acknowledgement_needs_a_bookings_match(self):
        self.authorize_retry()
        self.form.after_book = ("Bookker displayed a booking acknowledgement.", None)
        result = self.run_command(*EXACT_ARGS, "--confirm")
        self.assertEqual(result["outcome"], "unknown")
        self.assertEqual(result["submission_observation"]["book"], self.form.after_book[0])

    def test_receipt_keeps_redacted_observation_when_bookings_reload_fails(self):
        self.authorize_retry()
        self.form.after_book = ("Bookker displayed an error or refused the booking action.", None)
        with patch.object(self.form, "bookings", side_effect=[[], TimeoutError(self.form.vehicle)]):
            result = self.run_command(*EXACT_ARGS, "--confirm")
        self.assertEqual(result["outcome"], "unknown")
        self.assertIn("reload Bookings", result["reason"])
        self.assertEqual(result["submission_observation"]["book"], self.form.after_book[0])

    def test_result_path_cannot_clobber_retry_permission(self):
        _, marker = self.authorize_retry()
        saved = marker.read_bytes()
        with (patch.object(sys, "argv", self.args + ["--result-file", str(marker)]),
              contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit)):
            run_bookker.main()
        self.assertEqual(marker.read_bytes(), saved)

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

    def test_bookings_requires_selected_past_tab_for_historical_results(self):
        page = Mock(url="https://bookker.test/#/bookings")
        view = page.locator.return_value.filter.return_value
        view.count.return_value = 1
        period = view.get_by_label.return_value.filter.return_value
        period.evaluate.return_value = False
        period.inner_text.return_value = "All past bookings"
        history = locator()
        history.get_attribute.return_value = "false"
        upcoming = locator()
        unavailable = locator(0)
        unavailable.first = unavailable
        view.get_by_role.side_effect = lambda role, name=None: (
            history if role == "tab" and name.fullmatch("Past bookings") else unavailable
        )
        view.locator.return_value.filter.return_value.count.return_value = 0
        view.locator.return_value.and_.return_value.filter.return_value.evaluate_all.return_value = []
        empty = view.get_by_text.return_value.and_.return_value.filter.return_value.first
        empty.count.return_value = 1
        empty.inner_text.return_value = "No bookings"
        form = ParkingForm(page)
        day = date(2026, 11, 19)
        with (patch("parking_booking.datetime", RunDate), patch.object(form, "check_session"),
              patch.object(form, "label", return_value=upcoming)):
            with self.assertRaisesRegex(BookingProblem, "period could not be verified"):
                form.bookings("about:blank", day, reload=True)
            history.get_attribute.return_value = "true"
            self.assertEqual(form.bookings("about:blank", day, reload=True), [])
            empty.inner_text.return_value = "No upcoming bookings"
            with self.assertRaisesRegex(BookingProblem, "period could not be verified"):
                form.bookings("about:blank", day, reload=True)
        upcoming.click.assert_not_called()

    def test_material_empty_list_rejects_upcoming_marker_for_a_past_date(self):
        page = Mock()
        view = page.locator.return_value.filter.return_value
        view.count.return_value = 1
        history = locator()
        history.get_attribute.return_value = "true"
        view.get_by_role.side_effect = lambda role, name=None: (
            history if role == "tab" and name.fullmatch("Past bookings") else locator(0)
        )
        material = view.first
        material.evaluate.return_value = True
        material.inner_text.return_value = "No upcoming bookings"
        marker = locator(text="No upcoming bookings")
        material.locator.side_effect = lambda selector: marker if selector == ":scope > div.empty" else locator(0)
        view.locator.return_value.and_.return_value.and_.return_value.filter.return_value.count.return_value = 0
        form = ParkingForm(page)
        with (patch("parking_booking.datetime", RunDate), patch.object(form, "check_session"),
              patch.object(form, "in_bookings", return_value=True),
              patch.object(form, "select_booking_period", return_value=locator(text="All past bookings")),
              patch.object(form, "label", return_value=locator(0))):
            with self.assertRaisesRegex(BookingProblem, "period could not be verified"):
                form.bookings("about:blank", date(2026, 11, 19), reload=True)

    def test_material_card_location_after_schedule(self):
        from parking_booking import shows_location
        row = "Space A1\nschedule 7:00 AM - 11:00 PMNEWTON, Parking Superficie C, other detail"
        self.assertTrue(shows_location(row, "NEWTON", "Parking Superficie C"))
        self.assertFalse(shows_location(row, "NEWTON", "Parking Superficie"))
        self.assertFalse(shows_location(row, "NEWTON", "other detail"))

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
        view.locator.return_value.filter.return_value.count.return_value = 0
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

    def test_bookings_reads_the_active_material_results_article(self):
        page = Mock(url="https://bookker.test/#/bookings")
        view = page.locator.return_value.filter.return_value
        view.count.return_value = 1
        base_locator = view.locator.return_value
        article_locator, flag_locator, loader_locator = Mock(), Mock(), Mock()
        view.locator.side_effect = lambda selector: (
            article_locator if selector.startswith(f"{BOOKING_ARTICLE}:not(") else {
                "article.empty-list": flag_locator, BOOKING_LOADING: loader_locator,
            }.get(selector, base_locator)
        )
        article = article_locator.filter.return_value
        article.count.return_value = 1
        marker_locator, nested_locator = Mock(), Mock()
        article.locator.side_effect = {
            ".empty": marker_locator, f"{BOOKING_LIST_ROWS}, mat-card, article": nested_locator,
        }.__getitem__
        marker = marker_locator.and_.return_value.filter.return_value
        marker.count.return_value = 1
        message = "You don't have any reservations for the selected period yet."
        marker.inner_text.return_value = message
        article.inner_text.return_value = message
        flag = article.and_.return_value.filter.return_value
        flag.count.return_value = 1
        nested = nested_locator.filter.return_value
        nested.count.return_value = 0
        rows = article.or_.return_value
        rows.evaluate_all.return_value = [(message, "card")]
        sibling_rows = base_locator.and_.return_value.and_.return_value.filter.return_value
        sibling_rows.count.return_value = 0
        sibling_rows.evaluate_all.side_effect = AssertionError("Layout card must stay outside the results")
        view.get_by_role.return_value.filter.return_value.first.count.return_value = 0
        period = view.get_by_label.return_value.filter.return_value
        period.evaluate.return_value = False
        period.inner_text.side_effect = ["This week", "This month", "This month"]
        month = Mock()
        month.inner_text.return_value = "This month"
        page.get_by_role.return_value.or_.return_value.filter.return_value.all.return_value = [month]
        loading = loader_locator.and_.return_value.filter.return_value.first
        ready = nested.or_.return_value.first
        form = ParkingForm(page)
        day = date(2026, 11, 26)

        def read():
            return form.bookings("https://bookker.test/", day, reload=False)

        def check_wait_order(**kwargs):
            month.click.assert_called_once()
            loading.wait_for.assert_called_once_with(state="hidden", timeout=10_000)

        with patch("parking_booking.datetime", RunDate), patch.object(form, "check_session"), patch.object(
            form, "label", return_value=Mock(**{"count.return_value": 0})
        ):
            ready.wait_for.side_effect = check_wait_order
            self.assertEqual(read(), [])
            ready.wait_for.side_effect = None
            period.inner_text.side_effect = None
            period.inner_text.return_value = "This month"
            marker_locator.and_.assert_called_with(article.get_by_text.return_value)
            article.get_by_text.assert_called_with(NO_BOOKINGS)

            for source in (nested, sibling_rows):
                source.count.return_value = 1
                with self.assertRaisesRegex(BookingProblem, "conflicting content"):
                    read()
                source.count.return_value = 0
            article.inner_text.return_value = f"{message}\n{MATCH}"
            with self.assertRaisesRegex(BookingProblem, "conflicting content"):
                read()

            flag.count.return_value = 0
            marker.count.return_value = 0
            nested.count.return_value = 2
            first = MATCH.strip().replace("November 23", "November 26")
            second = first.replace("PTQ-123", "PTQ-124")
            rows.evaluate_all.return_value = [("", "layout"), (first, "row", 1), (second, "row", 2)]
            self.assertEqual(read(), [first, second])
            self.assertEqual(parking_dates(read(), form), {day})
            self.assertEqual(rows.evaluate_all.call_args.args[1], BOOKING_LIST_ROWS)
            rows.nth.return_value.evaluate.return_value = True
            form.bookings("https://bookker.test/", day, reload=False, row_text=second)
            rows.nth.assert_called_once_with(2)
            text, token = rows.nth.return_value.evaluate.call_args.args[1]
            self.assertEqual(text, second)
            page.locator.assert_called_with(f'[data-bookker-cli-row="{token}"]')
            sibling_rows.count.return_value = 1
            with self.assertRaisesRegex(BookingProblem, "outside its active results list"):
                read()

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
        view.locator.return_value.filter.return_value.count.return_value = 0
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
        elapsed = date(2026, 11, 19)
        self.assertFalse(period_covers("All upcoming bookings", elapsed, today, False))
        self.assertFalse(period_covers("All upcoming bookings", None, today, False))
        self.assertTrue(period_covers("All upcoming bookings", day, today, False))
        self.assertFalse(period_covers("All past bookings", day, today, False))
        self.assertTrue(period_covers("All past bookings", elapsed, today, False))
        self.assertTrue(period_covers("All bookings", None, today, False))

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

    def test_live_shift_selection_verifies_every_contiguous_shift(self):
        page = Mock()
        chips = page.locator.return_value.filter.return_value
        hours = ("08:00–12:00", "12:00–16:00", "16:00–21:00")
        shifts = [locator(text=interval) for interval in hours]
        for shift in shifts:
            shift.locator.return_value = locator(0)
            shift.evaluate.side_effect = [False, True]
        chips.or_.return_value.filter.return_value.all.return_value = shifts
        form = ParkingForm(page)
        with patch.object(form, "label", return_value=locator(0)):
            self.assertEqual(form.whole_day(verify_all=True), (("08:00", "21:00"), False))
            for shift in shifts:
                shift.click.assert_called_once()
            shifts[1].inner_text.return_value = "13:00–16:00"
            self.assertIsNone(form.whole_day(verify_all=True))

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
        dialog_text = "PTQ-123\nBuilding\nNEWTON, Parking Torres Quevedo\nBook"
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

        def resource_row(name, hours, location, *, location_label=None):
            row = Mock()
            row.inner_text.return_value = f"{name}\n{hours}\n{location}"
            resource = row.locator.return_value.filter.return_value
            resource.count.return_value = 1
            names, clickable = Mock(), Mock()
            names.filter.return_value.count.return_value = 1
            names.filter.return_value.inner_text.return_value = name
            clickable.filter.return_value.count.return_value = 1
            icons = Mock()
            icon = icons.filter.return_value
            icon.count.return_value = int(location_label is not None)
            icon.evaluate.return_value = location_label
            resource.locator.side_effect = {
                ".resource-name": names, ".resource.clickable": clickable, ".info mat-icon": icons,
            }.__getitem__
            return row, clickable.filter.return_value

        partial, partial_button = resource_row("PTQ-101", "07:00–14:00", "NEWTON / Parking Torres Quevedo")
        elsewhere, elsewhere_button = resource_row("PTQ-102", "07:00–23:00", "NEWTON / Visitor Parking")
        wrong_floor, wrong_floor_button = resource_row(
            "PTQ-103", "07:00–23:00", "NEWTON, Parking Torres Quevedo, Visitor Parking",
        )
        wrong_site, wrong_site_button = resource_row(
            "PTQ-104", "07:00–23:00", "NEWTON ANNEX, Parking Torres Quevedo,",
        )
        full, full_button = resource_row(
            "PTQ-123", "07:00–23:00", "schedule 7:00 AM - 11:00 PMlocation_on NEWTON, Parking Torres Quevedo, Floor detail",
            location_label="NEWTON, Parking Torres Quevedo,",
        )
        rows.filter.return_value.all.return_value = [partial, elsewhere, wrong_floor, wrong_site, full]
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
        wrong_floor_button.click.assert_not_called()
        wrong_site_button.click.assert_not_called()
        full_button.click.assert_called_once()
        self.assertEqual(choice, SpaceChoice("PTQ-123", material=True))

        args = (date(2026, 11, 26), span, False, building, floor, choice, self.form.vehicle)
        selection, control = form.review(*args)
        self.assertEqual(selection.space, "PTQ-123")
        self.assertIs(control, submit)
        for detail in ("November 27, 2026", "Date: 11/27", "07:00–14:00", "Vehicle: another vehicle",
                       "Building: OTHER", "Floor: Visitor Parking", "Location: NEWTON / Visitor Parking",
                       "Space: PTQ-124"):
            with self.subTest(detail=detail):
                dialog.inner_text.return_value = f"{dialog_text}\n{detail}"
                with self.assertRaises(BookingProblem):
                    form.review(*args)
        for bad_location in ("OTHER, Parking Torres Quevedo", "NEWTON, Parking Torres Quevedo, Visitor Parking",
                             "NEWTON, Parking Torres Quevedo | Visitor Parking"):
            with self.subTest(bad_location=bad_location):
                dialog.inner_text.return_value = dialog_text.replace("NEWTON, Parking Torres Quevedo", bad_location)
                with self.assertRaises(BookingProblem):
                    form.review(*args)
        dialog.inner_text.return_value = dialog_text
        heading.inner_text.return_value = "PTQ-124"
        with self.assertRaises(BookingProblem):
            form.review(*args)
        submit.click.assert_not_called()

    def test_bookings_all_day_label_reconciles_an_explicit_all_day_selection(self):
        record = MATCH.replace("7:00 AM – 11:00 PM", "Time: All day").replace(
            "Space: PTQ-123\nStatus: Confirmed", "PTQ-123 · Status: Confirmed",
        )
        selection = Selection("2026-11-23", "07:00", "23:00", "NEWTON",
                              "Parking Torres Quevedo", "PTQ-123", all_day=True)
        result = verify_booking([record], selection, self.form, recovered=True)
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual((result["start_time"], result["end_time"]), ("07:00", "23:00"))

    def test_submission_wait_reads_new_feedback_without_logging_page_text(self):
        page = Mock()
        form = ParkingForm(page)
        before = "Booking limit information\n" + self.form.vehicle
        notice = before + "\nThe selected date is not eligible for " + self.form.vehicle
        with patch.object(form, "submission_text", side_effect=[before, TimeoutError(), notice]):
            result, prompt = form.wait_for_submission(before, watch_final=False)
        self.assertEqual(result, "Bookker reported a booking date or booking window restriction.")
        self.assertIsNone(prompt)
        self.assertEqual(page.wait_for_timeout.call_count, 2)
        page.goto.assert_not_called()

    def test_submission_wait_requires_a_stable_follow_on_prompt(self):
        page = Mock()
        form = ParkingForm(page)
        action = Mock()
        elapsed = 0.0

        def advance(ms):
            nonlocal elapsed
            elapsed += ms / 1000

        page.wait_for_timeout.side_effect = advance
        with (patch("parking_booking.monotonic", side_effect=lambda: elapsed),
              patch.object(form, "submission_text", return_value="Please confirm your booking"),
              patch.object(form, "final_prompt", side_effect=[(action, "loading")] + [(action, EXACT_MATCH)] * 5)):
            result, prompt = form.wait_for_submission("Book", watch_final=True)
        self.assertEqual(result, "Bookker displayed a separate final confirmation step.")
        self.assertEqual(prompt, EXACT_MATCH)
        self.assertEqual(elapsed, 1.25)
        page.goto.assert_not_called()

    def test_final_confirmation_requires_the_same_visible_selection(self):
        page = Mock()
        page.locator.return_value.get_attribute.return_value = "en"
        form = ParkingForm(page)
        selection = Selection("2026-11-26", "07:00", "23:00", "NEWTON", "Parking Torres Quevedo", "PTQ-123")
        action = Mock()
        with patch.object(form, "final_prompt", return_value=(action, EXACT_MATCH)):
            self.assertIs(form.verify_final_prompt(selection, self.form.vehicle, EXACT_MATCH), action)
            self.assertIsNone(form.verify_final_prompt(selection, self.form.vehicle, "changed since observed"))
        for old, new in (("November 26", "November 27"), ("11:00 PM", "02:00 PM"), ("PTQ-123", "PTQ-124"),
                         ("NEWTON", "OTHER"), ("Parking Torres Quevedo", "Visitor Parking"),
                         (self.form.vehicle, "other vehicle"), ("Type: Parking", "Type: Parking\nRepeat: Weekly")):
            edited = EXACT_MATCH.replace(old, new)
            with self.subTest(edited=edited), patch.object(form, "final_prompt", return_value=(action, edited)):
                self.assertIsNone(form.verify_final_prompt(selection, self.form.vehicle, edited))
        action.click.assert_not_called()

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
