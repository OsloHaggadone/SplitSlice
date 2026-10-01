"""
Agent 1: tracks what a user actually orders, and is meant to learn
their topping and price-sensitivity preferences over time, predicting
what they'd want on a future order.

What's implemented now: loading/saving per-user data as JSON, and
recording completed orders into that history. That part is real and
working.

What's a stub (TODO, to design together):
  - predict_preferences() currently returns near-empty defaults. With
    any one user's real order history likely being small (a few dozen
    orders at most over a semester), a full ML model has very little
    to learn from. Worth starting with a simple heuristic -- e.g. an
    exponential moving average of which toppings appear in accepted
    orders, and how often a cheaper option was chosen when one was
    offered -- rather than reaching for a real model right away.
  - Exactly how price_sensitivity gets derived from observed trade-offs
    (the "would rather save $0.75 than get sausage" kind of signal)
    isn't decided yet -- this needs a concrete signal to learn from,
    which in turn depends on what Agent 2's Smart mode actually offers
    the user to choose between.
"""

import json
from dataclasses import asdict
from pathlib import Path

from models import OrderResult, UserPreferences

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "users"


class PreferenceAgent:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, user_id: str) -> Path:
        return self.data_dir / f"{user_id}.json"

    def load(self, user_id: str) -> dict:
        """Raw stored data for a user: preferences plus order history."""
        path = self._path(user_id)
        if not path.exists():
            return {"preferences": None, "order_history": []}
        return json.loads(path.read_text())

    def save(self, user_id: str, data: dict) -> None:
        self._path(user_id).write_text(json.dumps(data, indent=2))

    def record_order(self, user_id: str, order: OrderResult) -> None:
        """Append a completed order to this user's history. Called by
        the orchestrator after Agent 3 finalizes an order."""
        data = self.load(user_id)
        data["order_history"].append({
            "cart": [asdict(item) for item in order.cart],
            "price_breakdown": order.price_breakdown,
        })
        self.save(user_id, data)
        # TODO: update learned topping/price-sensitivity scores here
        # based on the new data point, rather than only recomputing
        # them on-demand in predict_preferences() below.

    def predict_preferences(self, user_id: str) -> UserPreferences:
        """Return what we currently know/predict about this user's
        preferences.

        TODO: replace this placeholder with real scoring once the
        update rule above is decided. Right now this only reports how
        many orders we've seen -- no actual prediction yet.
        """
        data = self.load(user_id)
        order_count = len(data["order_history"])
        return UserPreferences(user_id=user_id, order_count=order_count)