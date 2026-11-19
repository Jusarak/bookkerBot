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
sudo apt install python3-venv ca-certificates nftables curl
cd /home/marcos/workspace/code/bookkerBot
python3 -m venv .venv
.venv/bin/pip install 'playwright>=1.51' python-dotenv
sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium
timedatectl show -p NTPSynchronized --value
```

Confirm the last command prints `yes`. The unit orders startup after
`time-sync.target`. Configure the host's time synchronization service to wait
for that target to represent a synchronized clock.

The services default to bundled Chromium and the Bookker web application. If
you need a different browser channel or Bookker base URL, use matching
`PLAYWRIGHT_CHANNEL` and `BOOKKER_URL` values in both service environment files.
Use the Bookker origin reached after signing in. The existing CLI's
`save-session` can capture an initial session through ordinary sign-in and MFA
on a machine with a display. Move that state through a private channel and
install it under `/var/lib/bookker-parking/session.json` with mode `0600`, or
create the initial session using the phone flow below.

```sh
sudo install -d -m 0700 -o marcos -g marcos /var/lib/bookker-parking
sudo install -m 0600 -o marcos -g marcos /secure/path/session.json /var/lib/bookker-parking/session.json
```

To preview from the worker with operator-selected settings, use the following
command during the warm-up or release window, while the timer is disabled. The
preview uses the release gate and exact target, then stops at the reviewed form.
Check that the deployed Bookker UI shows and clears an availability loading
indicator on the Search button or inside `app-resources`. If the worker cannot
observe that sequence after activating Search, it exits before selecting
inventory.
For an arbitrary offered date, use the existing `run_bookker.py book` preview.

```sh
sudo -u marcos .venv/bin/python -B parking_release.py run --dry-run
```

## Phone renewal and notifications

The worker checks the installed session during warm-up of each selected
occurrence. A missing session or a Bookker sign-in redirect produces
`auth_required`. It attempts one email per installed session generation,
including one for a missing session. The alert names the target date and
release window. The worker records a durable claim before attempting delivery
to prevent duplicate mail even if the SMTP outcome is ambiguous. The journal
shows `expiry_alert` with `sent`, `suppressed`, `unconfigured`,
`delivery_unverified`, `session_replaced` or `state_error`. For an ambiguous
delivery, check Gmail and the SMTP relay, then use the test command below if
needed. A verified phone renewal gives the next worker a new generation. Each
worker opens fresh state from disk. There is no session keepalive timer.

Choose the intended Tailscale user login and its untagged Android device. The
examples use `https://debian-server.tail431acd.ts.net:8765/` and reserve port
`8765` for renewal on that host. Restrict access to that port in the tailnet
policy to the intended user. Tailscale Serve sets the backend identity header.
The service checks the exact identity on every page, action and viewer request.
The local nftables rule permits only root, which runs `tailscaled`, to connect
to the backend listener. Keep the rule loaded for the life of the socket.
[Tailscale Serve identity headers](https://tailscale.com/docs/features/tailscale-serve#identity-headers)

Create the two environment files as root with mode `0600`. Replace the example
values with the Serve URL, the Tailscale login and the relay settings. The mail
transport uses SMTP over implicit TLS, with certificate verification. If the
relay uses a login, set both `BOOKKER_SMTP_USER` and `BOOKKER_SMTP_PASSWORD`.

```sh
sudo touch /etc/bookker-parking.env /etc/bookker-renewal.env
sudo chmod 0600 /etc/bookker-parking.env /etc/bookker-renewal.env
sudoedit /etc/bookker-parking.env /etc/bookker-renewal.env
```

`/etc/bookker-parking.env` settings for email delivery:

```ini
BOOKKER_RENEWAL_URL=https://debian-server.tail431acd.ts.net:8765/
BOOKKER_SMTP_HOST=smtp.example.net
BOOKKER_SMTP_PORT=465
BOOKKER_SMTP_FROM=parking@example.net
BOOKKER_SMTP_TO=your-inbox@gmail.com
```

`/etc/bookker-renewal.env` settings for the private page:

```ini
BOOKKER_RENEWAL_URL=https://debian-server.tail431acd.ts.net:8765/
BOOKKER_TAILSCALE_LOGIN=your-tailnet-login@example.net
```

Install the renewal units and loopback rules. Keep the `include` directive in
the host firewall configuration so firewall reloads also load this rule.
Review the existing configuration before editing it and place the include after
any `flush ruleset`. An existing include for `/etc/nftables.d/*.nft` covers this
file. Keep the host's other firewall rules when editing the configuration.

```sh
sudo install -d -m 0755 /etc/nftables.d
sudo install -m 0644 deploy/bookker-renewal.nft /etc/nftables.d/bookker-renewal.nft
sudoedit /etc/nftables.conf
```

```nft
include "/etc/nftables.d/bookker-renewal.nft"
```

```sh
sudo nft --check -f /etc/nftables.conf
sudo systemctl enable --now nftables.service
sudo install -m 0644 deploy/bookker-renewal.{service,socket} /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/bookker-renewal.{service,socket}
sudo systemctl daemon-reload
sudo systemctl enable --now bookker-renewal.socket
sudo nft list table inet bookker_renewal
ss -ltn '( sport = :8765 )'
curl --max-time 3 -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/
```

Run that `curl` as the normal unprivileged user. The connection should fail.
From the host's `debian-server-hk` checkout, publish the loopback listener with
the existing Tailscale Serve script. Check the URL in `tailscale serve status`
and use it in both environment files.

```sh
sudo ./scripts/25-serve-app.sh 8765 bookker-renewal
tailscale serve status
```

From the Bookker checkout, use an operator-triggered transient unit to send a
real test to Gmail. Check the notification result and Gmail inbox or spam.

```sh
sudo systemd-run --unit=bookker-notification-test --wait --pipe --collect \
  -p User=marcos -p EnvironmentFile=/etc/bookker-parking.env \
  -p RuntimeMaxSec=60s \
  /home/marcos/workspace/code/bookkerBot/.venv/bin/python -B \
  /home/marcos/workspace/code/bookkerBot/parking_release.py notification-test
journalctl -u bookker-notification-test.service -u bookker-renewal.service -n 40 --no-pager -o cat
```

### Android smoke test

1. On the Android phone with Tailscale connected, open the Gmail message and
   follow its link in the phone browser. Opening the page should leave the
   browser helpers stopped. Check that a different tailnet login gets 403 or
   is denied by tailnet policy. With Tailscale disconnected the link should be
   unreachable.
2. Tap **Start sign-in**. Tap fields on the streamed Bookker screen, enter text
   in the phone field and tap **Type**. Swipe the streamed screen to scroll.
   Use the account's actual MFA method and return to the phone browser after
   approving a push or retrieving a code. Browser-local Android passkeys are
   tied to the phone's browser, while Bookker is running in server Chromium.
   If the IdP requests one, use an approved method the account offers for
   signing in to a remote browser. Verify this with the actual account before
   enabling booking.
3. Tap **Verify and save** once Bookker opens. A separate headless browser
   verifies that the candidate opens authenticated Bookings, then atomically
   replaces the saved state with a mode-`0600` file. Check the success message,
   journal, and file mode without reading its contents. Preview the next
   eligible run through the worker and verify it uses the renewed session.
4. Start another attempt and cancel it. Start a third and let it expire after
   five minutes. The session file's inode and timestamp should remain the same
   on these two attempts. The browser closes on each exit. After 15 seconds
   without an authorized request the HTTP service exits, leaving the systemd
   socket and Tailscale Serve to receive the next visit.

```sh
sudo stat -c '%a %U %i %y' /var/lib/bookker-parking/session.json
systemctl show bookker-renewal.service -p ActiveState -p Result -p MainPID
systemctl show bookker-renewal.socket -p ActiveState
journalctl -u bookker-renewal.service -n 20 --no-pager -o cat
```

### Replace the viewer

Set `BOOKKER_RENEWAL_VIEWER=module:factory` in the renewal environment file.
The default is `parking_viewer:BrowserViewer`. Stop the renewal service after a
configuration change so the next request starts it with the new setting.
The factory receives the Bookker base URL. The service awaits `start()` on an
explicit **Start sign-in**, `frame()` for JPEG bytes at 420 × 690, and
`input(action)` for a tap, scroll, text or permitted key. It awaits
`candidate()` for Playwright storage state on **Verify and save** and
`close()` after every attempt, including partial startup. The implementation
can raise `SignInIncomplete` for incomplete sign-in or a generic exception
for runtime failure. Its cleanup should be safe after task cancellation.
The service owns user authentication, the five-minute deadline, fresh-browser
validation and file installation. The built-in viewer uses in-memory browser
frames and page input. It has no exposed debugging port, operating-system
desktop or persistent browser profile.

## Install and inspect the booking timer

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

The worker caps browser waits by the remaining release window before submission.
A cutoff before any click reports `missed`. If Bookker offers a separate
confirmation after the first click, the worker waits for its verified control
to become enabled within the same release window. Once any click could have
reached Bookker, the worker gives the Bookings readback a separate 20-second
budget, including when the release window has closed.

For `uncertain`, keep the date-keyed intent and sidecars under
`/var/lib/bookker-parking/` and check that target in Bookings. A repeated trigger
within its release window reloads Bookings to reconcile the saved selection. The
worker exits `uncertain` when the readback is inconclusive and retains the same
intent for review. `error` and `uncertain` produce a failed oneshot result.
If Bookker asks for sign-in during readback, the worker also logs
`expiry_alert` and uses the normal one-alert-per-session policy.

## Disable and roll back

```sh
sudo systemctl disable --now bookker-parking.timer
sudo systemctl stop bookker-parking.service
sudo rm /etc/systemd/system/bookker-parking.{timer,service}
sudo systemctl daemon-reload
```

To unpublish renewal, run `sudo ./scripts/25-serve-app.sh --off 8765` in the
server configuration checkout. Then remove its socket and service. Remove the
explicit nftables include if you added one for this service. A wildcard include
can stay in place.

```sh
sudo systemctl disable --now bookker-renewal.socket
sudo systemctl stop bookker-renewal.service
sudo rm /etc/systemd/system/bookker-renewal.{socket,service}
sudo systemctl daemon-reload
sudoedit /etc/nftables.conf
sudo rm /etc/nftables.d/bookker-renewal.nft
sudo nft delete table inet bookker_renewal
```

Keep `/var/lib/bookker-parking/` private so a later install can reconcile old
intents and pick up the existing session. Use the existing CLI cancellation
workflow for a separately authorized cancellation in Bookker.

## Offline evidence

```sh
make offline-parking-tests offline-release-tests offline-renewal-tests offline-release-trace
python3 -B parking_release.py timer --example
systemd-analyze calendar --base-time='2026-11-22 22:00:00 UTC' \
  'Wed,Sun *-*-* 23:59:30 Europe/Madrid'
```

The example schedules Monday and Thursday at 00:00:30 Madrid. The calendar's
next wake in this example is Sunday 2026-11-22 at 23:59:30 CET
(22:59:30 UTC), for Monday's release and a target of 2026-11-30. The fixture
runs `parking_release.py run` with a synthetic clock and one browser context.
The production date, shift, search and review methods wait for fixture
controls. Warm-up finished at −57000 ms. The exact-date action ran at 0 ms,
fresh search at +18 ms, and submission at +60 ms, the instant the fixture
enabled the verified control. Confirmation from reloaded Bookings and worker
exit followed at +80 ms. The fixture rejects any post-release network-idle
wait and checks that the intent exists at the click. The offsets are synthetic
and exclude real Playwright, disk and network overhead. Use journal offsets
to measure the host after deployment.

In the delayed search fixture, Bookker recreates the cached rows with identical
contents while the fresh search is in flight. The worker uses the warmed browser
to wait for the loading cycle triggered by Search for the verified date,
location and shifts. The fixture returns fresh inventory 150 ms after Search.
Submission follows at +175 ms, when the verified form becomes ready. A cached
empty message also stays gated until search completion. Reloaded Bookings
confirms the saved 07:00–23:00 selection when the record lists 07:00–14:00 and
14:00–23:00 separately.

A search dispatched 40 ms before cutoff exits as `missed` at cutoff without
an intent. A separate confirmation fixture dispatches at +70 ms, 10 ms after
the first click, when its control becomes enabled. The durable final
confirmation claim exists at the click. The expiry-on-readback fixture keeps
the intent and returns `uncertain` with a failing exit status, sending one
synthetic alert across two attempts.

The phone fixture runs the renewal entry point, dispatches HTTP requests
through the production handler and substitutes a local identity provider and
synthetic browser contexts. It verifies link previews, identity checks on the
viewer, cancellation, timeout, invalid candidates, mode-`0600` replacement,
viewer substitution and browser cleanup. The scheduled entry point sends a
synthetic Gmail message once per expired state, suppresses the second alert,
and picks up the newly installed state on its next run. Real relay delivery,
Android MFA, host activation and live Bookker submission are operator checks.

The Debian 13 `systemd-analyze calendar` check ran offline. The controller
recorded both required checks passing with exit code 0 at
`654fb940e51553f7637ff41c1e86a8a22791ba29`.

- `offline-tests` ran `/usr/bin/python3 -B -m unittest discover`.
- `offline-systemd-units` ran `systemd-analyze verify` for
  `bookker-parking.service`, `bookker-renewal.socket`, `bookker-renewal.service`
  and the worker-generated example timer in a disposable Debian 13 root.
  The fixture supplied the host unit dependencies and executables at the
  configured paths, including `/usr/sbin/nft` and
  `/home/marcos/workspace/code/bookkerBot/.venv/bin/python`.

The earlier disposable-root verification used systemd 257.13. A separate
attempt in the implementation sandbox stopped during working-directory setup
with `Read-only file system`. Installation, startup and the unit, firewall and
Tailscale checks on the target host are operator steps.
