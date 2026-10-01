"""
Agent 2: the ordering mechanism. Understands a free-text request (and
follow-ups), matches it against a provider's real menu, and -- across
Smart / Premium / Standard retrieval modes -- produces an order option
for the user to confirm.

This file owns both language understanding (turning free text into
structured size/style/extras data) and retrieval strategy, since
understanding what was asked for is a prerequisite for retrieving
options for it. If it grows unwieldy once Smart/Premium are built out,
splitting the NLU half into its own module is a reasonable follow-up.

What's implemented now: all of the NLU parsing (initial request +
follow-up add/remove/done loop) and cart management, ported directly
from the single-file version of this project, plus STANDARD retrieval
mode (best match to the stated request, no budget optimization).

What's a stub (TODO, to design together):
  - SMART mode: needs to decide which substitutions are acceptable
    (smaller size? drop a topping?) using UserPreferences from Agent 1,
    and apply discounts/coupons -- Domino's deals aren't exposed by
    pizzapi directly, so sourcing those is its own design problem.
  - PREMIUM mode: like STANDARD (exact match, no substitutions) but
    should also try applying discounts *after* the exact match, never
    changing what's ordered to do so.
"""

import json
import os
import re
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

from matching import best_name_match
from models import CartItem, OrderOption, RetrievalMode, UserPreferences
from providers.base import PizzaProvider

load_dotenv()

GEMINI_MODEL = "gemini-3.6-flash"  # available on the free tier via AI Studio (rate-limited)

EXTRACTION_PROMPT = """You are a pizza order parser. Read the customer's request and
respond with ONLY a JSON object (no other text, no markdown fences) with these keys:

  "size":     one of "small", "medium", "large" (default "large" if unclear)
  "style":    a short word for the pizza style/toppings the customer wants,
              e.g. "pepperoni", "cheese", "supreme", "hawaiian", "plain"
  "quantity": integer number of pizzas requested (default 1)
  "extras":   a list of any other items requested besides the pizza itself,
              e.g. drinks, breadsticks, wings, desserts. Each entry is an
              object: {{"name": "<short description>", "quantity": <int>}}.
              Use an empty list if nothing else was requested.
              Example: [{{"name": "2 liter coke", "quantity": 1}},
                         {{"name": "breadsticks", "quantity": 2}}]

Customer request: {text}
JSON:"""

FOLLOWUP_PROMPT = """The customer's order so far contains:
{cart_summary}

They were asked "Would you like to add, remove, or change anything else?" and replied:
"{text}"

Respond with ONLY a JSON object (no other text, no markdown fences) with these keys:
  "action":   one of "done", "add", "remove"
              - "done" if they indicated they're finished (e.g. "no", "that's all",
                "nothing else", "I'm good", "that's it")
              - "add" if they want to add item(s) or increase a quantity
              - "remove" if they want to remove, cancel, or take something off
                that's already in the order listed above
  "size":     only relevant if action is "add" and a pizza is being added:
              one of "small", "medium", "large" (default "large")
  "style":    only relevant if action is "add" and a pizza is being added: a
              short style word (e.g. "pepperoni", "cheese"); use "" (empty
              string) if no pizza is being added in this response
  "quantity": only relevant if action is "add" and a pizza is being added:
              integer (default 1)
  "extras":   only relevant if action is "add": list of any other items
              (drinks, sides, desserts) to add, as
              {{"name": "...", "quantity": <int>}}. Empty list if none.
  "target":   only relevant if action is "remove": which item from the order
              listed above they want removed, described in a few words

JSON:"""

# Known extra-item keywords for the fallback parser (no AI key required)
KNOWN_EXTRAS = [
    "coke", "coca-cola", "pepsi", "sprite", "dr pepper", "mountain dew",
    "root beer", "soda", "breadsticks", "cheesy bread", "cheese bread",
    "wings", "cinnamon twists", "cinnamon bread", "parmesan bread", "salad",
    "lava cake", "brownie", "chocolate cake",
]

DONE_PHRASES = [
    "no", "nope", "nothing else", "thats all", "that's all", "im done",
    "i'm done", "no thanks", "no thank you", "thats it", "that's it",
    "all set", "done", "finish", "checkout", "thats everything",
    "that's everything", "good", "im good", "i'm good",
]

REMOVE_PHRASES = [
    "remove", "delete", "cancel", "take off", "get rid of",
    "without the", "no more",
]


class RetrievalAgent:
    def __init__(self, provider: PizzaProvider):
        self.provider = provider

    # -----------------------------------------------------------------
    # NLU: initial request
    # -----------------------------------------------------------------
    def parse_request(self, text: str) -> dict:
        return self._parse_with_ai(text) or self._parse_with_keywords(text)

    def _parse_with_ai(self, text: str) -> Optional[dict]:
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            print("[No GOOGLE_API_KEY set; using keyword parser instead]")
            return None
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=EXTRACTION_PROMPT.format(text=text),
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    )
                ),
            )
            raw = response.text.strip().strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
            data = json.loads(raw)
            data.setdefault("size", "large")
            data.setdefault("style", "plain")
            data.setdefault("quantity", 1)
            data.setdefault("extras", [])
            return data
        except Exception as e:
            print(f"[AI parser unavailable ({e}); using keyword parser instead]")
            return None

    def _parse_with_keywords(self, text: str) -> dict:
        """Fallback parser: no AI model required."""
        text_l = text.lower()

        size = "large"
        for s in ("small", "medium", "large"):
            if s in text_l:
                size = s
                break

        qty_match = re.search(r"\b(\d+)\b", text_l)
        quantity = int(qty_match.group(1)) if qty_match else 1

        known_styles = ["pepperoni", "cheese", "supreme", "hawaiian",
                         "meat", "veggie", "bbq", "buffalo", "plain"]
        style = next((s for s in known_styles if s in text_l), "plain")

        extras = [{"name": phrase, "quantity": 1}
                  for phrase in KNOWN_EXTRAS if phrase in text_l]

        return {"size": size, "style": style, "quantity": quantity, "extras": extras}

    # -----------------------------------------------------------------
    # NLU: follow-up loop ("anything else to add, remove, or change?")
    # -----------------------------------------------------------------
    def parse_followup(self, text: str, cart) -> dict:
        result = self._parse_followup_with_ai(text, self.cart_to_text(cart))
        return result if result is not None else self._parse_followup_keywords(text)

    def _parse_followup_with_ai(self, text: str, cart_summary: str) -> Optional[dict]:
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return None
        try:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=FOLLOWUP_PROMPT.format(cart_summary=cart_summary, text=text),
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    )
                ),
            )
            raw = response.text.strip().strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
            data = json.loads(raw)
            data.setdefault("action", "add")
            data.setdefault("size", "large")
            data.setdefault("style", "")
            data.setdefault("quantity", 1)
            data.setdefault("extras", [])
            data.setdefault("target", "")
            return data
        except Exception as e:
            print(f"[AI parser unavailable ({e}); using keyword parser instead]")
            return None

    def _parse_followup_keywords(self, text: str) -> dict:
        t = text.lower().strip()

        if self._is_done_keyword(t):
            return {"action": "done"}

        if any(p in t for p in REMOVE_PHRASES):
            target = t
            for p in REMOVE_PHRASES:
                target = target.replace(p, "")
            return {"action": "remove", "target": target.strip()}

        result = {"action": "add", "size": "large", "style": "", "quantity": 1, "extras": []}
        if "pizza" in t:
            base = self._parse_with_keywords(t)
            result["size"] = base["size"]
            result["style"] = base["style"]
            result["quantity"] = base["quantity"]
            result["extras"] = base["extras"]
        else:
            known = [k for k in KNOWN_EXTRAS if k in t]
            result["extras"] = ([{"name": k, "quantity": 1} for k in known]
                                 if known else [{"name": t, "quantity": 1}])
        return result

    @staticmethod
    def _is_done_keyword(text_l: str) -> bool:
        t = text_l.strip().strip(".!")
        return t in DONE_PHRASES or any(t.startswith(p) for p in DONE_PHRASES)

    # -----------------------------------------------------------------
    # Cart management -- built up here across rounds, independent of
    # the provider's own order object, and only flushed into it once
    # (via provider.add_items) when the user is done.
    # -----------------------------------------------------------------
    def add_to_cart(self, cart, item: CartItem) -> None:
        for entry in cart:
            if entry.code == item.code:
                entry.qty += item.qty
                return
        cart.append(item)

    def find_cart_entry(self, cart, query: str) -> Optional[int]:
        """Fuzzy-match a free-text description against what's already
        in the cart (not the store menu). Returns an index, or None."""
        candidates = [(i, item.name) for i, item in enumerate(cart)]
        idx, _ = best_name_match(query, candidates)
        return idx

    def cart_to_text(self, cart) -> str:
        if not cart:
            return "(nothing yet)"
        return ", ".join(f"{e.qty} x {e.name}" for e in cart)

    def apply_addition(self, order, cart, followup: dict) -> None:
        """Handle an action == 'add' response: match a pizza (if one
        was mentioned) and/or extras against the store menu, and fold
        matches into the cart."""
        if followup.get("style"):
            item = self.provider.match_pizza(order, followup.get("size", "large"), followup["style"])
            if item:
                item.qty = followup.get("quantity", 1)
                self.add_to_cart(cart, item)
            else:
                print("Could not find a matching pizza at this store's menu.")

        for extra in followup.get("extras", []):
            item = self.provider.match_extra(order, extra["name"])
            if item is None:
                print(f"  Could not find a menu match for '{extra['name']}' -- skipping.")
                continue
            item.qty = extra.get("quantity", 1)
            self.add_to_cart(cart, item)

    # -----------------------------------------------------------------
    # Retrieval modes
    # -----------------------------------------------------------------
    def get_options(self, order, request_text: str, prefs: UserPreferences,
                     mode: RetrievalMode, budget: Optional[float] = None) -> OrderOption:
        """Entry point Agent 4 calls to turn a free-text request into a
        concrete, store-matched order option under one of the three
        strategies."""
        if mode == RetrievalMode.STANDARD:
            return self._standard(order, request_text)
        elif mode == RetrievalMode.SMART:
            return self._smart(order, request_text, prefs, budget)
        elif mode == RetrievalMode.PREMIUM:
            return self._premium(order, request_text, prefs)
        raise ValueError(f"Unknown retrieval mode: {mode}")

    def _standard(self, order, request_text: str) -> OrderOption:
        """Match the request as directly as possible against the real
        menu -- no budget optimization, no substitutions."""
        details = self.parse_request(request_text)
        cart = []

        pizza = self.provider.match_pizza(order, details["size"], details["style"])
        if pizza:
            pizza.qty = details["quantity"]
            self.add_to_cart(cart, pizza)

        for extra in details.get("extras", []):
            item = self.provider.match_extra(order, extra["name"])
            if item:
                item.qty = extra.get("quantity", 1)
                self.add_to_cart(cart, item)

        return OrderOption(mode=RetrievalMode.STANDARD, cart=cart)

    def _smart(self, order, request_text: str, prefs: UserPreferences,
               budget: Optional[float]) -> OrderOption:
        """TODO: build this out. Needs to:
          - start from the same matched cart as _standard()
          - if a budget is given (or none -- just minimize cost),
            consider cheaper substitutions (smaller size, drop a
            topping the user only weakly prefers) using
            prefs.topping_preferences and prefs.price_sensitivity to
            decide which trade-offs are acceptable
          - apply any available coupons/deals -- not sourced anywhere
            in this project yet; pizzapi doesn't expose Domino's deals
            directly, so this needs its own design pass
        """
        raise NotImplementedError("Smart retrieval mode is not built yet.")

    def _premium(self, order, request_text: str, prefs: UserPreferences) -> OrderOption:
        """TODO: build this out. Should behave like _standard() (exact
        match, no substitutions) but additionally try to apply
        available discounts/deals AFTER the exact match is found --
        never before, and never in a way that changes what's ordered.
        """
        raise NotImplementedError("Premium retrieval mode is not built yet.")