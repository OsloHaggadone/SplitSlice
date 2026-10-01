"""
Agent 3: takes a finalized cart (the user's confirmed choice from
Agent 2), builds the real provider order, validates and prices it, and
-- for group orders -- splits the cost. Placing the order for real is
intentionally left disabled; see place_order() below.

What's implemented now: finalize() (flush cart -> validate -> price)
and clean_split() (even split across participants) are real and
working.

What's a stub (TODO, to design together):
  - slice_split(): the weighted cost-split formula isn't decided yet.
    It needs to attribute each participant's share based on what they
    actually had (e.g. extra meat on one half vs. a plain/vegan half
    on the other), which first requires deciding how a cart's items
    map to "whose half/slice is this" -- that mapping doesn't exist
    anywhere in this project yet (Participant.items is meant to hold
    it, but nothing populates it).
"""

from models import CartItem, OrderResult, Participant
from providers.base import PizzaProvider


class FulfillmentAgent:
    def __init__(self, provider: PizzaProvider):
        self.provider = provider

    def finalize(self, order, cart) -> OrderResult:
        """Flush the cart into the real order, validate it with the
        provider, and fetch pricing -- all without placing anything."""
        self.provider.add_items(order, cart)

        if not self.provider.validate(order):
            raise ValueError("Order failed provider validation.")

        price_breakdown = self.provider.get_price_breakdown(order)
        return OrderResult(cart=cart, price_breakdown=price_breakdown, placed=False)

    def place_order(self, order, card=None) -> None:
        """Actually submit the order. Deliberately not called anywhere
        in this project yet -- uncomment the provider.place(...) call
        below only once we're sure we want a real order to go through."""
        # self.provider.place(order, card)
        raise NotImplementedError(
            "Order placement is intentionally disabled. Uncomment "
            "provider.place(...) above when you're ready to go live."
        )

    def clean_split(self, result: OrderResult, participants: list) -> dict:
        """Divide the total evenly across participants."""
        total = result.price_breakdown.get("total")
        if total is None or not participants:
            return {}
        share = round(float(total) / len(participants), 2)
        return {p.name: share for p in participants}

    def slice_split(self, result: OrderResult, participants: list) -> dict:
        """TODO: build this out. Needs to weight each participant's
        share by what they actually ordered, not just split evenly.
        Open questions to settle before implementing:
          - how a shared pizza's cost gets attributed per-slice/
            per-half to each participant's `items`
          - how shared costs (delivery fee, tax, base pizza price)
            get allocated once per-item costs are attributed -- evenly
            across everyone, or proportional to each person's subtotal?
        """
        raise NotImplementedError("SliceSplit is not built yet.")