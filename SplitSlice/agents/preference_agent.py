"""
Learns from a user's order history how much each preference matters,
which decides what the budget modes may give up.

Each score is an exponential moving average over past pizza orders,
starting at 0.5 (three consistent orders cross HIGH_PRIORITY):
  - topping: toward 1 if ordered and kept; toward 0 if not ordered, or
    dropped by a substitution they accepted.
  - size_priority: toward 0 if they accepted a smaller size, else toward 1.
  - price_sensitivity: toward the mode they picked (Smart 1, Standard 0.5,
    Premium 0); it picks the recommended mode.
New users start from the averages of the nearest circle of people with
enough orders (contacts, ZIP, store, ZIP area, state, everyone); their
own orders fade those out. Stated preferences (preferences_cli.py)
override learned ones.
"""

import re
from collections import Counter
from dataclasses import replace
from typing import Optional

from models import (DEFAULT_PRIORITY, PLAIN_STYLES, Location, OrderOption, OrderResult,
                    PreferencePrior, RecordedPreferences, ToppingPreference, UserPreferences)
from storage import PreferenceStore

EMA_ALPHA = 0.2

PRIOR_MIN_ORDERS = 10   # a circle of people is averaged once it has this many orders...
PRIOR_MIN_PEOPLE = 2    # ...from at least this many people
PRIOR_FADES_AFTER = 20  # own pizza orders after which the averages have faded to ~1%

MODE_PRICE_SIGNAL = {"smart": 1.0, "standard": 0.5, "premium": 0.0}

USUAL_LOOKBACK = 10     # recent orders that count toward a usual order
USUAL_MIN_REPEATS = 2   # times the same order must appear among them


def _ema(previous: float, observation: float) -> float:
    return (1 - EMA_ALPHA) * previous + EMA_ALPHA * observation


def prior_applies(history: list) -> bool:
    """Whether nearby people's averages still matter for this history."""
    return sum(1 for past in history if past.pizzas) < PRIOR_FADES_AFTER


def as_user_id(name: str) -> str:
    """"Jane Doe" -> jane_doe."""
    return re.sub(r"[\s\-]+", "_", str(name).strip().lower())


def _cart_signature(cart) -> tuple:
    """Two orders are the same if their pizzas (by size and style), other items, and counts match."""
    totals = Counter()
    for item in cart:
        key = ("pizza", item.size, item.style) if item.is_pizza else ("item", item.code)
        totals[key] += item.qty
    return tuple(sorted(totals.items()))


class PreferenceAgent:
    def __init__(self, store: Optional[PreferenceStore] = None):
        self.store = store if store is not None else PreferenceStore()

    def record_order(self, user_id: str, order: OrderResult,
                     requested_cart: Optional[list] = None,
                     option: Optional[OrderOption] = None,
                     location: Optional[Location] = None) -> None:
        """Save a finished order. What they asked for (`requested_cart`) against what
        they accepted (`option`) shows which substitutions they were fine with."""
        self.store.record_order(user_id, order, requested_cart, option, location)

    def predict_preferences(self, user_id: str, location: Optional[Location] = None) -> UserPreferences:
        """Learned preferences, starting from nearby people's averages while the
        history is short, with stated ones on top. `location` defaults to their last."""
        history = self.store.load_order_history(user_id)
        prior = self.neighborhood_prior(user_id, location) if prior_applies(history) else None
        return self.predict_from(user_id, history, self.store.get_recorded_preferences(user_id), prior)

    def neighborhood_prior(self, user_id: str, location: Optional[Location] = None) -> Optional[PreferencePrior]:
        """The averages of the closest circle with enough orders (each circle includes
        the ones before it), or None if not even everyone has enough yet."""
        everyone = {user for user, _ in self.store.list_users()} - {user_id}
        locations = self.store.latest_locations()
        here = location or locations.get(user_id)

        def near(same) -> set:
            return {user for user, there in locations.items() if same(there)}

        circles = [("your contacts", self.store.load_contacts(user_id))]
        if here and here.zip:
            circles.append(("people in your ZIP code", near(lambda there: there.zip == here.zip)))
        if here and here.store_id:
            circles.append(("people your store delivers to", near(lambda there: there.store_id == here.store_id)))
        if here and here.zip_area:
            circles.append((f"people in your area (ZIP {here.zip_area}xx)",
                            near(lambda there: there.zip_area == here.zip_area)))
        if here and here.state:
            circles.append((f"people in {here.state}", near(lambda there: there.state == here.state)))
        circles.append(("everyone using SplitSlice", everyone))

        people = {}  # user_id -> (history, recorded), for the circle so far
        for label, members in circles:
            new = (members & everyone) - people.keys()
            if new:
                people.update(self.store.load_people(sorted(new)))
            active = {user: data for user, data in people.items() if data[0]}  # those with orders
            if (sum(len(history) for history, _ in active.values()) >= PRIOR_MIN_ORDERS
                    and len(active) >= PRIOR_MIN_PEOPLE):
                return self.average_preferences(label, active)
        return None

    @classmethod
    def average_preferences(cls, source: str, people: dict) -> PreferencePrior:
        """Average each person's own predictions ({user_id: (history, recorded)});
        a topping someone has no score for counts as 0.5."""
        predictions = [cls.predict_from(user, history, recorded)
                       for user, (history, recorded) in sorted(people.items())]
        count = len(predictions)
        sizes = Counter(p.preferred_size for p in predictions if p.preferred_size)
        budgets = [p.typical_budget for p in predictions if p.typical_budget is not None]
        toppings = sorted({name for p in predictions for name in p.topping_preferences})
        return PreferencePrior(
            source=source,
            people=count,
            orders=sum(len(history) for history, _ in people.values()),
            toppings={name: sum(p.topping_score(name) for p in predictions) / count for name in toppings},
            size_priority=sum(p.size_priority for p in predictions) / count,
            price_sensitivity=sum(p.price_sensitivity for p in predictions) / count,
            preferred_size=sizes.most_common(1)[0][0] if sizes else None,
            typical_budget=round(sum(budgets) / len(budgets), 2) if budgets else None,
        )

    def link_contacts(self, user_id: str, names) -> list:
        """Make the people they split a bill with (who use SplitSlice) contacts; returns the new ones."""
        users = {as_user_id(user): user for user, _ in self.store.list_users()}
        matches = [users[key] for key in map(as_user_id, names) if key in users]
        return self.store.add_contacts(user_id, matches, source="split") if matches else []

    def usual_order(self, user_id: str) -> Optional[list]:
        """The user's usual order (CartItems without prices), or None."""
        return self.pick_usual(self.store.load_recent_requests(user_id, USUAL_LOOKBACK))

    @staticmethod
    def pick_usual(recent_carts: list) -> Optional[list]:
        """The most frequent order among `recent_carts` (newest first), if it
        repeats USUAL_MIN_REPEATS times; ties go to the most recent."""
        signatures = [_cart_signature(cart) for cart in recent_carts]
        counts = Counter(sig for sig in signatures if sig)
        if not counts or max(counts.values()) < USUAL_MIN_REPEATS:
            return None
        top = max(counts.values())
        for cart, sig in zip(recent_carts, signatures):
            if sig and counts[sig] == top:
                return [replace(item) for item in cart]
        return None

    @staticmethod
    def predict_from(user_id: str, history: list, recorded: Optional[RecordedPreferences] = None,
                     prior: Optional[PreferencePrior] = None) -> UserPreferences:
        """The model: PastOrders (oldest first) in, UserPreferences out, starting
        from the prior's averages if there is one."""
        topping_scores = dict(prior.toppings) if prior else {}
        size_priority = prior.size_priority if prior else DEFAULT_PRIORITY
        price_sensitivity = prior.price_sensitivity if prior else DEFAULT_PRIORITY
        size_counts = Counter()
        budgets = []

        for past in history:
            if past.budget is not None:
                budgets.append(past.budget)
            if past.mode in MODE_PRICE_SIGNAL:
                price_sensitivity = _ema(price_sensitivity, MODE_PRICE_SIGNAL[past.mode])
            if not past.pizzas:
                continue

            kept = {style for _, style in past.pizzas
                    if style not in PLAIN_STYLES and style not in past.dropped_toppings}
            for topping in set(topping_scores) | kept | past.dropped_toppings:
                observed = 1.0 if topping in kept else 0.0
                topping_scores[topping] = _ema(topping_scores.get(topping, DEFAULT_PRIORITY), observed)

            size_counts.update(size for size, _ in past.pizzas)
            size_priority = _ema(size_priority, 0.0 if past.downsized else 1.0)

        prefs = UserPreferences(
            user_id=user_id,
            preferred_size=(size_counts.most_common(1)[0][0] if size_counts
                            else prior.preferred_size if prior else None),
            size_priority=size_priority,
            topping_preferences={name: ToppingPreference(name=name, score=score)
                                 for name, score in topping_scores.items()},
            typical_budget=round(sum(budgets) / len(budgets), 2) if budgets else None,
            price_sensitivity=price_sensitivity,
            order_count=len(history),
            prior=prior,
        )
        if recorded is not None:
            for name, score in recorded.toppings.items():
                prefs.topping_preferences[name] = ToppingPreference(name=name, score=score)
            if recorded.size_priority is not None:
                prefs.size_priority = recorded.size_priority
            if recorded.preferred_size:
                prefs.preferred_size = recorded.preferred_size
        return prefs
