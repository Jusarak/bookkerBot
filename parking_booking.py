"""Whole-day parking search and verification through Bookker's visible UI."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

MADRID = "Europe/Madrid"
FIRST_DAY = date(2026, 11, 19)
TORRES_DATE = date(2026, 11, 26)
TORRES_SHIFTS = (("07:00", "14:00"), ("14:00", "23:00"))
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
UNAVAILABLE_SPACE = re.compile(
    r"\b(?:unavailable|occupied|not available|no disponible|ocupad[ao])\b", re.I,
)
NO_BOOKINGS = re.compile(
    r"^(?:(?:info(?:_outline)?|event_busy)\s+)?(?:No (?:tienes|hay) reservas(?: próximas| activas)?"
    r"|(?:(?:There (?:are|were)|You have) )?(?:currently )?no (?:upcoming |active )?(?:bookings|reservations)"
    r"|You (?:do not have|don['’]t have) (?:any )?(?:upcoming |active )?(?:bookings|reservations))"
    r"(?: (?:were |have been )?found)?(?: yet)?(?: (?:for|in|during|within|on) (?:the |this |that )?"
    r"(?:selected |specified |current |given )?(?:time period|period|date range|dates?|week|month|time range)"
    r"(?: selected)?)?"
    r"(?: yet)?[.!]?$", re.I,
)
BOOKING_ROWS = (
    "tbody tr, [role=row]:not(:has([role=columnheader])), "
    "[data-testid=booking], [data-testid=booking-row], [data-testid=booking-card]"
)
BOOKING_ARTICLE = "article.list-container"
BOOKING_LIST_ROWS = f"{BOOKING_ROWS}, mat-list-item, [role=listitem]"
BOOKING_LOADING = "[aria-busy=true], [role=progressbar], mat-spinner, mat-progress-spinner"
CONFIRMED_STATUS = re.compile(r"confirmed|booked|reserved|approved|confirmad[ao]|reservad[ao]|aprobad[ao]", re.I)
INACTIVE = re.compile(r"cancelled|canceled|expired|rejected|rechazad[ao]|cancelad[ao]|caducad[ao]", re.I)
UNVERIFIED_STATUS = re.compile(r"pending(?: approval)?|requested|pendiente(?: de aprobación)?", re.I)
PERIOD = re.compile(r"\b(?:This (?:week|month|year)|Next (?:week|month|year|\d+ days)|"
                    r"Today|Tomorrow|All (?:time|dates))\b", re.I)
STATUS_FIELD = r"(?:Booking )?Status|Estado(?: de la reserva)?"
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


@dataclass(frozen=True)
class SpaceChoice:
    name: str
    material: bool = False


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


def torres_shifts(day, building, floor):
    if (day == TORRES_DATE and normalized(building) == "newton"
            and normalized(floor) == "parking torres quevedo"):
        return TORRES_SHIFTS
    return ()


def period_covers(label, day, today, day_first):
    label = normalized(label)
    explicit = sorted(dates(label, day_first))
    if len(explicit) == 2:
        return explicit[0] <= day <= explicit[1]
    if len(explicit) == 1:
        return explicit[0] == day

    period = PERIOD.search(label)
    if not period:
        return False
    label = period[0]
    if label.startswith("all "):
        return True
    if label in {"today", "tomorrow"}:
        return day == today + timedelta(days=label == "tomorrow")
    if label.endswith(" week"):
        monday = today - timedelta(days=today.weekday())
        start = monday + timedelta(weeks=label == "next week")
        return start <= day < start + timedelta(weeks=1)
    if label.endswith(" month"):
        month = today.year * 12 + today.month + (label == "next month")
        return day.year * 12 + day.month == month
    if label.endswith(" year"):
        return day.year == today.year + (label == "next year")
    # A relative day count can include today or start tomorrow. Use only the
    # dates that are in the filter under either interpretation.
    return today < day < today + timedelta(days=int(label.split()[1]))


def fields(text):
    return [part.strip() for part in re.split(r"[\n·|]", text) if part.strip()]


def field_values(text, labels, *, split_inline=True):
    pattern = re.compile(rf"^(?:{labels})(?:\s*:\s*(.+))?$", re.I)
    parts = fields(text) if split_inline else [line.strip() for line in text.splitlines() if line.strip()]
    for index, part in enumerate(parts):
        match = pattern.fullmatch(part)
        if match:
            value = match[1] or (parts[index + 1] if index + 1 < len(parts) else "")
            yield value.strip()


def field(text, labels):
    return next(field_values(text, labels), "")


def booking_statuses(text):
    labelled = list(field_values(text, STATUS_FIELD))
    if labelled:
        return labelled
    return [part for part in fields(text) if any(pattern.fullmatch(part) for pattern in (
        CONFIRMED_STATUS, INACTIVE, UNVERIFIED_STATUS,
    ))]


def shows(text, labels, value):
    labelled = {normalized(part) for part in field_values(text, labels)}
    if labelled:
        return labelled == {normalized(value)}
    return any(normalized(part) == normalized(value) for part in fields(text))


def shows_location(text, building, floor):
    building_labels, floor_labels = r"Building|Edificio", r"Floor|Planta"
    building_name, floor_name = normalized(building), normalized(floor)
    first, second = re.escape(building_name), re.escape(floor_name)
    separator = r"(?:\s*[-–—/,·•|]\s*|\s+)"
    pair = re.compile(rf"(?:{first}{separator}{second}|{second}{separator}{first}|"
                      rf"{first}\s*\({second}\)|{second}\s*\({first}\))(?:\s*,)?")
    floors = {normalized(value) for value in field_values(text, floor_labels)}
    if floors and floors != {floor_name}:
        return False
    buildings = {normalized(value) for value in field_values(text, building_labels)}
    combined = buildings - {building_name}
    if combined:
        # The resource dialog displays the combined location under Building.
        # Verify the entire value before accepting its delimited components.
        full_values = {normalized(value) for value in field_values(text, building_labels, split_inline=False)}
        if not combined <= full_values or any(not pair.fullmatch(value) for value in combined):
            return False
    lines = [re.sub(r"^(?:location_on|place)\s+", "", normalized(line)) for line in text.splitlines()]
    labelled = list(field_values("\n".join(lines), r"Location|Ubicación", split_inline=False))
    if labelled:
        return all(pair.fullmatch(line) for line in labelled)
    return (bool(combined)
            or (shows(text, building_labels, building) and shows(text, floor_labels, floor))
            or any(pair.fullmatch(line) for line in lines))


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

    def settled(self):
        self.page.wait_for_load_state("networkidle", timeout=10_000)

    @property
    def day_first(self):
        language = self.page.locator("html").get_attribute("lang") or ""
        return language.lower().startswith(("es", "en-gb"))

    def check_session(self):
        first_step = self.page.locator(
            "app-login input:is([type=email], [name=email], [autocomplete=email], [formcontrolname=email])"
        )
        login = visible(self.page.locator("input[type=password]").or_(first_step)).or_(
            self.label(re.compile(r"^(?:Sign in|Log in|Iniciar sesión|Acceder)$", re.I))
        )
        url = urlsplit(self.page.url)
        login_route = any(re.search(r"(?:^|/)(?:login|sign-in)(?:/|$)", path) for path in (
            url.path, url.fragment.split("?", 1)[0],
        ))
        if login_route or login.count():
            raise BookingProblem("Bookker requires sign-in.", SIGN_IN)

    def navigation(self, name):
        # Material icons can be included in the link's accessible name.
        link = self.page.get_by_role("link", name=re.compile(rf"\b{name}\b", re.I))
        route = visible(self.page.locator(f'a[href$="/{name.lower()}"]'))
        # The Home logo and side navigation both mention Home.
        return route if route.count() == 1 else visible(link.or_(route))

    def control(self, label):
        page = self.page
        caption = page.get_by_text(label, exact=True)
        material = visible(page.locator("mat-form-field").filter(
            has=caption.and_(page.locator("mat-label, label"))
        ).locator("mat-select"))
        if material.count():
            return material
        labelled = visible(page.get_by_label(label, exact=True)).or_(
            visible(page.get_by_role("combobox", name=label, exact=True))
        ).first
        if labelled.count():
            return labelled
        # The illustrated Bookker form also uses controls with visual labels.
        caption = self.label(label)
        caption.wait_for(timeout=10_000)
        wrapper = caption.locator(
            "xpath=ancestor-or-self::*[self::label or self::mat-form-field or "
            "self::mat-select or self::button or self::ion-item or @role='combobox'][1]"
        )
        return wrapper if wrapper.count() else caption.locator("..")

    def value(self, control, label):
        return control.evaluate(
            """(el, label) => {
                if (el.matches('mat-select')) {
                    const value = el.querySelector('.mat-mdc-select-value, .mat-select-value');
                    if (value) return (value.querySelector(
                        '.mat-mdc-select-value-text, .mat-select-value-text'
                    )?.innerText || '').trim();
                }
                const input = el.matches('input,select') ? el : el.querySelector('input,select');
                if (input) return input.selectedOptions
                    ? (input.selectedOptions[0]?.textContent || '').trim() : input.value.trim();
                return (el.getAttribute('aria-valuetext') || el.innerText)
                    .trim().replace(new RegExp('^' + label + '\\\\s*', 'i'), '')
                    .replace(/\\s*(?:expand_more|keyboard_arrow_down|arrow_drop_down)$/i, '').trim();
            }""", label
        )

    @contextmanager
    def parking_step(self, reason, next_action):
        try:
            yield
        except BookingProblem:
            raise
        except Exception:
            try:
                self.check_session()
            except BookingProblem:
                raise
            except Exception:
                pass
            raise BookingProblem(reason, next_action) from None

    def location(self, label, desired, *, select):
        action = f"Check the {label} dropdown and its available options in the Bookker parking form."
        with self.parking_step(f"Bookker's parking {label} control could not be read.", action):
            control = self.control(label)
            if normalized(self.value(control, label)) == normalized(desired):
                return
        if select:
            with self.parking_step(f"The requested parking {label} could not be selected in Bookker.", action):
                if control.evaluate("(el) => el.tagName === 'SELECT'"):
                    control.select_option(label=desired, timeout=10_000)
                else:
                    control.click(timeout=10_000)
                    option = visible(self.page.get_by_role("option", name=desired, exact=True))
                    (option.first if option.count() else self.label(desired)).click(timeout=10_000)
                self.settled()
            with self.parking_step(f"Bookker's selected parking {label} could not be verified.", action):
                if normalized(self.value(self.control(label), label)) == normalized(desired):
                    return
        raise BookingProblem(f"Bookker did not show the requested {label.lower()} as selected.", action)

    def open(self, building, floor, vehicle=None):
        with self.parking_step("The Bookker Home navigation link could not be opened.",
                               "Open Home from Bookker's sidebar and check that it loads."):
            self.navigation("Home").click(timeout=10_000)
            self.settled()
            self.check_session()
        with self.parking_step("The Add a Parking booking button on Home could not be opened.",
                               "Check that the Parking card on Bookker Home has an enabled add button."):
            visible(self.page.get_by_role(
                "button", name=re.compile(r"\bAdd (?:a )?parking booking\b", re.I)
            )).click(timeout=10_000)
        with self.parking_step("Bookker did not display the Parking space booking form.",
                               "Open the Parking card on Home and check that the booking form loads."):
            self.label("Parking space booking").wait_for(timeout=10_000)
        with self.parking_step("Bookker's parking Type control could not be read.",
                               "Check the Type dropdown in the Bookker parking form."):
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
        action = "Select the intended vehicle in the normal Bookker form."
        with self.parking_step("Bookker's parking Vehicle control could not be read.", action):
            selected = self.value(self.control("Vehicle"), "Vehicle")
        if (not selected or re.match(r"^(?:[-–—]+$|select\b|choose\b|add\b|no vehicle\b)",
                                     selected, re.I)
                or (vehicle is not None and normalized(selected) != normalized(vehicle))):
            raise BookingProblem("Bookker did not show the same selected account vehicle.", action)
        return selected

    def next_date(self, earliest, *, exact=False):
        control = self.control("Date")
        native = control.locator("input[type=date]").or_(control.and_(
            self.page.locator("input[type=date]")
        )).first
        native_present = bool(native.count())
        maximum = native.get_attribute("max") if native_present else None
        if maximum or (exact and native_present):
            minimum = native.get_attribute("min")
            candidate = max(earliest, date.fromisoformat(minimum)) if minimum else earliest
            if (maximum and candidate > date.fromisoformat(maximum)) or (exact and candidate != earliest):
                return None
            if exact and not native.is_editable():
                return None
            native.fill(candidate.isoformat())
            native.press("Tab")
        else:
            candidate = self.calendar_date(control, earliest, exact=exact)
            if candidate is None:
                return None
        self.settled()
        self.check_date(candidate)
        return candidate

    def check_date(self, day):
        if dates(self.value(self.control("Date"), "Date"), self.day_first) != {day}:
            raise BookingProblem("Bookker did not retain the requested local date.",
                                 "Check the selected date in the Bookker parking form.")

    def calendar_date(self, control, earliest, *, exact=False):
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
            if exact:
                candidates = [(earliest, cells[earliest])] if earliest in cells else []
            else:
                candidates = sorted(cells.items())
            for day, cell in candidates:
                if (day >= earliest and day.month == month.month
                        and cell.is_enabled() and not cell.evaluate(
                            "(el) => !!el.closest('[aria-disabled=true], .mat-calendar-body-disabled')"
                        )):
                    cell.click()
                    return day
            if exact and month >= earliest.replace(day=1):
                return None
            if not following.count():
                raise BookingProblem("Bookker's calendar did not show where the booking window ends.")
            if not following.is_enabled():
                return None
            last_month = month
            following.click()
            self.settled()
        raise BookingProblem("Bookker did not expose a bounded booking window.")

    def whole_day(self, expected_shifts=()):
        page = self.page
        all_day = self.label(ALL_DAY)
        chips = page.locator("mat-chip-option").filter(has_text=INTERVAL)
        buttons = page.get_by_role("button", name=INTERVAL).and_(page.locator(":not(mat-chip-option *)"))
        shifts = {}
        for shift in visible(chips.or_(buttons)).all():
            text = shift.inner_text()
            schedule = intervals(text)
            if len(schedule) != 1:
                raise BookingProblem("A displayed parking shift did not have one readable interval.")
            action = visible(shift.locator("button"))
            action = action if action.count() == 1 else shift
            if (not shift.is_enabled() or not action.is_enabled()
                    or re.search(r"shift ended|turno finalizado", text, re.I)):
                return None
            interval = schedule.pop()
            if expected_shifts and interval in shifts:
                raise BookingProblem("Bookker displayed the same parking shift more than once.")
            shifts[interval] = shift, action
        if not shifts:
            empty = self.label(re.compile(r"^(?:No shifts available|No hay turnos disponibles)\.?$", re.I))
            if empty.count():
                return None
            raise BookingProblem("Bookker did not display parking shift hours to verify a whole day.")
        if expected_shifts:
            if set(shifts) != set(expected_shifts):
                return None
            selected = """(el) => {
                const active = '[aria-selected=true], [aria-pressed=true], [aria-checked=true]';
                return el.matches(active + ', .mat-mdc-chip-selected, .mat-chip-selected, ' +
                    '.mdc-evolution-chip--selected') || !!el.querySelector(active);
            }"""
            for interval in expected_shifts:
                shift, action = shifts[interval]
                if not shift.evaluate(selected):
                    action.click()
            self.settled()
            if not all(shift.evaluate(selected) for shift, _ in shifts.values()):
                raise BookingProblem("Bookker did not retain both requested parking shifts.",
                                     "Check that both parking shifts are selected before searching.")
            return (expected_shifts[0][0], expected_shifts[-1][1]), False

        use_all_day = bool(all_day.count() and all_day.is_enabled())
        start = min(shifts)
        end = max(shifts, key=lambda span: span[1])
        if use_all_day:
            all_day.click()
        else:
            shifts[start][1].click()
            if end != start:
                shifts[end][1].click()
        return (start[0], end[1]), use_all_day

    def select_space(self, building, floor, span, spot_name, spot_id, *, require_material=False):
        random_booking = visible(self.page.get_by_role(
            "switch", name=re.compile(r"random (?:parking )?space|plaza aleatoria", re.I)
        ))
        switches = random_booking.count()
        if (switches > 1 or (require_material and not switches)
                or (switches and random_booking.get_attribute("aria-checked") != "false")):
            raise BookingProblem("Bookker's random parking booking option is on or could not be verified.",
                                 "Check that the random parking space switch is off before searching.")
        search = visible(self.page.get_by_role(
            "button", name=re.compile(r"\b(?:Search parking spaces|Buscar plazas de aparcamiento)\b", re.I)
        ))
        with self.parking_step("The Search parking spaces button could not be opened.",
                               "Check the enabled Search parking spaces button in the parking form."):
            search.wait_for(timeout=10_000)
            if not search.is_enabled():
                return None
            search.focus(timeout=10_000)
            search.press("Enter", timeout=10_000)
            self.settled()
        if self.label(NO_SPACES).count():
            return None
        self.location("Floor", floor, select=False)
        list_button = visible(self.page.get_by_role(
            "button", name=re.compile(r"^(?:(?:list|format_list_bulleted)\s+)?"
                                      r"(?:List(?: view)?|Show list|View list|Lista|Ver lista)$", re.I)
        ).or_(self.page.locator("button").filter(has_text=re.compile(r"^\s*(?:list|format_list_bulleted)\s*$"))))
        if list_button.count():
            with self.parking_step("The Bookker map's List button could not be opened.",
                                   "Check the enabled List button on the Bookker results map."):
                if not list_button.is_enabled():
                    raise BookingProblem("The Bookker map's List button is disabled.",
                                         "Check whether Bookker can display its parking results as a list.")
                list_button.focus(timeout=10_000)
                list_button.press("Enter", timeout=10_000)
                self.settled()
        if self.label(NO_SPACES).count():
            return None
        material_rows = visible(self.page.locator("app-resources article.list-container mat-list-item")).all()
        if material_rows:
            return self.select_material_space(material_rows, building, floor, span, spot_name, spot_id)
        if require_material:
            raise BookingProblem("Bookker did not show a readable list of named parking resources.",
                                 "Check the parking results list for named spaces with location and hours.")
        rows = visible(self.page.locator("tbody tr, mat-list-option").or_(
            self.page.get_by_role("row").filter(has_not=self.page.get_by_role("columnheader"))
        ).or_(self.page.get_by_role("listitem"))).all()
        unknown_availability = False
        for row in rows:
            text = row.inner_text()
            status = row.get_attribute("data-available")
            if status == "false" or UNAVAILABLE_SPACE.search(text):
                continue
            available = status == "true" or re.search(
                r"\b(?:available|free|disponible|libre)\b", text, re.I
            )
            if not available:
                unknown_availability = True
                continue
            displayed_hours = intervals(text)
            if displayed_hours and span not in displayed_hours:
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
            return SpaceChoice(name)
        if rows and not unknown_availability:
            return None
        raise BookingProblem("Bookker did not expose a verifiable list of available parking spaces.")

    def select_material_space(self, rows, building, floor, span, spot_name, spot_id):
        unreadable = False
        for row in rows:
            text = row.inner_text()
            if row.get_attribute("data-available") == "false" or UNAVAILABLE_SPACE.search(text):
                continue
            resource = visible(row.locator("app-resource"))
            names = visible(resource.locator(".resource-name"))
            choose = visible(resource.locator(".resource.clickable"))
            if resource.count() != 1 or names.count() != 1 or choose.count() != 1:
                unreadable = True
                continue
            name = names.inner_text().strip()
            hours = intervals(text)
            if not re.fullmatch(SPACE_CODE, name) or not hours:
                unreadable = True
                continue
            if hours != {span} or not shows_location(text, building, floor) or not choose.is_enabled():
                continue
            if spot_name and normalized(name) != normalized(spot_name):
                continue
            if spot_id and spot_id not in (row.get_attribute("data-id"), resource.get_attribute("data-id"), name):
                continue
            with self.parking_step("The named parking resource could not be opened for review.",
                                   "Check the named resource in the Bookker parking results list."):
                choose.click(timeout=10_000)
                self.settled()
            return SpaceChoice(name, material=True)
        if unreadable:
            raise BookingProblem("Bookker showed parking rows without readable names, controls or hours.",
                                 "Check the named spaces and their availability in the Bookker list.")
        return None

    def review(self, day, span, all_day, building, floor, choice, vehicle):
        space = choice.name
        if choice.material:
            dialog = visible(self.page.locator("mat-dialog-container"))
            submit_label = re.compile(r"^(?:Book|Reservar)$", re.I)
        else:
            dialog = visible(self.page.get_by_role("dialog").or_(
                self.page.locator("mat-dialog-container, ion-modal")
            )).last
            submit_label = re.compile(
                r"^(?:Book|Reserve|Confirm(?: booking)?|Book parking space|Reservar|Confirmar(?: reserva)?)$", re.I
            )
        dialog.wait_for(timeout=10_000)
        submit = visible(dialog.get_by_role("button", name=submit_label))
        text = dialog.inner_text()
        details = text
        if choice.material:
            details = "\n".join(line for line in text.splitlines() if normalized(line) != normalized(space))
        hours = intervals(details)
        whole_day_shown = all_day and not INTERVAL.search(details) and shows_all_day(details)
        shown_vehicle = {normalized(value) for value in field_values(text, r"Vehicle|Vehículo")}
        shown_dates = dates(details, self.day_first)
        right_date = shown_dates == {day}
        right_hours = hours == {span} or whole_day_shown
        space_labels = r"Parking space|Space|Plaza|Resource|Recurso"
        right_space = shows(text, space_labels, space)
        if choice.material:
            # The named resource dialog may omit date and hours. They were
            # checked in the form and the exact row that opened this dialog.
            heading = visible(dialog.locator("h2"))
            right_space = (heading.count() == 1 and normalized(heading.inner_text()) == normalized(space)
                           and (not list(field_values(text, space_labels)) or right_space))
            if not shown_dates:
                right_date = not re.search(r"\b(?:Date|Fecha)\b|(?<!\d)\d{1,2}/\d{1,2}", details, re.I)
            if not hours:
                right_hours = whole_day_shown or not re.search(rf"\b(?:Time|Schedule|Horario)\b|{TIME}", details, re.I)
            expected = torres_shifts(day, building, floor)
            if expected and hours == set(expected):
                right_hours = True
        if (
            submit.count() != 1 or not submit.is_enabled()
            or not right_date
            or not all(span)
            or not right_hours
            or not shows_location(text, building, floor)
            or not right_space
            or (shown_vehicle and shown_vehicle != {normalized(vehicle)})
        ):
            raise BookingProblem("Bookker's final selection did not verify one space, date, location and full interval.")
        return Selection(day.isoformat(), *span, building, floor, space, all_day), submit

    def in_bookings(self):
        url = urlsplit(self.page.url)
        return any(re.search(r"(?:^|/)(?:my-)?bookings(?:/|$)", path) for path in (
            url.path, url.fragment.split("?", 1)[0],
        )) or visible(self.page.get_by_role("heading", name="Bookings", exact=True)).count() == 1

    def open_bookings(self):
        try:
            self.navigation("Bookings").click(timeout=10_000)
            self.settled()
        except Exception:
            self.check_session()
            raise BookingProblem("The Bookings navigation link could not be opened.",
                                 "Open Bookings from Bookker's visible navigation and check the view.") from None
        self.check_session()
        if not self.in_bookings():
            raise BookingProblem("The Bookings navigation link did not reach the Bookings view.",
                                 "Open Bookings from Bookker's visible navigation and check the view.")

    def select_booking_period(self, day, view):
        page = self.page
        combo = view.get_by_role("combobox")
        period = visible(view.get_by_label(re.compile(r"^(?:Booking )?(?:Period|Date range)$", re.I)))
        if not period.count():
            period = visible(combo.filter(has_text=PERIOD).or_(combo.filter(has_text=DATE_TEXT)))
        if not period.count():
            period = visible(view.get_by_role("button", name=PERIOD))
        today = datetime.now(ZoneInfo(MADRID)).date()
        day_first = self.day_first

        def covers(label):
            return period_covers(label, day, today, day_first)

        problem = BookingProblem(
            "The Bookings period could not be verified for the candidate date.",
            "Check the Bookings period filter for a range that includes the intended date.",
        )
        try:
            period.wait_for(timeout=10_000)
            native = period.evaluate("el => el.tagName === 'SELECT'")

            def selected_period():
                if native:
                    return period.evaluate("el => el.selectedOptions[0]?.label || ''")
                return period.inner_text()

            if covers(selected_period()):
                return period
            if native:
                for option in period.locator("option").all():
                    label = option.evaluate("el => el.label")
                    if option.is_enabled() and covers(label):
                        period.select_option(label=label)
                        break
            else:
                period.click()
                options = visible(page.get_by_role("option").or_(page.get_by_role("menuitem")))
                options.first.wait_for(timeout=10_000)
                for option in options.all():
                    if option.is_enabled() and covers(option.inner_text()):
                        option.click()
                        break
            self.settled()
            if covers(selected_period()):
                return period
        except Exception:
            self.check_session()
            raise problem from None
        raise problem

    def bookings(self, base_url, day, *, reload):
        if reload or not self.in_bookings():
            # Leave the parking form before using the navigation links. It can
            # cover them while the form or its review dialog is open.
            self.page.goto(base_url, wait_until="domcontentloaded")
            self.settled()
        self.check_session()
        if not self.in_bookings():
            self.open_bookings()
        if reload:
            self.page.reload(wait_until="domcontentloaded")
            self.settled()
            self.check_session()
            if not self.in_bookings():
                self.open_bookings()
        view = visible(self.page.locator("app-bookings"))
        views = view.count()
        if views > 1:
            raise BookingProblem("Bookker showed multiple active Bookings views.")
        if not views:
            main = visible(self.page.get_by_role("main"))
            view = main if main.count() == 1 else self.page

        upcoming = self.label(re.compile(r"^(?:Upcoming(?: bookings)?|Próximas(?: reservas)?)$", re.I), view)
        if upcoming.count():
            upcoming.click()
            self.settled()
        period = self.select_booking_period(day, view)
        self.check_session()
        if not self.in_bookings():
            raise BookingProblem("Bookker left the Bookings view while selecting the period.")

        navigation = ":is(nav, aside, [role=navigation], [role=complementary])"
        content = view.locator(f":not({navigation}, {navigation} *)")
        loading = visible(view.locator(BOOKING_LOADING).and_(content)).first
        results_action = "Check that the selected Bookings period has finished loading."
        with self.parking_step("Bookker did not finish loading the selected Bookings period.", results_action):
            loading.wait_for(state="hidden", timeout=10_000)
        articles = visible(view.locator(f"{BOOKING_ARTICLE}:not({navigation} *)"))
        article_count = articles.count()
        if article_count > 1:
            raise BookingProblem("Bookker showed multiple active booking results lists.")
        if article_count == 1:
            # The card around the Angular tabs is layout. Only the active
            # results article and its children can supply reservation records.
            explicit_rows = BOOKING_LIST_ROWS
            nested = visible(articles.locator(f"{explicit_rows}, mat-card, article"))
            rows = articles.or_(nested)
            empty = visible(articles.locator(".empty").and_(articles.get_by_text(NO_BOOKINGS)))
            empty_articles = visible(articles.and_(view.locator("article.empty-list")))
            outside = visible(view.locator(BOOKING_ROWS).and_(content).and_(
                view.locator(f":not({BOOKING_ARTICLE} *)")
            ))
        else:
            explicit_rows = BOOKING_ROWS
            rows = visible(view.locator(f"{explicit_rows}, mat-card, [role=listitem], article").and_(content))
            empty = visible(view.get_by_text(NO_BOOKINGS).and_(content)).first
        period_text = re.escape(normalized(period.inner_text()))
        period_layout = rf"(?:(?:booking )?(?:period|date range)\s*:?\s*)?(?:{period_text})?"
        result = []
        seen = set()
        while True:
            if article_count == 1:
                with self.parking_step("Bookker did not show the results for the selected Bookings period.",
                                       results_action):
                    nested.or_(empty).first.wait_for(timeout=10_000)
                empty_count = empty.count()
                empty_article_count = empty_articles.count()
                if empty_count or empty_article_count:
                    if (empty_count != 1 or empty_article_count != 1
                            or normalized(articles.inner_text()) != normalized(empty.inner_text())
                            or nested.count() or outside.count()):
                        raise BookingProblem("Bookker showed conflicting content in its Bookings empty list.",
                                             "Check which reservations are shown for the selected period in Bookker.")
                elif outside.count():
                    raise BookingProblem("Bookker showed reservation rows outside its active results list.",
                                         "Check which reservations are shown for the selected period in Bookker.")
            cards = rows.evaluate_all(r"""(rows, bookingRows) => {
                const explicit = rows.filter(row => row.matches(bookingRows));
                // Generic cards inside an explicit row are part of that row.
                const candidates = rows.filter(row => row.matches(bookingRows) ||
                    !explicit.some(parent => parent.contains(row)));
                const children = new Map(candidates.map(row => [row, []]));
                for (const row of candidates) {
                    let parent = row.parentElement;
                    while (parent && !children.has(parent)) parent = parent.parentElement;
                    if (parent) children.get(parent).push(row);
                }
                const flat = text => text.trim().replace(/\s+/g, ' ');
                return candidates.map(row => {
                    const text = row.innerText.trim();
                    const nested = children.get(row);
                    if (!nested.length) {
                        return [text, row.matches(bookingRows) ? 'row' : 'card'];
                    }
                    // Keep nested rows separate and check that the outer
                    // card contains only layout text outside them.
                    let remainder = flat(text);
                    const fragments = [];
                    for (const child of nested) {
                        const childText = flat(child.innerText);
                        if (!childText) continue;
                        const start = remainder.indexOf(childText);
                        if (start < 0) return [null, 'layout'];
                        fragments.push(remainder.slice(0, start));
                        remainder = remainder.slice(start + childText.length);
                    }
                    return [fragments.concat(remainder).join(' ').trim(), 'layout'];
                });
            }""", explicit_rows)
            empty_text = normalized(empty.inner_text()) if empty.count() else ""
            layout = period_layout
            if empty_text:
                marker = re.escape(empty_text)
                layout = rf"(?:{layout}|{layout}\s*{marker}|{marker}\s*{layout})"
            if any(kind == "layout" and (text is None or not re.fullmatch(layout, normalized(text)))
                   for text, kind in cards):
                raise BookingProblem("Bookker did not expose separate reservation rows.",
                                     "Check the reservation rows in Bookings for the selected period.")
            texts = [text for text, kind in cards if kind == "row" or (
                kind == "card" and text and not re.fullmatch(layout, normalized(text))
            )]
            if empty_text and texts:
                raise BookingProblem("Bookker showed row content alongside its Bookings empty state.",
                                     "Check which reservations are shown for the selected period in Bookker.")
            if (not texts and not empty_text) or any(not text for text in texts):
                raise BookingProblem("Bookker did not show a readable Bookings list or empty state.",
                                     "Check the Bookings list for the selected period in Bookker.")
            snapshot = tuple(texts)
            if snapshot in seen:
                raise BookingProblem("Bookker's Bookings list did not advance to another page.")
            seen.add(snapshot)
            for text in texts:
                statuses = booking_statuses(text)
                if not statuses or not all(INACTIVE.fullmatch(status) for status in statuses):
                    result.append(text)
            following = visible(view.get_by_role(
                "button", name=re.compile(r"^(?:Next page|Página siguiente)$", re.I)
            )).first
            if not following.count() or not following.is_enabled():
                return result
            following.click()
            self.settled()
            with self.parking_step("Bookker did not finish loading the next Bookings page.", results_action):
                loading.wait_for(state="hidden", timeout=10_000)


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
    day = date.fromisoformat(selection.date)
    shifts = torres_shifts(day, selection.building, selection.floor)
    span = (selection.start_time, selection.end_time)

    def matches(record):
        shown_hours = intervals(record)
        return (
            dates(record, form.day_first) == {day}
            and (shown_hours == {span}
                 or (shifts and span == (shifts[0][0], shifts[-1][1]) and shown_hours == set(shifts))
                 or (selection.all_day and not INTERVAL.search(record) and shows_all_day(record)))
            and shows_location(record, selection.building, selection.floor)
            and shows(record, r"Parking space|Space|Plaza|Resource|Recurso", selection.space)
        )

    matches = [record for record in records if matches(record)]
    if len(matches) != 1:
        reason = ("Reloaded Bookings showed multiple matching reservations." if matches
                  else "Reloaded Bookings did not show a verifiable matching reservation.")
        return outcome("unknown", reason, RECONCILE)

    statuses = booking_statuses(matches[0])
    if not statuses or not all(CONFIRMED_STATUS.fullmatch(status) for status in statuses):
        return outcome("unknown", "The matching record in reloaded Bookings did not show a confirmed status.",
                       RECONCILE)

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
                 confirm_click, submission_state, result_file, on_date=None):
    if not whole_day or not building or not floor:
        return outcome("blocked", "Whole-day parking needs an explicit building and floor.",
                       "Rerun with --whole-day, --building and --floor.")
    if confirm_click and submission_state is None:
        return outcome("blocked", "A confirmed run requires a persistent submission state path.",
                       "Supply --submission-state and reuse that path for retries.")

    expected_shifts = torres_shifts(on_date, building, floor)
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
    if pending and on_date and pending.date != on_date.isoformat():
        return outcome("unknown", "The saved submission intent is for a different date.", RECONCILE)
    if pending and expected_shifts and (
        (pending.start_time, pending.end_time) != (expected_shifts[0][0], expected_shifts[-1][1])
        or pending.all_day
    ):
        return outcome("unknown", "The saved submission intent does not verify both requested parking shifts.",
                       RECONCILE)
    today = datetime.now(ZoneInfo(MADRID)).date()
    if not pending and on_date and on_date < today:
        return outcome("unavailable", "The requested local date has passed in Europe/Madrid.",
                       "Check Bookker Bookings for any reservation already made for the requested date.")

    phase = "open the browser with the saved session"
    submitted_or_pending = pending is not None
    try:
        if pending and result_file is not None:
            phase = "mark the saved submission as pending reconciliation"
            write_json(result_file, PENDING_RESULT)
            phase = "open the browser with the saved session"
        client.open_with_session(timezone_id=MADRID)
        form = ParkingForm(client.page)
        phase = "check whether Bookker requires sign-in"
        form.check_session()
        if pending:
            phase = "reload Bookings and reconcile the saved submission"
            records = form.bookings(client.base_url, date.fromisoformat(pending.date), reload=True)
            return verify_booking(records, pending, form, recovered=True)
        earliest = on_date or max(from_date or FIRST_DAY, FIRST_DAY, today)
        vehicle = None
        while True:
            phase = "open the parking form to check the requested date"
            vehicle = form.open(building, floor, vehicle)
            phase = "read the local date from Bookker's offered calendar"
            day = form.next_date(earliest, exact=on_date is not None)
            if day is None:
                if on_date:
                    return outcome("unavailable", "Bookker did not offer the requested local date.",
                                   "Check the requested date in the Bookker parking calendar.")
                return outcome("unavailable", "No eligible whole-day space was found in Bookker's offered window.",
                               "Retry when Bookker opens more dates or availability changes.")
            if on_date:
                if day != on_date:
                    raise BookingProblem("Bookker offered a different date from the exact date requested.",
                                         "Check the requested date in the Bookker parking calendar.")
            else:
                earliest = day + timedelta(days=1)
            phase = "read the account's Bookings for the candidate date"
            records = form.bookings(client.base_url, day, reload=False)
            if day in parking_dates(records, form):
                if on_date:
                    return outcome("unavailable", "Bookker already shows a parking booking for the requested date.",
                                   "Check the existing reservation in Bookker Bookings for that date.")
                continue
            phase = "reopen the parking form after checking Bookings"
            form.open(building, floor, vehicle)
            phase = "restore the candidate date after checking Bookings"
            if form.next_date(day, exact=on_date is not None) != day:
                raise BookingProblem("Bookker stopped offering the candidate date after the Bookings check.",
                                     "Check the offered date in the Bookker parking form before rerunning.")
            phase = "select one interval covering the displayed parking shifts"
            schedule = form.whole_day(expected_shifts)
            if schedule is None:
                if on_date:
                    required = "07:00–14:00 and 14:00–23:00" if expected_shifts else "all required parking shifts"
                    return outcome("unavailable", f"Bookker did not offer {required} on the requested date.",
                                   "Check shift availability for the requested date in the Bookker parking form.")
                continue
            span, all_day = schedule
            phase = "search for an available parking space"
            form.location("Building", building, select=False)
            form.location("Floor", floor, select=False)
            form.selected_vehicle(vehicle)
            form.check_repeat()
            if on_date:
                form.check_date(day)
            choice = form.select_space(building, floor, span, spot_name, spot_id,
                                       require_material=bool(expected_shifts))
            if choice is None:
                if on_date:
                    return outcome("unavailable", "No parking space covered the full interval on the requested date.",
                                   "Check availability for the full interval and requested location in Bookker.")
                continue
            phase = "verify the final parking selection"
            selection, submit = form.review(day, span, all_day, building, floor, choice, vehicle)
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
            records = form.bookings(client.base_url, day, reload=True)
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
