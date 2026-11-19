"""Preview and cancel one occurrence from the visible Bookings list."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import date

from parking_booking import (
    LOCATION_FIELD, MADRID, PARKING_TYPE, SIGN_IN, SPACE_CODE, SPACE_FIELD,
    BookingProblem, MissingBookingDetailsLink, ParkingForm, booking_references, card_locations, dates, displayed_spaces,
    field_values, intervals, location_pair_names, normalized, outcome, shift_span, shows_location, visible, write_json,
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
    combined.extend(card_locations(text))
    for label in combined:
        names = location_pair_names(label)
        if names and shows_location(text, *names):
            building, floor = names
            if PARKING_TYPE.search(building) and not PARKING_TYPE.search(floor):
                return floor, building
            return building, floor
    return None


def find_reservation(records, target, form, base_url, day):
    matches = []
    location = (target.building, target.floor) if target.building else None
    requested_space = normalized(target.space) if target.space else None
    for record in records:
        days = dates(record, form.day_first)
        if day and days and day not in days:
            continue
        refs = booking_references(record)
        if target.reference and refs and target.reference not in refs:
            continue
        if requested_space:
            labelled_spaces = {normalized(value) for value in field_values(record, SPACE_FIELD)}
            if (labelled_spaces and requested_space not in labelled_spaces
                    and requested_space not in displayed_spaces(record, form.day_first, location)):
                continue
            if not shows_location(record, *location):
                shown = booking_location(record)
                if shown and shows_location(record, *shown):
                    continue
        resolved = reservation_details(record, target, location, form, base_url, day)
        if resolved is not None:
            details, recurring = resolved
            matches.append((record, details, recurring))
            if len(matches) > 1:
                raise BookingProblem("More than one Bookings row matched the cancellation selector.", CANCEL_CHECK)
    return matches[0] if matches else None


def reservation_details(record, target, requested_location, form, base_url, day):
    def location_in(text):
        if requested_location and shows_location(text, *requested_location):
            return requested_location
        return booking_location(text)

    location = location_in(record)
    card_spaces = displayed_spaces(record, form.day_first, location or requested_location or ())
    days = dates(record, form.day_first)
    card_references = booking_references(record)
    references = card_references.copy()
    hours = intervals(record)
    linked = ""
    linked_spaces = {}
    needs_details = not (location and card_spaces and days) or (target.reference is not None and not references)
    if needs_details:
        row = form.booking_row(base_url, day, record)
        names = visible(row.locator(
            ".resource-name, .booking-name, [data-testid=booking-space], a .strong, [role=link] .strong"
        ))
        for name in names.all_inner_texts():
            name = name.strip()
            if re.fullmatch(SPACE_CODE, name):
                card_spaces.setdefault(normalized(name), name)
    if needs_details or (target.space and not references and card_spaces.keys() == {normalized(target.space)}):
        title = next(iter(card_spaces.values())) if len(card_spaces) == 1 else ""
        try:
            linked = form.booking_details(base_url, day, record, title)
        except MissingBookingDetailsLink:
            if needs_details:
                raise
        else:
            if not linked.strip():
                raise BookingProblem("Bookker did not display readable details for the matching reservation.",
                                     CANCEL_CHECK)
            references |= booking_references(linked)

    if target.reference and target.reference not in references:
        return None
    if linked:
        linked_location = location_in(linked)
        if linked_location and location and not shows_location(linked, *location):
            raise BookingProblem("The linked booking details showed a different location.", CANCEL_CHECK)
        location = location or linked_location
        linked_spaces = displayed_spaces(linked, form.day_first, location or ())
        days |= dates(linked, form.day_first)
        linked_hours = intervals(linked)
        if hours and linked_hours and hours != linked_hours:
            span = shift_span(hours)
            if span is None or span != shift_span(linked_hours):
                raise BookingProblem("The linked booking details showed different hours than the Bookings row.",
                                     CANCEL_CHECK)
        hours = hours or linked_hours

    spaces = linked_spaces | card_spaces
    if len(spaces) > 1:
        raise BookingProblem("A possible matching Bookings row showed conflicting spaces.", CANCEL_CHECK)
    if target.reference and not card_references and (
        not card_spaces or not (linked_spaces or list(field_values(record, SPACE_FIELD)))
    ):
        raise BookingProblem("The linked reference could not be tied to a verified space on the Bookings row.",
                             CANCEL_CHECK)
    space = next(iter(spaces.values()), "")
    if target.space and space and normalized(target.space) != normalized(space):
        return None
    if not location or not re.fullmatch(SPACE_CODE, space):
        raise BookingProblem("Bookker did not show the parking space and location for the selected reservation.",
                             CANCEL_CHECK)
    building, floor = location
    text = f"{record}\n{linked}"
    if (len(days) != 1 or (day is not None and days != {day}) or len(references) > 1
            or not shows_location(f"{text}\nBuilding: {building}\nFloor: {floor}", building, floor)):
        raise BookingProblem("A possible matching Bookings row had conflicting or incomplete details.", CANCEL_CHECK)
    if requested_location and location != requested_location:
        return None
    types = {normalized(value) for value in field_values(text, r"Type|Resource type|Tipo")}
    if not all(PARKING_TYPE.search(value) for value in (types or {floor})):
        raise BookingProblem("The selected reservation could not be verified as parking.", CANCEL_CHECK)
    reservation_day, = days
    details = {"date": reservation_day.isoformat(), "timezone": MADRID,
               "building": building, "floor": floor, "space": space}
    if references:
        details["confirmation_reference"] = references.pop()
    if hours:
        hours = sorted(hours)
        details.update(start_time=hours[0][0], end_time=hours[-1][1],
                       displayed_intervals=[list(span) for span in hours])
    recurrence = re.sub(r"(?im)^\s*(?:no repeat|repeat\s*:\s*none|sin repetición)\s*$", "", text)
    return details, bool(RECURRING.search(recurrence))


def cancellation_action(form, row, record, *, recurring):
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
        return single, dialogs, False
    delete = visible(menu.get_by_role("menuitem", name=DELETE).or_(menu.get_by_role("button", name=DELETE)))
    if delete.count() != 1 or not delete.is_enabled(timeout=1000) or single.count():
        raise BookingProblem("Bookker did not expose one Delete action in the selected row's menu.", CANCEL_CHECK)
    if recurring and delete.get_attribute("aria-haspopup", timeout=1000) != "dialog":
        raise BookingProblem("Bookker did not verify a single-occurrence Delete action for the recurring row.",
                             CANCEL_CHECK)
    return delete, dialogs, recurring


def confirm_cancellation(dialogs, details, *, day_first, needs_occurrence_choice):
    try:
        dialogs.wait_for(timeout=5000)
    except Exception:
        # Some row-level Delete controls act immediately.
        if not dialogs.count():
            if needs_occurrence_choice:
                raise BookingProblem("Bookker did not show a single-occurrence choice for the recurring row.",
                                     CANCEL_CHECK) from None
            return
        raise
    if dialogs.count() != 1:
        raise BookingProblem("Bookker showed multiple cancellation dialogs.", CANCEL_CHECK)
    text = dialogs.inner_text(timeout=1000)
    shown_days = dates(text, day_first)
    shown_references = booking_references(text)
    shown_location = list(field_values(text, LOCATION_FIELD)) or card_locations(text)
    expected_location = details["building"], details["floor"]
    shown_spaces = displayed_spaces(text, day_first, expected_location)
    shown_hours = intervals(text)
    known_hours = {tuple(span) for span in details.get("displayed_intervals", [])}
    if ((shown_days and shown_days != {date.fromisoformat(details["date"])})
            or (shown_spaces and shown_spaces.keys() != {normalized(details["space"])})
            or (shown_references and shown_references != {details.get("confirmation_reference")})
            or (shown_location and not shows_location(text, *expected_location))
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
          and not needs_occurrence_choice and not RECURRING.search(text)
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
        if attempted:
            if reservation_remaining(records, details, form, base_url, day):
                return result("unknown", "A cancellation intent already exists and the reservation is still present.")
            return result("cancelled", "The selected reservation was absent from reloaded Bookings.")

        selected = find_reservation(records, target, form, base_url, day)
        if selected is None:
            return result("absent", "No parking reservation matched in reloaded Bookings.")

        row_text, details, recurring = selected
        if not confirm:
            return {**result("preview", "One parking reservation matched."), "dry_run": True}
        if state is None:
            return result("blocked", "Cancellation requires a persistent submission state path.")
        show_preview(details)
        row = form.booking_row(base_url, day, row_text)
        action, dialogs, needs_occurrence_choice = cancellation_action(form, row, row_text, recurring=recurring)
        try:
            write_json(state, {"version": 1, "action": "cancel-one-occurrence", "target": asdict(target),
                               "reservation": details}, exclusive=True)
        except FileExistsError:
            return result("unknown", "Another run recorded the cancellation intent. Rerun to check Bookings.")
        attempted = True
        action_error = None
        try:
            action.click(timeout=5000)
            confirm_cancellation(dialogs, details, day_first=form.day_first,
                                 needs_occurrence_choice=needs_occurrence_choice)
            form.settled()
        except BookingProblem as error:
            action_error = error.reason
        except Exception:
            action_error = "The Bookker cancellation action could not be verified."
        day = date.fromisoformat(details["date"])
        records = form.bookings(base_url, day, reload=True)
        if not reservation_remaining(records, details, form, base_url, day):
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


def reservation_remaining(records, details, form, base_url, day):
    reference = details.get("confirmation_reference")
    location = details["building"], details["floor"]
    space = details["space"]
    if reference:
        saved_location = f"Building: {location[0]}\nFloor: {location[1]}"
        for record in records:
            shown_days = dates(record, form.day_first)
            if not shown_days or shown_days == {day}:
                continue
            references = booking_references(record)
            if not references:
                title = displayed_spaces(record, form.day_first, location).get(normalized(space))
                # Supply missing location fields so the check only rejects conflicts.
                if not title or not shows_location(f"{record}\n{saved_location}", *location):
                    continue
                linked = form.booking_details(base_url, day, record, title)
                references = booking_references(linked)
                if len(references) != 1 and reference not in references:
                    raise BookingProblem("The linked reference for a Bookings row with a different date "
                                         "could not be verified.", CANCEL_CHECK)
            if reference in references:
                raise BookingProblem("Reloaded Bookings showed the saved reference with a different date.",
                                     CANCEL_CHECK)
        by_reference = CancelTarget(details["date"], None, None, None, reference)
        if find_reservation(records, by_reference, form, base_url, day):
            return True
    by_location = CancelTarget(details["date"], *location, space, None)
    return find_reservation(records, by_location, form, base_url, day) is not None
