# Scheduled parking on Debian 13

The booking worker runs as `marcos` on the `debian-server-hk` host. It follows
the finAnalyser service convention of a oneshot, a separate calendar timer and
JSON diagnostics in journald. The booking worker opens outbound connections and
systemd owns the wake-up. Each run closes its Playwright context and browser.
The service has a three-minute hard limit, followed by at most five seconds for
termination of its process group. This includes bounded time for Bookings
readback after a submission.

## Schedule and settings

Edit `PRODUCTION` in [`parking_schedule.py`](../parking_schedule.py). Set all
four choices before installation.

- `release_time` is a `time(hour, minute, second)` in Europe/Madrid.
- `parking_weekdays` is a tuple of weekday numbers. Monday is `0` and Sunday
  is `6`. For a single day, include the trailing comma. The release date and
  the parking date share the same weekday.
- `building` and `floor` are the exact labels for the recurring reservation.
  Confirm those preferences on the account. `NEWTON` and `Parking Torres
  Quevedo` are example labels.

The checked-in production choices leave the worker disabled. The worker takes
the target date from the intended Madrid release date plus seven calendar days,
including when the timer wakes on the previous day. The wake is 60 elapsed
seconds before release. The window for starting and dispatching a submission
closes 60 elapsed seconds after release. A stale wake exits before starting
Playwright. The timer uses a 1 µs accuracy setting, zero randomized delay and
no persistence. The worker also checks the wall clock before each dispatch.

A release in the spring DST gap has no occurrence. The autumn repeated hour
uses the first occurrence. For a Sunday release just after 03:00, the generator
adds the elapsed-time wakes for the current Madrid spring and autumn transition
rules. The ordinary calendar wake can also fire an hour early on that autumn
Sunday, when the worker exits as `missed` before browser startup. If Madrid's
DST rules change, recheck the generated transition calendars against host tzdata.

## Prepare the host

Run installation and activation on the Debian host. The unit expects the
checkout at `/home/marcos/workspace/code/bookkerBot`. Adjust `User`, `Group`,
and both checkout paths in `deploy/bookker-parking.service` together if the
host differs. Install with the unprivileged user that owns the checkout.

```sh
sudo apt install python3-venv ca-certificates
cd /home/marcos/workspace/code/bookkerBot
python3 -m venv .venv
.venv/bin/pip install playwright python-dotenv
sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium
timedatectl show -p NTPSynchronized --value
```

Confirm the last command prints `yes`. The unit orders startup after
`time-sync.target`. Configure the host's time synchronization service to wait
for that target to represent a synchronized clock.

The worker defaults to bundled Chromium and the Bookker web application. If the
host requires a different browser channel or Bookker base URL, provide
`PLAYWRIGHT_CHANNEL` and `BOOKKER_URL` in `/etc/bookker-parking.env`. Use the
base URL for the final Bookker origin after signing in. The existing CLI's
`save-session` can capture an initial session through ordinary sign-in and MFA
on a machine with a display. Move that state through a private channel and
install it under `/var/lib/bookker-parking/session.json` with mode `0600`.

```sh
sudo install -d -m 0700 -o marcos -g marcos /var/lib/bookker-parking
sudo install -m 0600 -o marcos -g marcos /secure/path/session.json /var/lib/bookker-parking/session.json
```

To preview from the worker with operator-selected settings, use the following
command during the warm-up or release window, while the timer is disabled. The
preview uses the release gate and exact target, then stops at the reviewed form.
For an arbitrary offered date, use the existing `run_bookker.py book` preview.

```sh
sudo -u marcos .venv/bin/python -B parking_release.py run --dry-run
```

## Install and inspect

After editing `PRODUCTION`, generate the timer from that checkout again on each
schedule change. Review the timer and check every printed `OnCalendar` line.
With the units installed, `systemd-analyze verify` checks the actual paths and
dependencies on the host.

```sh
release_units=$(mktemp -d)
.venv/bin/python -B parking_release.py timer > "$release_units/bookker-parking.timer"
cat "$release_units/bookker-parking.timer"
sed -n 's/^OnCalendar=//p' "$release_units/bookker-parking.timer" |
  while IFS= read -r calendar; do systemd-analyze calendar --iterations=3 "$calendar"; done
sudo install -m 0644 deploy/bookker-parking.service /etc/systemd/system/bookker-parking.service
sudo install -m 0644 "$release_units/bookker-parking.timer" /etc/systemd/system/bookker-parking.timer
rm "$release_units/bookker-parking.timer"
rmdir "$release_units"
sudo systemd-analyze verify /etc/systemd/system/bookker-parking.{service,timer}
sudo systemctl daemon-reload
sudo systemctl enable --now bookker-parking.timer
sudo systemctl restart bookker-parking.timer
systemctl list-timers --all bookker-parking.timer
```

Inspect the last result and check for idle workers after the run.

```sh
systemctl show bookker-parking.service -p ActiveState -p Result -p ExecMainStatus -p MainPID
journalctl -u bookker-parking.service -n 40 --no-pager -o cat
pgrep -af '[p]arking_release.py run'
```

Between runs `MainPID=0` and `pgrep` produces no matches. `wake` records actual
process entry, `prepared` marks completion of read-only warm-up, `date_dispatch`
marks the exact-date action, and `refresh_dispatch` marks the fresh space
search. `form_refresh_dispatch` appears if the warmed calendar still has a
stale horizon and the worker reloads it once in the same browser context.
`submit_dispatch` and any `final_submit_dispatch` are timestamps at the local
Playwright call. `confirmation_observed` records the local observation from
reloaded Bookings. Subtract the planned offset of −60000 ms from the `wake`
offset to measure worker startup delay. Subtract `submit_dispatch` from
`confirmation_observed` to measure the elapsed time to local verification,
including Bookker and UI/readback latency. The remote confirmation time and
availability of any particular space depend on Bookker.

For `uncertain`, keep the date-keyed intent and sidecars under
`/var/lib/bookker-parking/` and check that target in Bookings. A repeated trigger
within its release window reloads Bookings to reconcile the saved selection. The
worker exits `uncertain` when the readback is inconclusive and retains the same
intent for review. `error` and `uncertain` produce a failed oneshot result.

## Disable and roll back

```sh
sudo systemctl disable --now bookker-parking.timer
sudo systemctl stop bookker-parking.service
sudo rm /etc/systemd/system/bookker-parking.{timer,service}
sudo systemctl daemon-reload
```

Keep `/var/lib/bookker-parking/` private so a later install can reconcile old
intents and pick up the existing session. Use the existing CLI cancellation
workflow for a separately authorized cancellation in Bookker.

## Offline evidence

```sh
make offline-parking-tests offline-release-tests offline-release-trace
python3 -B parking_release.py timer --example
systemd-analyze calendar --base-time='2026-11-22 22:00:00 UTC' \
  'Wed,Sun *-*-* 23:59:30 Europe/Madrid'
```

The example schedules Monday and Thursday at 00:00:30 Madrid. The calendar's
next wake in this example is Sunday 2026-11-22 at 23:59:30 CET
(22:59:30 UTC), for Monday's release and a target of 2026-11-30. The fixture
runs `parking_release.py run` with a synthetic clock and browser. It completed
warm-up at −57000 ms and issued the date action at 0 ms, search at +23 ms and
submission at +58 ms. Confirmation and worker exit were observed at +78 ms.
It asserted that the reviewed selection and submission shared the same fake
clock instant and that the intent was already on disk at the click. These
offsets describe fixture behavior. Use the journal to measure actual host
and Bookker timing after deployment.

The Debian 13 `systemd-analyze calendar` check ran offline. In the implementation
sandbox, `systemd-analyze verify` returned `Failed to setup working directory:
Read-only file system` even for an installed system unit. Complete unit
verification and activation on the target host.
