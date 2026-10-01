"""
Agent 4: runs the other three agents in sequence, checking each step's
output before moving to the next, so one agent's bad input never
reaches the next.

What's implemented now: the full interactive flow -- ask what they
want, loop on add/remove/done, finalize through Agent 3, record the
result with Agent 1. This is the same flow the single-file version of
this project had, now split across agents.

What's a stub (TODO, to design together):
  - The one-button "order my usual" shortcut, skipping free-text input
    entirely once Agent 1's prediction is real enough to trust. Marked
    below at the point it would hook in.
"""

from typing import Optional

from agents.fulfillment_agent import FulfillmentAgent
from agents.preference_agent import PreferenceAgent
from agents.retrieval_agent import RetrievalAgent
from models import RetrievalMode
from providers.base import PizzaProvider


class Orchestrator:
    def __init__(self, provider: PizzaProvider):
        self.provider = provider
        self.preferences = PreferenceAgent()
        self.retrieval = RetrievalAgent(provider)
        self.fulfillment = FulfillmentAgent(provider)

    def run(self, user_id: str, store, customer_info: dict, address_obj,
            mode: RetrievalMode = RetrievalMode.STANDARD):
        prefs = self.preferences.predict_preferences(user_id)

        # TODO: once prediction is real, offer "repeat your usual
        # order?" here, and skip straight to retrieval.get_options()
        # with a synthesized request instead of asking for free text.

        order = self.provider.new_order(store, customer_info, address_obj)

        print("What would you like to order?")
        text = input("> ").strip()
        if not text:
            print("Nothing entered.")
            return None

        option = self.retrieval.get_options(order, text, prefs, mode)
        cart = option.cart
        if not cart:
            print("Could not match anything to this store's menu.")
            return None

        self._print_cart(cart)

        while True:
            print("Anything you'd like to add, remove, or change? (say 'no' to finish)")
            followup_text = input("> ").strip()
            if not followup_text:
                continue

            followup = self.retrieval.parse_followup(followup_text, cart)

            if followup["action"] == "done":
                break
            elif followup["action"] == "remove":
                idx = self.retrieval.find_cart_entry(cart, followup.get("target") or followup_text)
                if idx is not None:
                    removed = cart.pop(idx)
                    print(f"Removed: {removed.qty} x {removed.name}")
                else:
                    print("Couldn't match that to anything in your order.")
            else:
                self.retrieval.apply_addition(order, cart, followup)

            self._print_cart(cart)

        if not cart:
            print("Order is empty -- nothing to place.")
            return None

        try:
            result = self.fulfillment.finalize(order, cart)
        except ValueError as e:
            print(f"Order validation failed: {e}")
            return None

        print("Order validated successfully.")
        print("Price breakdown:", result.price_breakdown)

        self.preferences.record_order(user_id, result)
        return result

    def _print_cart(self, cart) -> None:
        if not cart:
            print("Cart is empty.")
            return
        print("Current order:")
        for e in cart:
            print(f"  {e.qty} x {e.name}")