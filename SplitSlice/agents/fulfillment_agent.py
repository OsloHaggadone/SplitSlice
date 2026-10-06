import math
from fractions import Fraction
from typing import Optional

from background import Background
from models import Discount, OrderResult, SplitMode
from providers.base import PizzaProvider


def _signature(cart, discount: Optional[Discount]) -> tuple:
    return tuple((item.key, item.qty) for item in cart), discount.code if discount else None


class FulfillmentAgent:
    def __init__(self, provider: PizzaProvider):
        self.provider = provider
        self._prepared = {}  # _signature -> (validation, pricing), started by prepare()

    def prepare(self, order, cart, discount: Optional[Discount] = None) -> None:
        """Start validating and pricing this cart and coupon in the background,
        for finalize() to pick up if the customer settles on them."""
        self._prepared[_signature(cart, discount)] = (
            Background(self.provider.validate, order, cart, discount),
            Background(self.provider.get_price_breakdown, order, cart, discount))

    def finalize(self, order, cart, discount: Optional[Discount] = None) -> OrderResult:
        """Validate and price the order (both at once), without placing it."""
        valid, priced = self._prepared.pop(_signature(cart, discount), None) or (
            Background(self.provider.validate, order, cart, discount),
            Background(self.provider.get_price_breakdown, order, cart, discount))
        if not valid.result():
            raise ValueError("Order failed provider validation.")
        return OrderResult(cart=cart, price_breakdown=priced.result(), discount=discount)

    def split_cost(self, result: OrderResult, participants: list, mode: SplitMode) -> dict:
        """{name: dollars} (names must be unique), or {} if the total isn't known."""
        if mode == SplitMode.SLICE_SPLIT:
            return self.slice_split(result, participants)
        return self.clean_split(result, participants)

    def clean_split(self, result: OrderResult, participants: list) -> dict:
        total = _to_cents(result.price_breakdown.get("total"))
        if total is None or not participants:
            return {}
        return _to_dollars(_allocate(total, {p.name: 1 for p in participants}))

    def slice_split(self, result: OrderResult, participants: list) -> dict:
        """Falls back to an even split if no item has a known price."""
        total = _to_cents(result.price_breakdown.get("total"))
        if total is None or not participants:
            return {}
        food = self._food_by_person(result.cart, participants)
        if sum(food.values()) <= 0:
            return self.clean_split(result, participants)

        fee = min(max(_to_cents(result.price_breakdown.get("delivery_fee")) or 0, 0), total)
        even = _allocate(fee, {name: 1 for name in food})
        by_food = _allocate(total - fee, food)
        return _to_dollars({name: even[name] + by_food[name] for name in food})

    @staticmethod
    def _food_by_person(cart, participants) -> dict:
        """Menu-price dollars of food each participant had."""
        food = {p.name: Fraction(0) for p in participants}
        for line, item in enumerate(cart):
            cost = Fraction(item.unit_price or 0) * item.qty
            claims = {p.name: Fraction(p.portions.get(line, 0)) for p in participants
                      if p.portions.get(line, 0) > 0}
            if not claims:
                claims = {name: Fraction(1) for name in food}
            claimed = sum(claims.values())
            for name, portion in claims.items():
                food[name] += cost * portion / claimed
        return food


def _allocate(cents: int, weights: dict) -> dict:
    """Split whole cents in proportion to `weights`, adding up exactly: leftover
    cents go to the largest fractions (ties: whoever is listed first)."""
    if not weights:
        return {}
    total_weight = sum(Fraction(w) for w in weights.values())
    if total_weight <= 0:
        weights = {name: 1 for name in weights}
        total_weight = Fraction(len(weights))
    exact = {name: Fraction(cents) * Fraction(w) / total_weight for name, w in weights.items()}
    parts = {name: math.floor(share) for name, share in exact.items()}
    leftover = cents - sum(parts.values())
    for name in sorted(exact, key=lambda n: exact[n] - parts[n], reverse=True)[:leftover]:
        parts[name] += 1
    return parts


def _to_cents(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return round(float(value) * 100)
    except (TypeError, ValueError, OverflowError):
        return None


def _to_dollars(cents_by_name: dict) -> dict:
    return {name: cents / 100 for name, cents in cents_by_name.items()}
