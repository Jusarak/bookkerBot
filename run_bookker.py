import argparse
import json
import os
from datetime import date
from pathlib import Path

from bookker_bot import BookkerClient
from parking_booking import SUBMISSION_SIDECARS, write_json


def iso_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return parsed
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use YYYY-MM-DD")


def main():
    ap = argparse.ArgumentParser(description="Bookker helper")
    ap.add_argument("cmd", choices=["save-session", "list", "book"], help="Action to perform")
    ap.add_argument("--url")
    ap.add_argument("--auth", default="auth_state.json", help="Path to storage state JSON")
    ap.add_argument("--api-host-hint")
    ap.add_argument("--headful", action="store_true", help="Run with visible browser")
    ap.add_argument("--slowmo", type=int, default=0, help="Slow motion ms")
    ap.add_argument("--building")
    ap.add_argument("--floor")
    ap.add_argument("--from-date", type=iso_date, metavar="YYYY-MM-DD", help="Earliest day to search")
    ap.add_argument("--on-date", type=iso_date, metavar="YYYY-MM-DD",
                    help="Book only this date, overriding --from-date")
    ap.add_argument("--whole-day", action="store_true", help="Cover all displayed parking shifts")
    ap.add_argument("--spot-name")
    ap.add_argument("--spot-id")
    ap.add_argument("--confirm", action="store_true", help="Submit the verified booking instead of a dry run")
    ap.add_argument("--submission-state", type=Path, help="Persistent submission intent shared by retries")
    ap.add_argument("--result-file", type=Path, help="Write a redacted booking result as JSON")
    args = ap.parse_args()

    if args.cmd == "book":
        paths = [Path(args.auth), args.submission_state, args.result_file]
        paths = [path.resolve() for path in paths if path is not None]
        if args.submission_state is not None:
            try:
                paths.extend(args.submission_state.with_suffix(suffix).resolve() for suffix in SUBMISSION_SIDECARS)
            except ValueError:
                ap.error("Use a file path for --submission-state.")
        protected = {Path.cwd() / ".env", Path.cwd() / "auth_state.json"}
        if len(set(paths)) != len(paths) or protected.intersection(paths[1:]):
            ap.error("Use distinct paths for --auth, --submission-state, its sidecars and --result-file. "
                     "Keep output separate from session and environment files.")
        if args.result_file is not None:
            try:
                write_json(args.result_file, {
                    "outcome": "blocked",
                    "reason": "The booking run did not complete.",
                    "next_action": "Check the submission state and Bookker Bookings before retrying.",
                })
            except OSError:
                ap.exit(1, "Could not write the redacted result file. Check its path and permissions.\n")
    else:
        from dotenv import load_dotenv

        load_dotenv()

    client = BookkerClient(
        base_url=args.url or os.environ.get("BOOKKER_URL", "https://webapp.bookkercorp.com/"),
        auth_state_path=args.auth,
        api_host_hint=args.api_host_hint or os.environ.get("BOOKKER_API_HOST_HINT"),
        headless=not args.headful,
        slow_mo_ms=args.slowmo,
        channel=os.environ.get("PLAYWRIGHT_CHANNEL", "msedge"),
    )
    floor = args.floor or os.environ.get("BOOKKER_FLOOR_LABEL")

    try:
        if args.cmd == "book":
            result = client.book(
                building=args.building, floor=floor, from_date=args.from_date, on_date=args.on_date,
                whole_day=args.whole_day, spot_name=args.spot_name, spot_id=args.spot_id,
                submission_state=args.submission_state, confirm_click=args.confirm,
                result_file=args.result_file,
            )
            if args.result_file is not None:
                try:
                    write_json(args.result_file, result)
                except OSError:
                    ap.exit(1, "Could not update the redacted result file. Check Bookker Bookings before retrying.\n")
            print(json.dumps(result, ensure_ascii=False))
            return

        if args.cmd == "save-session":
            client.save_session()
            return

        client.open_with_session()
        spots = client.read_availability(floor_label=floor)
        if not spots:
            print("No spots captured yet. Check the JSON files under ./captures and refine url matchers/paths.")
            return
        print("\nOpen (or unknown) spots:")
        for spot in spots:
            if spot.available is True or spot.available is None:
                print(f"- {spot.name or spot.id}   id={spot.id}   avail={spot.available}")

    finally:
        client.close()


if __name__ == "__main__":
    main()
