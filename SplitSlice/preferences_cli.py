"""
The preference database from the command line:

  python preferences_cli.py import-json        import data/users/*.json (safe to rerun)
  python preferences_cli.py users              users and their order counts
  python preferences_cli.py show jane_doe      stated + predicted preferences
  python preferences_cli.py show newbie --zip 95060 --state CA   a new user's starting point there
  python preferences_cli.py set jane_doe --topping pepperoni=0.9 --size-priority 0.2
  python preferences_cli.py unset jane_doe --topping pepperoni --size-priority
  python preferences_cli.py contacts add jane_doe alice bob     (also: list, remove)

Scores run 0-1; 0.7 or more is high priority, which Standard never gives up.
"""

import argparse
import sys
from pathlib import Path

from agents.preference_agent import PreferenceAgent, as_user_id, prior_applies
from models import SIZE_ORDER, Location, UserPreferences
from storage import PreferenceStore, StorageError, clean_state, clean_zip

LEGACY_DIR = Path(__file__).resolve().parent / "data" / "users"


def cmd_import_json(store: PreferenceStore, args) -> int:
    folder = Path(args.dir)
    if not folder.is_dir():
        print(f"No such folder: {folder}")
        return 1
    files = sorted(folder.glob("*.json"))
    if not files:
        print(f"No .json files in {folder}")
        return 0
    failed = 0
    for path in files:
        try:
            count = store.import_legacy_json(path)
        except (ValueError, StorageError) as e:
            failed += 1
            print(f"  {path.name}: FAILED -- {e}")
            continue
        except Exception as e:  # one unreadable file shouldn't stop the rest
            failed += 1
            print(f"  {path.name}: FAILED -- unexpected {type(e).__name__}: {e}")
            continue
        if count is None:
            print(f"  {path.name}: already imported, skipped")
        else:
            print(f"  {path.name}: imported {count} order(s) for user '{path.stem}'")
    return 1 if failed else 0


def cmd_users(store: PreferenceStore, args) -> int:
    users = store.list_users()
    if not users:
        print("No users yet.")
    for user_id, orders in users:
        print(f"  {user_id}  ({orders} order{'s' if orders != 1 else ''})")
    return 0


def cmd_show(store: PreferenceStore, args) -> int:
    agent = PreferenceAgent(store)
    recorded = store.get_recorded_preferences(args.user)
    zip_code, state = getattr(args, "zip", None), getattr(args, "state", None)
    if (zip_code and not clean_zip(zip_code)) or (state and not clean_state(state)):
        print("Use a 5-digit --zip and a 2-letter --state.")
        return 1
    location = None
    if zip_code or state:
        # Their last delivery location, with just what was given changed.
        last = store.latest_locations().get(args.user) or Location()
        new_zip = clean_zip(zip_code) or last.zip
        location = Location(zip=new_zip, state=clean_state(state) or last.state,
                            store_id=last.store_id if new_zip == last.zip else None)
    prefs = agent.predict_preferences(args.user, location)
    usual = agent.usual_order(args.user)

    def tag(score: float, is_recorded: bool) -> str:
        level = "high" if UserPreferences.is_high_priority(score) else "low"
        return f"{score:.2f} ({level}{', recorded' if is_recorded else ''})"

    print(f"{args.user}: {prefs.order_count} order(s) on record")
    if prefs.prior:
        print(f"  starting point: {prefs.prior.source} -- the averages of {prefs.prior.people} "
              f"people's {prefs.prior.orders} orders, which your own orders gradually replace")
        if prefs.prior.typical_budget is not None:
            print(f"  people near you usually spend: ${prefs.prior.typical_budget:.2f}")
    elif prior_applies(store.load_order_history(args.user)):
        print("  starting point: neutral -- not enough orders from people nearby yet")
    print(f"  preferred size: {prefs.preferred_size or 'unknown'}"
          f"{' (recorded)' if recorded.preferred_size else ''}")
    print(f"  size priority:  {tag(prefs.size_priority, recorded.size_priority is not None)}")
    if prefs.topping_preferences:
        print("  toppings:")
        for name, pref in sorted(prefs.topping_preferences.items()):
            print(f"    {name:<12} {tag(pref.score, name in recorded.toppings)}")
    else:
        print("  toppings:       none yet")
    budget = f"${prefs.typical_budget:.2f}" if prefs.typical_budget is not None else "unknown"
    print(f"  typical budget: {budget}")
    print(f"  price sensitivity: {prefs.price_sensitivity:.2f} "
          f"(recommends {prefs.recommended_mode().value.capitalize()} retrieval)")
    if usual:
        print("  usual order: " + ", ".join(f"{item.qty} x {item.name}" for item in usual))
    else:
        print("  usual order: none yet")
    return 0


def cmd_set(store: PreferenceStore, args) -> int:
    if not (args.topping or args.size_priority is not None or args.preferred_size):
        print("Nothing to set -- pass --topping, --size-priority, and/or --preferred-size.")
        return 1
    store.set_recorded_preferences(args.user, toppings=dict(args.topping or []),
                                   size_priority=args.size_priority,
                                   preferred_size=args.preferred_size)
    print(f"Saved. Current preferences for {args.user}:")
    return cmd_show(store, args)


def cmd_unset(store: PreferenceStore, args) -> int:
    if not (args.topping or args.size_priority or args.preferred_size):
        print("Nothing to unset -- pass --topping, --size-priority, and/or --preferred-size.")
        return 1
    store.unset_recorded_preferences(args.user, toppings=args.topping or [],
                                     size_priority=args.size_priority,
                                     preferred_size=args.preferred_size)
    print(f"Done. Current preferences for {args.user}:")
    return cmd_show(store, args)


def cmd_contacts(store: PreferenceStore, args) -> int:
    if args.action == "list":
        contacts = sorted(store.load_contacts(args.user))
        print(f"{args.user}'s contacts: {', '.join(contacts) if contacts else 'none yet'}")
        return 0
    if not args.people:
        print(f"Name at least one person to {args.action}.")
        return 1
    # Names match user IDs ignoring case and spacing; those added must already use SplitSlice.
    if args.action == "add":
        pool = [user for user, _ in store.list_users()]
    else:
        pool = sorted(store.load_contacts(args.user))
    by_key = {as_user_id(user): user for user in pool}
    found = [by_key[as_user_id(name)] for name in args.people if as_user_id(name) in by_key]
    unknown = [name for name in args.people if as_user_id(name) not in by_key]
    if unknown:
        what = "No such user" if args.action == "add" else f"Not one of {args.user}'s contacts"
        print(f"{what}: {', '.join(unknown)}")
    if args.action == "add":
        added = store.add_contacts(args.user, found, source="manual") if found else []
        print(f"Added: {', '.join(added) if added else 'nobody new'}")
    else:
        removed = store.remove_contacts(args.user, found) if found else []
        print(f"Removed: {', '.join(removed) if removed else 'nobody'}")
    return 1 if unknown else 0


def _topping_score(text: str) -> tuple:
    name, sep, score = text.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(f"expected NAME=SCORE, e.g. pepperoni=0.9, got {text!r}")
    try:
        return name.strip().lower(), float(score)
    except ValueError:
        raise argparse.ArgumentTypeError(f"score must be a number from 0 to 1, got {score!r}") from None


def main(argv=None) -> int:
    # Don't crash on names a redirected console can't encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Manage SplitSlice's preference database.")
    parser.add_argument("--db", help="database file, relative to the current folder "
                                     "(default: $SPLITSLICE_DB or data/splitslice.db)")
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser("import-json", help="import old data/users/*.json files")
    p.add_argument("--dir", default=str(LEGACY_DIR), help="folder of <user_id>.json files")
    p.set_defaults(run=cmd_import_json)

    p = commands.add_parser("users", help="list users")
    p.set_defaults(run=cmd_users)

    p = commands.add_parser("show", help="show a user's recorded and predicted preferences")
    p.add_argument("user")
    p.add_argument("--zip", help="preview from this delivery ZIP (default: their last order's)")
    p.add_argument("--state", help="preview from this 2-letter state (default: their last order's)")
    p.set_defaults(run=cmd_show)

    p = commands.add_parser("contacts", help="list, add, or remove a user's contacts")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("user")
    p.add_argument("people", nargs="*", help="user IDs to add or remove")
    p.set_defaults(run=cmd_contacts)

    p = commands.add_parser("set", help="record explicit preferences (override learned ones)")
    p.add_argument("user")
    p.add_argument("--topping", action="append", type=_topping_score, metavar="NAME=SCORE",
                   help="topping priority from 0 to 1; repeat for more toppings")
    p.add_argument("--size-priority", type=float, metavar="SCORE",
                   help="how strongly to keep the requested size, from 0 to 1")
    p.add_argument("--preferred-size", choices=SIZE_ORDER)
    p.set_defaults(run=cmd_set)

    p = commands.add_parser("unset", help="forget recorded preferences, so learned ones apply")
    p.add_argument("user")
    p.add_argument("--topping", action="append", metavar="NAME", help="repeat for more toppings")
    p.add_argument("--size-priority", action="store_true")
    p.add_argument("--preferred-size", action="store_true")
    p.set_defaults(run=cmd_unset)

    args = parser.parse_args(argv)
    # --db is relative to where you are; SPLITSLICE_DB to the project.
    db_path = Path(args.db).resolve() if args.db else None
    try:
        return args.run(PreferenceStore(db_path), args)
    except (StorageError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
