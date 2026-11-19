# Bookker parking CLI

Use a saved Bookker session with the configured Playwright browser. The booking and
cancellation commands use Europe/Madrid for dates and times.

## Book a whole day

Preview the Saturday request for NEWTON / Parking Superficie C.

```sh
python3 run_bookker.py book --building NEWTON --floor "Parking Superficie C" \
  --on-date 2026-11-21 --whole-day
```

Submit the same request with a fresh intent file in an existing directory.

```sh
python3 run_bookker.py book --building NEWTON --floor "Parking Superficie C" \
  --on-date 2026-11-21 --whole-day --confirm \
  --submission-state superficie-2026-11-21.intent.json \
  --result-file superficie-2026-11-21.result.json
```

That date requires Bookker to offer 07:00–14:00 and 14:00–23:00. For other dates
on this floor, the CLI checks the shifts displayed for the requested date. It
checks existing parking bookings and requires a named space for the full
interval. Reuse the same intent path if the result is uncertain. The CLI reloads
Bookings and checks the original reservation before reporting the outcome.

## Cancel one reservation

Preview with a reference, or replace the example space with the exact space
displayed in Bookings. A reference lookup without a date requires an all-dates
Bookings filter. Add `--on-date` if the account offers only bounded periods.

```sh
python3 run_bookker.py cancel --reference EXAMPLE-123 --on-date 2026-11-26

python3 run_bookker.py cancel --on-date 2026-11-26 --building NEWTON \
  --floor "Parking Torres Quevedo" --spot-name EXAMPLE-SPACE
```

Once the preview identifies the intended row, explicitly confirm with a separate
cancellation intent path. The selected reservation prints to standard error
before the action. The final JSON outcome prints to standard output.

```sh
python3 run_bookker.py cancel --on-date 2026-11-26 --building NEWTON \
  --floor "Parking Torres Quevedo" --spot-name EXAMPLE-SPACE --confirm \
  --submission-state cancellation.intent.json
```

Reuse that intent path after an interruption. A rerun checks for removal in
reloaded Bookings. If the selector still matches, the CLI reports an uncertain
result and leaves the row in place.

## Offline verification

```sh
make offline-parking-tests
```

The fixtures exercise the normal CLI using synthetic bookings and browser
controls. The Bookings DOM callback test also uses Node.js when available.
