# run_bookker.py
import argparse, os
from bookker_bot import BookkerClient

def main():
    ap = argparse.ArgumentParser(description="Bookker helper")
    ap.add_argument("cmd", choices=["save-session","list","book"], help="Action to perform")
    ap.add_argument("--url", default=os.environ.get("BOOKKER_URL","https://webapp.bookkercorp.com/"))
    ap.add_argument("--auth", default="auth_state.json", help="Path to storage state JSON")
    ap.add_argument("--api-host-hint", default=os.environ.get("BOOKKER_API_HOST_HINT"))
    ap.add_argument("--headful", action="store_true", help="Run with visible browser")
    ap.add_argument("--slowmo", type=int, default=0, help="Slow motion ms")
    ap.add_argument("--floor", default=os.environ.get("BOOKKER_FLOOR_LABEL"))
    ap.add_argument("--spot-name")
    ap.add_argument("--spot-id")
    ap.add_argument("--confirm", action="store_true", help="Actually click the final Book/Reserve button")
    args = ap.parse_args()

    client = BookkerClient(
        base_url=args.url,
        auth_state_path=args.auth,
        api_host_hint=args.api_host_hint,
        headless=not args.headful,
        slow_mo_ms=args.slowmo,
    )

    try:
        if args.cmd == "save-session":
            client.save_session()
            return

        client.open_with_session()

        if args.cmd == "list":
            spots = client.read_availability(floor_label=args.floor)
            if not spots:
                print("No spots captured yet. Check the JSON files under ./captures and refine url matchers/paths.")
                return
            print("\nOpen (or unknown) spots:")
            for s in spots:
                if s.available is True or s.available is None:
                    print(f"- {s.name or s.id}   id={s.id}   avail={s.available}")
            return

        if args.cmd == "book":
            result = client.book(spot_name=args.spot_name, spot_id=args.spot_id, confirm_click=args.confirm)
            print("Booking result:", result)
            if not args.confirm:
                print("(Dry run — pass --confirm to actually click the final button.)")
            return

    finally:
        client.close()

if __name__ == "__main__":
    main()
