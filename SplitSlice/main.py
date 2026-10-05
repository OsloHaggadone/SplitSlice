"""SplitSlice's command-line entry point: python main.py [--user USER_ID].

Customer details come from .env (see .env.example); anything missing falls
back to the placeholders in DETAILS.
"""

import argparse
import os
import sys

import requests
from dotenv import load_dotenv

from agents.orchestrator import Orchestrator
from background import Background
from models import Location
from providers.base import StoreNotFound
from providers.dominos import DominosProvider
from storage import clean_state, clean_zip

load_dotenv()

DETAILS = {  # .env variable -> placeholder if it's not set
    "SPLITSLICE_FIRST_NAME": "Jane",
    "SPLITSLICE_LAST_NAME": "Doe",
    "SPLITSLICE_EMAIL": "jane.doe@example.com",
    "SPLITSLICE_PHONE": "5555555555",
    "SPLITSLICE_STREET": "351A Western Dr",
    "SPLITSLICE_CITY": "Santa Cruz",
    "SPLITSLICE_STATE": "CA",
    "SPLITSLICE_ZIP": "95060",
}


def announce_store(store) -> None:
    data = getattr(store, "data", None) or {}
    address = " ".join(str(data.get("AddressDescription", "")).split())
    print(f"Ordering from Domino's #{getattr(store, 'id', '?')}" + (f" ({address})" if address else ""))
    if getattr(store, "closed_now", False):
        hours = " ".join(str(data.get("HoursDescription", "")).split())
        print(f"[It's closed right now{f' (hours: {hours})' if hours else ''}, but you can still build "
              "and price an order to plan ahead.]")


def main() -> int:
    # Don't crash on menu names a redirected console can't encode.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description="Order pizza with SplitSlice.")
    parser.add_argument("--user", default="jane_doe",
                        help="whose saved preferences and order history to use (default: jane_doe)")
    args = parser.parse_args()
    user_id = args.user.strip()
    if not user_id:
        parser.error("--user can't be empty")

    details = {key: os.environ.get(key) or default for key, default in DETAILS.items()}
    if any(not os.environ.get(key) for key in DETAILS):
        print("[Using placeholder customer details -- set the SPLITSLICE_* values in .env to use yours]")
    customer_info = {
        "first_name": details["SPLITSLICE_FIRST_NAME"],
        "last_name": details["SPLITSLICE_LAST_NAME"],
        "email": details["SPLITSLICE_EMAIL"],
        "phone": details["SPLITSLICE_PHONE"],
    }

    provider = DominosProvider()
    # Find the store in the background, so the first prompt appears right away.
    store_lookup = Background(provider.get_store, details["SPLITSLICE_STREET"], details["SPLITSLICE_CITY"],
                              details["SPLITSLICE_STATE"], details["SPLITSLICE_ZIP"])
    location = Location(zip=clean_zip(details["SPLITSLICE_ZIP"]), state=clean_state(details["SPLITSLICE_STATE"]))
    try:
        Orchestrator(provider).run(user_id, store_lookup, customer_info, location, announce=announce_store)
    except requests.RequestException as e:  # no connection, or no answer in time
        print(f"Couldn't reach Domino's: {e}")
        return 1
    except StoreNotFound as e:
        print(f"Couldn't find a Domino's store for that address: {e}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):  # Ctrl+C, or the input stream ended
        print("\nOrder cancelled -- nothing was placed.")
        sys.exit(130)
