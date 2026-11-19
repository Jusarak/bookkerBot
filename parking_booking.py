"""Whole-day parking search and verification through Bookker's visible UI."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

MADRID = "Europe/Madrid"
FIRST_DAY = date(2026, 11, 19)
RECONCILE = "Check the intended reservation in Bookker Bookings before retrying."
SIGN_IN = "Renew the saved session through normal Bookker sign-in and rerun."
CHECK_UI = "Check the indicated view in Bookker before rerunning."
PENDING_RESULT = {
    "outcome": "unknown",
    "reason": "A submission may have been sent and has not yet been verified in Bookings.",
    "next_action": RECONCILE,
}

TIME = r"(?:[01]?\d|2[0-4]):[0-5]\d(?:\s*[ap]\.?m\.?)?"
INTERVAL = re.compile(rf"(?<!\d)({TIME})\s*[-–—−]\s*({TIME})(?!\d)", re.I)
ALL_DAY = re.compile(r"^(?:All[- ]day|Whole day|Todo el día)$", re.I)
NO_SPACES = re.compile(
    r"^(?:No (?:available )?(?:parking )?(?:spaces|resources)(?: are)?(?: available| found)?"
    r"(?: for (?:the|this) selected (?:date|interval|period|schedule))?"
    r"|No availability|No hay (?:plazas|recursos)(?: de aparcamiento)? disponibles)\.?$", re.I,
)
NO_BOOKINGS = re.compile(
    r"^(?:No (?:upcoming |active )?(?:bookings|reservations)(?: found| yet)?"
    r"|You (?:do not have|don't have) any (?:upcoming )?(?:bookings|reservations)(?: yet)?"
    r"|No (?:tienes|hay) reservas(?: próximas| activas)?)\.?$", re.I,
)
INACTIVE = re.compile(r"\b(?:cancelled|canceled|expired|rechazada|cancelada|caducada)\b", re.I)
SPACE_CODE = r"[\w][\w .#/-]{0,47}"
DATE_TEXT = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4}|"
    r"[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+(?:de\s+)?"
    r"[A-Za-z]{3,10}\.?\s+(?:de\s+)?\d{4})\b"
)
MONTHS = (
    "January enero", "February febrero", "March marzo", "April abril", "May mayo",
    "June junio", "July julio", "August agosto", "September septiembre",
    "October octubre", "November noviembre", "December diciembre",
)


class BookingProblem(Exception):
    def __init__(self, reason, next_action=CHECK_UI):
        self.reason = reason
        self.next_action = next_action


def outcome(status, reason, next_action):
    return {"outcome": status, "reason": reason, "next_action": next_action}


@dataclass(frozen=True)
class Selection:
    date: str
    start_time: str
    end_time: str
    building: str
    floor: str
    space: str
    all_day: bool = False


def write_json(path: Path, data: dict, *, exclusive=False):
    # Flush a complete intent, create it without overwriting another process's
    # intent, then flush the directory entry before the browser can submit.
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        try:
            json.dump(data, temp, ensure_ascii=False, indent=2)
            temp.write("\n")
            temp.flush()
            os.fsync(temp.fileno())
            if exclusive:
                os.link(temporary, path)
            else:
                os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if exclusive:
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def read_intent(path: Path):
    with path.open(encoding="utf-8") as journal:
        data = json.load(journal)
    if data.pop("version", None) != 1:
        raise ValueError("Unsupported intent version")
    selection = Selection(**data)
    if (
        date.fromisoformat(selection.date).isoformat() != selection.date
        or not re.fullmatch(SPACE_CODE, selection.space)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", selection.start_time)
        or not re.fullmatch(r"(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)", selection.end_time)
        or selection.end_time <= selection.start_time
        or not isinstance(selection.all_day, bool)
    ):
        raise ValueError("Invalid intent")
    return selection


def local_time(text):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?:\s*([ap])\.?m\.?)?", text.lower())
    hour, minute, period = match.groups()
    hour = int(hour)
    if period:
        if not 1 <= hour <= 12:
            raise BookingProblem("Bookker displayed an unreadable parking time.")
        hour = hour % 12 + (12 if period == "p" else 0)
    if hour == 24 and minute != "00":
        raise BookingProblem("Bookker displayed an unreadable parking time.")
    return f"{hour:02d}:{minute}"


def intervals(text):
    result = set()
    for start, end in INTERVAL.findall(text):
        start, end = local_time(start), local_time(end)
        if end == "00:00" and start != "00:00":
            end = "24:00"
        if start < end and start != "24:00":
            result.add((start, end))
    return result


def month_number(word):
    word = word.casefold().strip(".")
    return next((index for index, names in enumerate(MONTHS, 1)
                 if any(word == name.casefold() or word == name[:3].casefold()
                        for name in names.split())), None)


def dates(text, day_first):
    result = set()
    for value in DATE_TEXT.findall(text):
        try:
            if "-" in value:
                result.add(date.fromisoformat(value))
                continue
            if "/" in value:
                first, second, year = map(int, value.split("/"))
                day, month = (first, second) if day_first else (second, first)
            else:
                day, year = map(int, re.findall(r"\d+", value))
                month = next(filter(None, map(month_number, value.split())), None)
                if month is None:
                    continue
            result.add(date(year, month, day))
        except ValueError:
            continue
    return result


def normalized(text):
    return " ".join(text.split()).casefold()


def fields(text):
    return [part.strip() for part in re.split(r"[\n·|]", text) if part.strip()]


def field_values(text, labels):
    pattern = re.compile(rf"^(?:{labels})(?:\s*:\s*(.+))?$", re.I)
    parts = fields(text)
    for index, part in enumerate(parts):
        match = pattern.fullmatch(part)
        if match:
            value = match[1] or (parts[index + 1] if index + 1 < len(parts) else "")
            yield value.strip()


def field(text, labels):
    return next(field_values(text, labels), "")


def shows(text, labels, value):
    labelled = {normalized(part) for part in field_values(text, labels)}
    if labelled:
        return labelled == {normalized(value)}
    return any(normalized(part) == normalized(value) for part in fields(text))


def booking_space(text):
    label = field(text, r"Parking space|Space|Plaza|Resource|Recurso")
    return label if re.fullmatch(SPACE_CODE, label) else ""


def shows_all_day(text):
    return any(ALL_DAY.fullmatch(value) for value in [
        field(text, r"Time|Schedule|Horario"), *fields(text),
    ])


def visible(locator):
    return locator.filter(visible=True)


class ParkingForm:
    def __init__(self, page):
        self.page = page

    def label(self, pattern, scope=None):
        if scope is None:
            scope = self.page
        return visible(scope.get_by_text(pattern, exact=isinstance(pattern, str))).first

    def click_label(self, pattern):
        self.label(pattern).click()

    def settled(self):
        self.page.wait_for_load_state("networkidle", timeout=10_000)

    @property
    def day_first(self):
        language = self.page.locator("html").get_attribute("lang") or ""
        return language.lower().startswith(("es", "en-gb"))

    def check_session(self):
        login = visible(self.page.locator("input[type=password]")).or_(
            self.label(re.compile(r"^(?:Sign in|Log in|Iniciar sesión|Acceder)$", re.I))
        )
        if login.count():
            raise BookingProblem("Bookker requires sign-in.", SIGN_IN)

    def control(self, label):
        page = self.page
        labelled = visible(page.get_by_label(label, exact=True)).or_(
            visible(page.get_by_role("combobox", name=label, exact=True))
        ).first
        if labelled.count():
            return labelled
        # The illustrated Bookker form also uses controls with visual labels.
        caption = self.label(label)
        caption.wait_for()
        wrapper = caption.locator(
            "xpath=ancestor-or-self::*[self::label or self::mat-form-field or "
            "self::mat-select or self::button or self::ion-item or @role='combobox'][1]"
        )
        return wrapper if wrapper.count() else caption.locator("..")

    def value(self, control, label):
        return control.evaluate(
            """(el, label) => {
                const input = el.matches('input,select') ? el : el.querySelector('input,select');
                if (input) return input.selectedOptions
                    ? (input.selectedOptions[0]?.textContent || '').trim() : input.value.trim();
                return (el.getAttribute('aria-valuetext') || el.innerText)
                    .trim().replace(new RegExp('^' + label + '\\\\s*', 'i'), '')
                    .replace(/\\s*(?:expand_more|keyboard_arrow_down|arrow_drop_down)$/i, '').trim();
            }""", label
        )

    def location(self, label, desired, *, select):
        control = self.control(label)
        if select and normalized(self.value(control, label)) != normalized(desired):
            if control.evaluate("(el) => el.tagName === 'SELECT'"):
                control.select_option(label=desired)
            else:
                control.click()
                option = visible(self.page.get_by_role("option", name=desired, exact=True))
                (option.first if option.count() else self.label(desired)).click()
            self.settled()
        if normalized(self.value(self.control(label), label)) != normalized(desired):
            raise BookingProblem(f"Bookker did not show the requested {label.lower()} as selected.")

    def open(self, building, floor, vehicle=None):
        self.click_label("Home")
        self.click_label("Parking")
        self.label("Parking space booking").wait_for()
        if self.label("Type").count():
            self.location("Type", "Parking", select=False)
        self.location("Building", building, select=True)
        self.location("Floor", floor, select=True)
        return self.selected_vehicle(vehicle)

    def check_repeat(self):
        if not self.label("Repeat").count():
            return
        repeat = normalized(self.value(self.control("Repeat"), "Repeat"))
        if repeat and not re.match(r"^(?:add the same booking\b|none$|no repeat$)", repeat):
            raise BookingProblem("Bookker's Repeat field is not empty.",
                                 "Clear repeat dates in the normal Bookker form.")

    def selected_vehicle(self, vehicle):
        selected = self.value(self.control("Vehicle"), "Vehicle")
        if (not selected or re.match(r"^(?:[-–—]+$|select\b|choose\b|add\b|no vehicle\b)",
                                     selected, re.I)
                or (vehicle is not None and normalized(selected) != normalized(vehicle))):
            raise BookingProblem("Bookker did not show the same selected account vehicle.",
                                 "Select the intended vehicle in the normal Bookker form.")
        return selected

    def next_date(self, earliest):
        control = self.control("Date")
        native = control.locator("input[type=date]").or_(control.and_(
            self.page.locator("input[type=date]")
        )).first
        if native.count() and native.get_attribute("max"):
            minimum = native.get_attribute("min")
            candidate = max(earliest, date.fromisoformat(minimum)) if minimum else earliest
            if candidate > date.fromisoformat(native.get_attribute("max")):
                return None
            native.fill(candidate.isoformat())
            native.press("Tab")
        else:
            candidate = self.calendar_date(control, earliest)
            if candidate is None:
                return None
        self.settled()
        if dates(self.value(self.control("Date"), "Date"), self.day_first) != {candidate}:
            raise BookingProblem("Bookker did not retain the requested local date.")
        return candidate

    def calendar_date(self, control, earliest):
        calendar_button = re.compile(r"calendar|date|calendario", re.I)
        toggle = visible(control.get_by_role("button", name=calendar_button))
        if toggle.count() != 1:
            toggle = visible(control.locator("xpath=..").get_by_role("button", name=calendar_button))
        (toggle if toggle.count() == 1 else control).click()
        calendar = visible(self.page.locator("mat-calendar").or_(self.page.get_by_role("grid"))).first
        calendar.wait_for()
        previous = visible(self.page.get_by_role(
            "button", name=re.compile(r"^(?:previous month|mes anterior)$", re.I)
        )).first
        following = visible(self.page.get_by_role(
            "button", name=re.compile(r"^(?:next month|mes siguiente)$", re.I)
        )).first
        direction = None
        last_month = None
        for _ in range(24):
            cells = {}
            day_first = self.day_first
            for cell in visible(calendar.locator(
                "[role=gridcell][aria-label], button[aria-label], button[data-date]"
            )).all():
                label = cell.get_attribute("data-date") or cell.get_attribute("aria-label") or ""
                cell_dates = dates(label, day_first)
                if len(cell_dates) == 1:
                    day = cell_dates.pop()
                    # Some grids display dates from neighboring months.
                    if cell.get_attribute("data-outside-month") in {"true", ""}:
                        continue
                    cells[day] = cell
            if not cells:
                raise BookingProblem("Bookker's calendar did not expose verifiable offered dates.")
            month = max({day.replace(day=1) for day in cells},
                        key=lambda start: sum(day.month == start.month for day in cells))
            if month == last_month:
                raise BookingProblem("Bookker's calendar did not advance to another month.")
            if direction is None and earliest < month and previous.count() and previous.is_enabled():
                last_month = month
                previous.click()
                self.settled()
                continue
            direction = "forward"
            for day, cell in sorted(cells.items()):
                if (day >= earliest and day.month == month.month
                        and cell.is_enabled() and not cell.evaluate(
                            "(el) => !!el.closest('[aria-disabled=true], .mat-calendar-body-disabled')"
                        )):
                    cell.click()
                    return day
            if not following.count():
                raise BookingProblem("Bookker's calendar did not show where the booking window ends.")
            if not following.is_enabled():
                return None
            last_month = month
            following.click()
            self.settled()
        raise BookingProblem("Bookker did not expose a bounded booking window.")

    def whole_day(self):
        page = self.page
        all_day = self.label(ALL_DAY)
        shifts = {}
        # Buttons can contain a status line such as "Shift ended".
        for shift in visible(page.get_by_role("button", name=INTERVAL)).all():
            text = shift.inner_text()
            schedule = intervals(text)
            if len(schedule) != 1:
                raise BookingProblem("A displayed parking shift did not have one readable interval.")
            shifts[schedule.pop()] = shift
        if not shifts and not all_day.count():
            empty = self.label(re.compile(r"^(?:No shifts available|No hay turnos disponibles)\.?$", re.I))
            if empty.count():
                return None
            raise BookingProblem("Bookker did not display parking shifts or an all-day option.")
        if any(not shift.is_enabled() or re.search(r"shift ended|turno finalizado", shift.inner_text(), re.I)
               for shift in shifts.values()):
            return None
        use_all_day = bool(all_day.count() and all_day.is_enabled())
        if use_all_day:
            all_day.click()
        elif shifts:
            start = min(shifts)
            end = max(shifts, key=lambda span: span[1])
            shifts[start].click()
            if end != start:
                shifts[end].click()
        else:
            return None
        span = (min(start for start, _ in shifts), max(end for _, end in shifts)) if shifts else ("", "")
        return span, use_all_day

    def select_space(self, floor, span, spot_name, spot_id):
        random_booking = visible(self.page.get_by_role(
            "switch", name=re.compile(r"random parking space|plaza aleatoria", re.I)
        )).first
        if random_booking.count() and random_booking.get_attribute("aria-checked") != "false":
            raise BookingProblem("Bookker's random parking booking option is on or could not be verified.")
        search = visible(self.page.get_by_role(
            "button", name=re.compile(r"^(?:Search parking spaces|Buscar plazas de aparcamiento)$", re.I)
        )).first
        if search.count() and not search.is_enabled():
            return None
        search.click()
        self.settled()
        if self.label(NO_SPACES).count():
            return None
        self.location("Floor", floor, select=False)
        list_button = visible(self.page.get_by_role(
            "button", name=re.compile(r"^(?:List(?: view)?|Show list|View list|Lista|Ver lista)$", re.I)
        ).or_(self.page.locator("button").filter(has_text=re.compile(r"^\s*(?:list|format_list_bulleted)\s*$")))).first
        if list_button.count():
            list_button.click()
            self.settled()
        if self.label(NO_SPACES).count():
            return None
        rows = visible(self.page.locator("tbody tr, mat-list-option").or_(
            self.page.get_by_role("row").filter(has_not=self.page.get_by_role("columnheader"))
        ).or_(self.page.get_by_role("listitem"))).all()
        unknown_availability = False
        for row in rows:
            text = row.inner_text()
            status = row.get_attribute("data-available")
            if status == "false" or re.search(
                r"\b(?:unavailable|occupied|not available|no disponible|ocupad[ao])\b", text, re.I
            ):
                continue
            available = status == "true" or re.search(
                r"\b(?:available|free|disponible|libre)\b", text, re.I
            )
            if not available:
                unknown_availability = True
                continue
            if unknown_availability:
                break
            displayed_hours = intervals(text)
            if all(span) and displayed_hours and span not in displayed_hours:
                continue
            name = booking_space(text)
            if not name:
                parts = [part.strip() for part in re.split(r"[\n\t]", text) if part.strip()]
                if len(parts) == 2 and re.fullmatch(SPACE_CODE, parts[0]):
                    name = parts[0]
            if not name:
                raise BookingProblem("Bookker showed an available row without a readable parking space name.")
            if spot_name and normalized(name) != normalized(spot_name):
                continue
            if spot_id and row.get_attribute("data-id") != spot_id and name != spot_id:
                continue
            choose = visible(row.get_by_role("button", name=re.compile(r"^(?:Select|Seleccionar)$", re.I)))
            name_cell = self.label(name, row)
            if choose.count() == 1:
                choose.click()
            elif name_cell.count():
                name_cell.click()
            elif not row.get_by_role("button").count():
                row.click()
            else:
                raise BookingProblem("Bookker did not show a separate control for selecting the parking space.")
            self.settled()
            return name
        if rows and not unknown_availability:
            return None
        raise BookingProblem("Bookker did not expose a verifiable list of available parking spaces.")

    def review(self, day, span, all_day, building, floor, space, vehicle):
        dialog = visible(self.page.get_by_role("dialog").or_(
            self.page.locator("mat-dialog-container, ion-modal")
        )).last
        dialog.wait_for()
        submit = visible(dialog.get_by_role("button", name=re.compile(
            r"^(?:Book|Reserve|Confirm(?: booking)?|Book parking space|Reservar|Confirmar(?: reserva)?)$", re.I
        )))
        text = dialog.inner_text()
        hours = intervals(text)
        if not all(span) and len(hours) == 1:
            span = next(iter(hours))
        whole_day_shown = all(span) and all_day and not INTERVAL.search(text) and shows_all_day(text)
        shown_vehicle = {normalized(value) for value in field_values(text, r"Vehicle|Vehículo")}
        if (
            submit.count() != 1 or not submit.is_enabled()
            or dates(text, self.day_first) != {day}
            or (hours != {span} and not whole_day_shown)
            or not shows(text, r"Building|Edificio", building)
            or not shows(text, r"Floor|Planta", floor)
            or not shows(text, r"Parking space|Space|Plaza|Resource|Recurso", space)
            or (shown_vehicle and shown_vehicle != {normalized(vehicle)})
        ):
            raise BookingProblem("Bookker's final selection did not verify one space, date, location and full interval.")
        return Selection(day.isoformat(), *span, building, floor, space, all_day), submit

    def bookings(self, base_url, *, reload):
        if reload:
            self.page.goto(base_url, wait_until="domcontentloaded")
            self.settled()
        self.check_session()
        self.click_label("Bookings")
        self.settled()
        if reload:
            self.page.reload(wait_until="domcontentloaded")
            self.settled()
            self.check_session()
            if self.label("Add booking").count():
                self.click_label("Bookings")
                self.settled()
        if self.label("Add booking").count():
            raise BookingProblem("Bookker remained on Home when opening Bookings.")
        upcoming = self.label(re.compile(r"^(?:Upcoming(?: bookings)?|Próximas(?: reservas)?)$", re.I))
        if upcoming.count():
            upcoming.click()
            self.settled()

        view = self.page
        main = visible(view.get_by_role("main"))
        if main.count() == 1:
            view = main
        rows = visible(view.locator(
            "tbody tr, [role=row]:not(:has([role=columnheader])), "
            "[data-testid=booking], [data-testid=booking-row], [data-testid=booking-card], "
            "mat-card, [role=listitem], article"
        ))
        result = []
        seen = set()
        while True:
            # Some views nest a card inside a list item. Read only the outer row.
            texts = rows.evaluate_all("""rows => rows
                .filter(row => !rows.some(other => row !== other && other.contains(row)))
                .map(row => row.innerText.trim())""")
            snapshot = tuple(texts)
            if snapshot in seen:
                raise BookingProblem("Bookker's Bookings list did not advance to another page.")
            seen.add(snapshot)
            records = [text for text in texts if text and not any(
                INACTIVE.fullmatch(part) for part in [field(text, r"Status|Estado"), *fields(text)]
            )]
            if not texts and not self.label(NO_BOOKINGS, view).count():
                raise BookingProblem("Bookker did not show a readable Bookings list or empty state.")
            result.extend(records)
            following = visible(view.get_by_role(
                "button", name=re.compile(r"^(?:Next page|Página siguiente)$", re.I)
            )).first
            if not following.count() or not following.is_enabled():
                return result
            following.click()
            self.settled()


def parking_dates(records, form):
    reserved = set()
    for record in records:
        kind = field(record, r"Type|Resource type|Tipo")
        if normalized(kind) in {"desk", "meeting room", "coffee area", "escritorio", "sala de reuniones"}:
            continue
        if not re.search(r"\b(?:parking|aparcamiento|estacionamiento|plaza)\b", record, re.I) and (
            re.search(r"\b(?:desk|meeting room|coffee area|escritorio|sala de reuniones)\b", record, re.I)
        ):
            continue
        days = dates(record, form.day_first)
        if len(days) != 1:
            raise BookingProblem("Bookker showed a booking whose local date could not be verified.")
        reserved.update(days)
    return reserved


def verify_booking(records, selection, form, recovered):
    def matches(record):
        shown_hours = intervals(record)
        return (
            dates(record, form.day_first) == {date.fromisoformat(selection.date)}
            and (shown_hours == {(selection.start_time, selection.end_time)}
                 or (selection.all_day and not INTERVAL.search(record) and shows_all_day(record)))
            and shows(record, r"Building|Edificio", selection.building)
            and shows(record, r"Floor|Planta", selection.floor)
            and shows(record, r"Parking space|Space|Plaza|Resource|Recurso", selection.space)
        )

    matches = [record for record in records if matches(record)]
    if len(matches) != 1:
        reason = ("Reloaded Bookings showed multiple matching reservations." if matches
                  else "Reloaded Bookings did not show a verifiable matching reservation.")
        return outcome("unknown", reason, RECONCILE)

    receipt = {"outcome": "confirmed", "timezone": MADRID, **asdict(selection)}
    reference = field(matches[0], r"Confirmation(?: (?:reference|code|number|ID))?|"
                      r"Booking (?:reference|number|ID)|Reservation (?:reference|number|ID)|"
                      r"Referencia(?: de (?:reserva|confirmación))?")
    if re.fullmatch(r"[\w#-]{1,80}", reference):
        receipt["confirmation_reference"] = reference
    if recovered:
        receipt["reconciled"] = True
    return receipt


def book_parking(client, *, building, floor, from_date, whole_day, spot_name, spot_id,
                 confirm_click, submission_state, result_file):
    if not whole_day or not building or not floor:
        return outcome("blocked", "Whole-day parking needs an explicit building and floor.",
                       "Rerun with --whole-day, --building and --floor.")
    if confirm_click and submission_state is None:
        return outcome("blocked", "A confirmed run requires a persistent submission state path.",
                       "Supply --submission-state and reuse that path for retries.")

    pending = None
    if submission_state is not None:
        try:
            pending = read_intent(submission_state)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            return outcome("unknown", "The saved submission intent could not be read.", RECONCILE)
    if pending and (pending.building != building or pending.floor != floor):
        return outcome("unknown", "The saved submission intent is for a different location.", RECONCILE)

    phase = "open the browser with the saved session"
    submitted_or_pending = pending is not None
    try:
        if pending and result_file is not None:
            phase = "mark the saved submission as pending reconciliation"
            write_json(result_file, PENDING_RESULT)
            phase = "open the browser with the saved session"
        client.open_with_session(timezone_id=MADRID)
        form = ParkingForm(client.page)
        phase = "read the account's Bookings"
        records = form.bookings(client.base_url, reload=submitted_or_pending)
        if pending:
            return verify_booking(records, pending, form, recovered=True)
        reserved = parking_dates(records, form)
        earliest = max(from_date or FIRST_DAY, FIRST_DAY, datetime.now(ZoneInfo(MADRID)).date())
        vehicle = None
        while True:
            phase = "verify Home → Parking and its form"
            vehicle = form.open(building, floor, vehicle)
            phase = "read the local date from Bookker's offered calendar"
            day = form.next_date(earliest)
            if day is None:
                return outcome("unavailable", "No eligible whole-day space was found in Bookker's offered window.",
                               "Retry when Bookker opens more dates or availability changes.")
            earliest = day + timedelta(days=1)
            if day in reserved:
                continue
            phase = "select one interval covering the displayed parking shifts"
            schedule = form.whole_day()
            if schedule is None:
                continue
            span, all_day = schedule
            phase = "search for an available parking space"
            form.location("Building", building, select=False)
            form.location("Floor", floor, select=False)
            form.selected_vehicle(vehicle)
            form.check_repeat()
            space = form.select_space(floor, span, spot_name, spot_id)
            if space is None:
                continue
            phase = "verify the final parking selection"
            selection, submit = form.review(day, span, all_day, building, floor, space, vehicle)
            if not confirm_click:
                return {
                    **outcome("blocked", "Dry run reached a verified selection.",
                              "Use the authorized command with --confirm to submit."),
                    "dry_run": True, "timezone": MADRID, **asdict(selection),
                }

            if result_file is not None:
                phase = "write the pending result"
                write_json(result_file, PENDING_RESULT)
            phase = "persist the submission intent"
            try:
                write_json(submission_state, {"version": 1, **asdict(selection)}, exclusive=True)
            except FileExistsError:
                return outcome("unknown", "Another run recorded a submission intent.", RECONCILE)
            submitted_or_pending = True
            # Once the journal is present, even a click error can mean that
            # Bookker received the request. The next browser action is a read.
            try:
                submit.click()
            except Exception:
                pass
            phase = "reload Bookings and verify the submitted reservation"
            records = form.bookings(client.base_url, reload=True)
            return verify_booking(records, selection, form, recovered=False)
    except BookingProblem as error:
        status = "unknown" if submitted_or_pending else "blocked"
        action = error.next_action
        if submitted_or_pending:
            action = RECONCILE if action == CHECK_UI else f"{action} {RECONCILE}"
        return outcome(status, error.reason, action)
    except FileNotFoundError:
        if phase == "open the browser with the saved session":
            return outcome("unknown" if submitted_or_pending else "blocked",
                           "The saved Bookker session is missing.",
                           f"{SIGN_IN} {RECONCILE}" if submitted_or_pending else SIGN_IN)
        action = "Supply existing directories for the result and submission state paths."
        return outcome("unknown" if submitted_or_pending else "blocked", f"The run could not {phase}.",
                       f"{action} {RECONCILE}" if submitted_or_pending else action)
    except OSError:
        action = "Check permissions for the supplied result and submission state directories."
        return outcome("unknown" if submitted_or_pending else "blocked", f"The run could not {phase}.",
                       f"{action} {RECONCILE}" if submitted_or_pending else action)
    except Exception:
        # Browser exceptions can contain URLs, HTML, or a vehicle label.
        action = RECONCILE if submitted_or_pending else CHECK_UI
        if phase == "open the browser with the saved session":
            action = "Check the configured Playwright browser runtime and saved Bookker session."
            if submitted_or_pending:
                action = f"{action} {RECONCILE}"
        return outcome("unknown" if submitted_or_pending else "blocked",
                       f"The run could not {phase}.", action)
