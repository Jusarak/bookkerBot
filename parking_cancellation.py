"""Preview and cancel one occurrence from the visible Bookings list."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import date

from parking_booking import (
    MADRID, SIGN_IN, SPACE_CODE, SPACE_FIELD, BookingProblem, ParkingForm,
    booking_references, booking_space, dates, field_values, intervals,
    normalized, outcome, shows, shows_location, visible, write_json,
)

CANCEL_CHECK = "Check the selected reservation in Bookker Bookings."
MENU_TRIGGER = re.compile(r"^(?:more(?: actions| options|_vert|_horiz)?|"
                          r"booking actions|options|acciones|opciones|más opciones)$", re.I)
DELETE = re.compile(r"^(?:(?:delete|eliminar)\s+)?(?:Delete|Eliminar)(?: (?:this )?(?:booking|reservation|"
                    r"reserva))?$", re.I)
CONFIRM_DELETE = re.compile(
    rf"(?:{DELETE.pattern})|^(?:Confirm(?: deletion)?|Yes(?:,? delete)?|Confirmar|Sí(?:,? eliminar)?)$", re.I,
)
SINGLE = re.compile(r"^(?:(?:Delete|Eliminar) )?(?:Only this (?:booking|occurrence|reservation)|"
                    r"This (?:booking|occurrence|reservation)(?: only)?|Single occurrence|"
                    r"Solo esta (?:reserva|ocurrencia)|Esta (?:reserva|ocurrencia)(?: solamente)?)$", re.I)
RECURRING = re.compile(r"\b(?:series|recurring|repeat(?:ed|ing)?|all (?:occurrences|bookings)|"
                       r"repetir|repetici[oó]n|recurrente|todas las (?:reservas|ocurrencias))\b", re.I)


@dataclass(frozen=True)
class CancelTarget:
    date: str | None
    building: str | None
    floor: str | None
    space: str | None
    reference: str | None


def booking_location(text):
    buildings = set(field_values(text, r"Building|Edificio"))
    floors = set(field_values(text, r"Floor|Planta"))
    if len(buildings) == len(floors) == 1:
        return buildings.pop(), floors.pop()
    combined = list(field_values(text, r"Building|Edificio|Location|Ubicación", split_inline=False))
    combined.extend(re.findall(r"^\s*(?:location_on|place)\s*([^\n]+)", text, re.I | re.M))
    for label in combined:
        names = re.split(r"\s*[,/|·]\s*", label.strip().rstrip(","))
        if len(names) == 2 and all(names) and shows_location(text, *names):
            return tuple(names)
    return None


def find_reservation(records, target, *, day_first):
    matches = []
    incomplete = False
    day = date.fromisoformat(target.date) if target.date else None
    for record in records:
        days = dates(record, day_first)
        references = booking_references(record)
        if target.reference:
            if target.reference not in references:
                continue
            if len(references) != 1 or len(days) != 1 or (day is not None and days != {day}):
                incomplete = True
                continue
        else:
            if not shows(record, SPACE_FIELD, target.space):
                continue
            if days and day not in days:
                continue
            if not shows_location(record, target.building, target.floor):
                if not booking_location(record):
                    incomplete = True
                continue
            if days != {day} or len(references) > 1:
                incomplete = True
                continue
        matches.append(record)
    if len(matches) > 1:
        raise BookingProblem("More than one Bookings row matched the cancellation selector.", CANCEL_CHECK)
    if incomplete:
        raise BookingProblem("A possible matching Bookings row had conflicting or incomplete details.", CANCEL_CHECK)
    return matches[0] if matches else None


def reservation_details(record, target, form, base_url, day):
    location = (target.building, target.floor) if target.building else booking_location(record)
    space = target.space or booking_space(record)
    if target.reference and not (location and space):
        row = form.booking_row(base_url, day, record)
        name = visible(row.locator(".resource-name, .booking-name, [data-testid=booking-space]"))
        if not space and name.count() == 1:
            candidate = name.inner_text(timeout=1000).strip()
            if re.fullmatch(SPACE_CODE, candidate) and shows(record, SPACE_FIELD, candidate):
                space = candidate
    if not location or not space:
        raise BookingProblem("Bookker did not show the parking space and location for the selected reservation.",
                             CANCEL_CHECK)
    building, floor = location
    types = {normalized(value) for value in field_values(record, r"Type|Resource type|Tipo")}
    parking = re.compile(r"\b(?:parking|aparcamiento|estacionamiento|plaza)\b", re.I)
    if not all(parking.search(value) for value in (types or {floor})):
        raise BookingProblem("The selected reservation could not be verified as parking.", CANCEL_CHECK)
    reservation_day, = dates(record, form.day_first)
    references = booking_references(record)
    details = {"date": reservation_day.isoformat(), "timezone": MADRID,
               "building": building, "floor": floor, "space": space}
    if references:
        details["confirmation_reference"] = references.pop()
    hours = sorted(intervals(record))
    if hours:
        details.update(start_time=hours[0][0], end_time=hours[-1][1],
                       displayed_intervals=[list(span) for span in hours])
    return details


def cancellation_action(form, row, record):
    page = form.page
    menus = visible(page.get_by_role("menu"))
    dialogs = visible(page.get_by_role("dialog").or_(page.locator("mat-dialog-container, ion-modal")))
    trigger = visible(row.get_by_role("button", name=MENU_TRIGGER).or_(row.locator(
        'button[aria-haspopup="menu"], button:has(mat-icon:text-is("more_vert")), '
        'button:has(mat-icon:text-is("more_horiz"))'
    )))
    if (row.count() != 1 or row.inner_text(timeout=1000).strip() != record
            or menus.count() or dialogs.count() or trigger.count() != 1):
        raise BookingProblem("Bookker did not expose one action menu for the selected row.", CANCEL_CHECK)
    trigger.click(timeout=5000)
    menu = menus
    menu.wait_for(timeout=5000)
    controls = trigger.get_attribute("aria-controls", timeout=1000)
    if menu.count() != 1 or (controls and menu.get_attribute("id", timeout=1000) not in controls.split()):
        raise BookingProblem("The opened menu could not be tied to the selected row.", CANCEL_CHECK)
    items = menu.get_by_role("menuitem").or_(menu.get_by_role("button"))
    single = visible(items.and_(menu.get_by_text(SINGLE)))
    if single.count() == 1:
        return single, dialogs
    delete = visible(menu.get_by_role("menuitem", name=DELETE).or_(menu.get_by_role("button", name=DELETE)))
    if delete.count() != 1 or not delete.is_enabled(timeout=1000) or single.count():
        raise BookingProblem("Bookker did not expose one Delete action in the selected row's menu.", CANCEL_CHECK)
    recurrence = re.sub(r"(?im)^\s*(?:no repeat|repeat\s*:\s*none|sin repetición)\s*$", "", record)
    if RECURRING.search(recurrence) and delete.get_attribute("aria-haspopup", timeout=1000) != "dialog":
        raise BookingProblem("Bookker did not verify a single-occurrence Delete action for the recurring row.",
                             CANCEL_CHECK)
    return delete, dialogs


def confirm_cancellation(dialogs, details, *, day_first):
    try:
        dialogs.wait_for(timeout=5000)
    except Exception:
        # Some row-level Delete controls act immediately.
        if not dialogs.count():
            return
        raise
    if dialogs.count() != 1:
        raise BookingProblem("Bookker showed multiple cancellation dialogs.", CANCEL_CHECK)
    text = dialogs.inner_text(timeout=1000)
    shown_days = dates(text, day_first)
    shown_spaces = {normalized(value) for value in field_values(text, SPACE_FIELD)}
    shown_references = booking_references(text)
    shown_location = list(field_values(text, r"Building|Edificio|Floor|Planta|Location|Ubicación"))
    shown_hours = intervals(text)
    known_hours = {tuple(span) for span in details.get("displayed_intervals", [])}
    if ((shown_days and shown_days != {date.fromisoformat(details["date"])})
            or (shown_spaces and shown_spaces != {normalized(details["space"])})
            or (shown_references and shown_references != {details.get("confirmation_reference")})
            or (shown_location and not shows_location(text, details["building"], details["floor"]))
            or (shown_hours and shown_hours != known_hours)):
        raise BookingProblem("The cancellation dialog showed details for a different reservation.", CANCEL_CHECK)

    single_button = visible(dialogs.get_by_role("button", name=SINGLE))
    occurrence = visible(dialogs.get_by_role("radio", name=SINGLE))
    delete = visible(dialogs.get_by_role("button", name=CONFIRM_DELETE))
    if single_button.count() == 1 and not occurrence.count():
        action = single_button
    elif not single_button.count() and occurrence.count() == 1 and delete.count() == 1:
        if not occurrence.is_checked(timeout=1000):
            occurrence.check(timeout=5000)
        if not occurrence.is_checked(timeout=1000):
            raise BookingProblem("Bookker did not retain the single-occurrence choice.", CANCEL_CHECK)
        action = delete
    elif (not single_button.count() and not occurrence.count() and delete.count() == 1
          and not RECURRING.search(text)
          and re.search(r"\b(?:this (?:booking|reservation)|delete (?:booking|reservation)|(?:esta |eliminar )reserva)\b",
                        text, re.I)):
        action = delete
    else:
        raise BookingProblem("The cancellation dialog did not verify one occurrence to delete.", CANCEL_CHECK)
    if not action.is_enabled(timeout=1000):
        raise BookingProblem("Bookker did not enable the verified cancellation action.", CANCEL_CHECK)
    action.click(timeout=5000)


def cancel_parking(client, *, target, confirm, state, show_preview):
    attempted = False
    details = {}

    def result(status, reason):
        return {**outcome(status, reason, CANCEL_CHECK), **details}

    try:
        day = date.fromisoformat(target.date) if target.date else None
        if state is not None:
            try:
                claim = json.loads(state.read_bytes())
            except FileNotFoundError:
                pass
            else:
                if (claim.get("version") != 1 or claim.get("action") != "cancel-one-occurrence"
                        or claim.get("target") != asdict(target)):
                    return result("blocked", "The supplied cancellation state belongs to a different operation.")
                attempted = True
                details = claim["reservation"]
                day = date.fromisoformat(details["date"])

        client.open_with_session(timezone_id=MADRID)
        form = ParkingForm(client.page)
        base_url = client.base_url
        records = form.bookings(base_url, day, reload=True)
        row_text = find_reservation(records, target, day_first=form.day_first)
        if attempted and row_text is None and target.reference:
            row_text = find_reservation(records, stored_location(details), day_first=form.day_first)
        if row_text is None:
            if attempted:
                return result("cancelled", "The selected reservation was absent from reloaded Bookings.")
            return result("absent", "No parking reservation matched in reloaded Bookings.")
        if attempted:
            return result("unknown", "A cancellation intent already exists and the selector still matches a Bookings row.")

        details = reservation_details(row_text, target, form, base_url, day)
        if not confirm:
            return {**result("preview", "One parking reservation matched."), "dry_run": True}
        if state is None:
            return result("blocked", "Cancellation requires a persistent submission state path.")
        show_preview(details)
        row = form.booking_row(base_url, day, row_text)
        action, dialogs = cancellation_action(form, row, row_text)
        try:
            write_json(state, {"version": 1, "action": "cancel-one-occurrence", "target": asdict(target),
                               "reservation": details}, exclusive=True)
        except FileExistsError:
            return result("unknown", "Another run recorded the cancellation intent. Rerun to check Bookings.")
        attempted = True
        action_error = None
        try:
            action.click(timeout=5000)
            confirm_cancellation(dialogs, details, day_first=form.day_first)
            form.settled()
        except BookingProblem as error:
            action_error = error.reason
        except Exception:
            action_error = "The Bookker cancellation action could not be verified."
        day = date.fromisoformat(details["date"])
        records = form.bookings(base_url, day, reload=True)
        remaining = find_reservation(records, target, day_first=form.day_first)
        if remaining is None and target.reference:
            remaining = find_reservation(records, stored_location(details), day_first=form.day_first)
        if remaining is None:
            return result("cancelled", "The selected reservation was absent from reloaded Bookings.")
        return result("unknown", action_error or "The selected reservation is still present in reloaded Bookings.")
    except BookingProblem as error:
        return {**result("unknown" if attempted else "blocked", error.reason), "next_action": error.next_action}
    except FileNotFoundError:
        return {**result("unknown" if attempted else "blocked", "The saved Bookker session is missing."),
                "next_action": SIGN_IN}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return result("unknown" if attempted else "blocked",
                      "The cancellation state or Bookings results could not be verified.")
    except Exception:
        return result("unknown" if attempted else "blocked", "The Bookker Bookings operation could not be verified.")


def stored_location(details):
    return CancelTarget(details["date"], details["building"], details["floor"], details["space"], None)
