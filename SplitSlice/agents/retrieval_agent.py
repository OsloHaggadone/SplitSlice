"""
Understands what the customer types, keeps the cart, and builds the
order for each retrieval mode.

Gemini reads each message (the keyword parser in order_parser.py when it
can't); both produce an OrderRequest or a FollowUp, which is applied to
the cart against the store's menu.

Modes (get_options()):
  - PREMIUM: exactly what was asked for, plus the best coupon. Used when
    there's no budget.
  - STANDARD: get under budget giving up only low-priority preferences
    (downsizing, dropping a topping).
  - SMART: get under budget first, giving up high-priority preferences
    only if no under-budget option keeps them; else the cheapest version.
Among options that fit, both give up the least preference (by the learned
scores). Candidates are compared on menu prices in cents; coupons, which
need the provider, are checked only for the exact order and the final pick.
"""

import json
import os
import re
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import Optional

import requests
from dotenv import load_dotenv

from agents import order_parser
from agents.order_parser import Change, FollowUp, OrderRequest, Removal
from background import Background
from matching import best_name_match
from models import (PLAIN_STYLES, SIZE_ORDER, CartItem, OrderOption, RetrievalMode, Substitution,
                    UserPreferences)
from providers.base import PizzaProvider

load_dotenv()

# Fast and accurate for this task, with the highest free-tier rate limit.
GEMINI_MODEL = "gemini-3.5-flash-lite"  # GEMINI_MODEL in .env overrides it
GEMINI_THINKING = "minimal"  # parsing an order needs no reasoning
GEMINI_TIMEOUT = (5, 10)     # seconds to connect and to read
GEMINI_MAX_TOKENS = 512      # replies are short; this only caps a runaway one
GEMINI_DEFAULT_WAIT = 60     # seconds to back off after a rate limit with no retry delay
# Called over REST with requests (already a dependency): Google's SDK would
# slow startup and add dependencies.
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/"

# The instructions live in the schemas' descriptions, which Gemini doesn't bill
# as input tokens, so a request sends only the customer's message (and, for a
# follow-up, the numbered order). Optional fields keep replies short.
_COMPACT = "Reply in compact JSON, without spaces or line breaks."
_SYSTEM = {"parts": [{"text": "Compact JSON."}]}  # the schema's request alone isn't always followed
_COUNT = {"type": "integer", "minimum": 1}
_SIZE = {"type": "string", "enum": SIZE_ORDER}
_STYLE = ('Lowercase: a specialty\'s short name if they name one (hawaiian, veggie, meat, bbq, supreme, '
          'extravaganzza, philly, buffalo, deluxe, wisconsin, ultimate pepperoni, chicken bacon ranch), else its '
          'toppings joined by " & " (e.g. "pepperoni & mushroom"; "pep" is the pepperoni topping), else "plain". '
          'Leave out toppings they decline; ignore crust, sauce and cheese.')
_ITEMS = {
    "pizzas": {"type": "array", "description": 'One entry per kind ("2 large pepperoni" is quantity 2).',
               "items": {"type": "object", "required": ["style"], "properties": {
                   "size": {**_SIZE, "description": 'Omit if not said. Personal/10" is small, 12" medium, '
                                                    '14"/XL large.'},
                   "style": {"type": "string", "description": _STYLE},
                   "quantity": {**_COUNT, "description": "Omit if 1."}}}},
    "extras": {"type": "array", "description": "Everything that isn't a pizza.",
               "items": {"type": "object", "required": ["name"], "properties": {
                   "name": {"type": "string", "description": 'As they said it, e.g. "diet coke".'},
                   "quantity": {**_COUNT, "description": "Omit if 1."}}}},
}
ITEMS_SCHEMA = {"type": "object", "description": "Everything the customer's pizza order asks for. " + _COMPACT,
                "properties": _ITEMS}
FOLLOWUP_SCHEMA = {"type": "object", "description": (
    'The customer\'s reply, on the last line, to "Anything to add, remove, or change?" about their numbered '
    "order. Omit what doesn't apply. " + _COMPACT), "properties": {  # "clear" first, so it's decided first
    "clear": {"type": "boolean", "description": "True to empty the order first; anything they want instead goes "
                                                "in add."},
    "remove": {"type": "array", "description": "Lines to take out.",
               "items": {"type": "object", "required": ["line"], "properties": {
                   "line": _COUNT, "quantity": {**_COUNT, "description": "Only if not all of it."}}}},
    "change": {"type": "array", "description": 'Edits to lines. "It" or "that" means one line: for a size or '
                                               "topping, the last pizza; otherwise the last line.",
               "items": {"type": "object", "required": ["line"], "properties": {
                   "line": _COUNT,
                   "size": _SIZE,
                   "style": {"type": "string", "description": 'The full new style, in the same form as a new '
                             'pizza\'s: adding mushrooms to "pepperoni" makes "pepperoni & mushroom".'},
                   "drop_topping": {"type": "string"},
                   "replace_with": {"type": "string", "description": 'A drink or side to swap in: "the coke to '
                                    '2 sprites" is replace_with "sprite", quantity 2, and nothing in add.'},
                   # after replace_with, so a swap's count is decided once the swap is
                   "quantity": {**_COUNT, "description": 'The new total, a swap\'s too ("the sprites for 3 '
                                                         'cokes": 3).'}}}},
    "add": {"type": "object", "description": "New items.", "properties": _ITEMS},
    "done": {"type": "boolean", "description": "True only if the reply just says they're finished ordering."},
}}

# Parenthesized sizes ('(14")') and trademark signs, which don't help tell items apart.
_NAME_NOISE = re.compile(r" ?\([^)]*\)|[®™]")


def _line_text(item: CartItem) -> str:
    """A pizza by size and style ("large pepperoni pizza": shorter than its menu name, and
    plainer), anything else by name; the count only if it's more than one."""
    name = f"{item.size} {item.style or 'plain'} pizza" if item.is_pizza else _NAME_NOISE.sub("", item.name)
    return name if item.qty == 1 else f"{item.qty} x {name}"


def followup_content(cart, reply: str) -> str:
    """The order as numbered lines (a reply points at lines by number), then the reply."""
    lines = "\n".join(f"{n}. {_line_text(item)}" for n, item in enumerate(cart, start=1))
    return f"{lines or '(empty order)'}\nReply: {reply}"


@dataclass(frozen=True)
class _LineOption:
    """One acceptable version of one cart line."""
    item: CartItem
    subs: tuple    # Substitutions vs. the requested line
    loss: float    # preference score given up
    high: float    # the high-priority part of loss
    cents: int     # menu price of the line


@dataclass(frozen=True)
class _Candidate:
    """One version of the whole cart."""
    cart: tuple           # CartItems, one per line
    substitutions: tuple
    loss: float           # 0 for the exact order
    high: float
    cents: int            # menu subtotal, before coupons


def _frontier(per_line) -> list:
    """Every whole-cart combination worth considering, built line by line,
    keeping only those nothing beats on price, high-priority loss, and total
    loss. Big orders stay fast, and the best choice for any budget is kept."""
    states = [_Candidate((), (), 0.0, 0.0, 0)]
    for options in per_line:
        combined = [_Candidate(state.cart + (option.item,), state.substitutions + option.subs,
                               state.loss + option.loss, state.high + option.high, state.cents + option.cents)
                    for state in states for option in options]
        combined.sort(key=lambda c: (c.cents, c.high, c.loss))
        states = []
        for candidate in combined:
            if not any(kept.high <= candidate.high + 1e-9 and kept.loss <= candidate.loss + 1e-9
                       for kept in states):
                states.append(candidate)
    return states


def _cents(amount) -> int:
    return int(round(float(amount or 0) * 100))


def _merge_lines(cart) -> list:
    """Lines that became the same item (two pizzas cut to a small cheese) as one.
    Copies the lines: candidate carts share their items."""
    merged = {}
    for item in cart:
        if item.key in merged:
            merged[item.key].qty += item.qty
        else:
            merged[item.key] = replace(item)
    return list(merged.values())


def _pizza_only(change: Change) -> bool:
    """A new size or a dropped topping, with no new style: only a pizza has those."""
    return bool(change.drop_topping or (change.new and change.new.pizzas and not change.new.pizzas[0].style_given))


def _error_details(error) -> list:
    """A Gemini error's structured details (RetryInfo, QuotaFailure, ...)."""
    body = error.details if isinstance(error.details, dict) else {}
    inner = body.get("error") if isinstance(body.get("error"), dict) else {}
    details = inner.get("details")
    return [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []


def _retry_delay(error) -> Optional[float]:
    """Seconds a rate-limit error says to wait ("44s"), or None."""
    for detail in _error_details(error):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(detail.get("retryDelay", "")))
        if match:
            return float(match.group(1))
    return None


def _daily_quota_used_up(error) -> bool:
    """Whether a rate-limit error is for the daily quota, not the per-minute one."""
    return any("PerDay" in str(violation.get("quotaId", ""))
               for detail in _error_details(error)
               for violation in (detail.get("violations") or []) if isinstance(violation, dict))


class GeminiError(Exception):
    """An error reply from Gemini. `details` is its JSON body (RetryInfo, QuotaFailure, ...)."""

    def __init__(self, code: int, body):
        error = body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}
        self.code, self.status, self.details = code, error.get("status", ""), body
        self.message = error.get("message") or f"HTTP {code}"
        super().__init__(f"{code} {self.status}: {self.message}")


def gemini_config(schema: dict, thinking: Optional[str] = GEMINI_THINKING) -> dict:
    """A request's generationConfig (thinking=None: the model's default). Temperature
    stays at its default, as Google recommends for Gemini 3."""
    config = {"responseMimeType": "application/json", "responseJsonSchema": schema,
              "maxOutputTokens": GEMINI_MAX_TOKENS}
    if thinking:
        config["thinkingConfig"] = {"thinkingLevel": thinking}
    return config


def _body(response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}


class RetrievalAgent:
    def __init__(self, provider: PizzaProvider):
        self.provider = provider
        api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
        self._model = (os.environ.get("GEMINI_MODEL") or "").strip() or GEMINI_MODEL
        self._thinking = GEMINI_THINKING
        self._paused_until = 0.0  # time.monotonic() until which Gemini isn't asked (rate limited)
        self._session = None  # None: Gemini off
        if api_key:
            self._session = requests.Session()
            self._session.headers["x-goog-api-key"] = api_key
            # Open the connection while the customer types; a model lookup isn't billed.
            Background(partial(self._session.get, GEMINI_URL + self._model, timeout=GEMINI_TIMEOUT))
        else:
            print("[No GEMINI_API_KEY set; using keyword parser instead]")

    def _post(self, schema: dict, content: str) -> dict:
        """Gemini's reply body; an error reply raises GeminiError. One quick retry
        for a busy server (500/502/503) -- not for a 504, which used the whole timeout."""
        busy_retry = True
        while True:
            body = {"systemInstruction": _SYSTEM, "contents": [{"role": "user", "parts": [{"text": content}]}],
                    "generationConfig": gemini_config(schema, self._thinking)}
            response = self._session.post(f"{GEMINI_URL}{self._model}:generateContent", json=body,
                                          timeout=GEMINI_TIMEOUT)
            if response.ok:
                return response.json()
            error = GeminiError(response.status_code, _body(response))
            if error.code in (500, 502, 503) and busy_retry:
                busy_retry = False
                time.sleep(0.5)
            elif error.code == 400 and self._thinking and "thinking" in error.message.lower():
                self._thinking = None  # a model set in .env may not take this level: use its default from now on
            else:
                raise error

    def _ask_ai(self, schema: dict, content: str) -> Optional[dict]:
        """Gemini's JSON reply, or None to use the keyword parser."""
        if self._session is None or time.monotonic() < self._paused_until:
            return None
        try:
            parts = self._post(schema, content)["candidates"][0]["content"]["parts"]
            return json.loads("".join(part.get("text", "") for part in parts if not part.get("thought")))
        except (KeyError, IndexError, TypeError, ValueError):
            return None  # no usable reply (blocked, cut off, or not JSON): this message only
        except requests.RequestException as e:  # no answer in time, or the connection dropped
            print(f"[Gemini didn't answer ({type(e).__name__}); using the keyword parser for this message.]")
            return None
        except GeminiError as e:
            if e.code >= 500:  # trouble on Google's side: try again next message
                print(f"[Gemini didn't answer ({e.code} {e.status}); using the keyword parser for this message.]")
                return None
            if e.code == 429 and not _daily_quota_used_up(e):  # per-minute limit: wait as long as Gemini says
                wait = _retry_delay(e) or GEMINI_DEFAULT_WAIT
                self._paused_until = time.monotonic() + wait
                print(f"[Gemini's per-minute limit for this key was reached; using the keyword parser "
                      f"for about {wait:.0f} seconds.]")
                return None
            # A bad key, unknown model, or used-up daily quota won't fix itself: say so once, then stop.
            reason = " ".join(str(e.message or e).split())
            if len(reason) > 200:
                reason = reason[:200].rsplit(" ", 1)[0] + "..."
            if e.code == 429:
                reason = f"this key's daily quota for {self._model} is used up."
                hint = (" It resets at midnight Pacific time (see Google AI Studio > Usage). Each model has its "
                        "own quota, so a different GEMINI_MODEL in .env works until then.")
            elif e.code == 404:
                hint = f" Set GEMINI_MODEL in .env to a model your key can use (now: {self._model})."
            elif e.code in (400, 401, 403) and "key" in reason.lower():
                hint = " Check GEMINI_API_KEY in .env (Google AI Studio > Get API key)."
            else:
                hint = ""
            print(f"[Gemini unavailable: {reason}{hint} Using the keyword parser instead.]")
            self._session = None
            return None

    # Understanding the customer

    def parse_request(self, text: str) -> OrderRequest:
        """The pizzas and other items a first message asks for."""
        request = order_parser.order_from_ai(self._ask_ai(ITEMS_SCHEMA, text))
        return request if request else order_parser.parse_order(text)

    def parse_followup(self, text: str, cart) -> FollowUp:
        """What a reply to "anything to add, remove, or change?" asks for.
        Replies like "no" or "start over" (nearly every order ends with one)
        are read locally without a request, as is a closing phrase after
        edits ("...and that's it"), which Gemini tends to detect too eagerly."""
        keyword = order_parser.parse_followup(text)
        if keyword.action in ("done", "clear"):
            return keyword
        body, closing = order_parser.strip_closing(text)
        if not body:
            return FollowUp("done")
        followup = (order_parser.followup_from_ai(self._ask_ai(FOLLOWUP_SCHEMA, followup_content(cart, body)))
                    or order_parser.parse_followup(body))
        followup.finish = closing and followup.action != "done"
        return followup

    # The cart

    def add_to_cart(self, cart, item: CartItem) -> None:
        for entry in cart:
            if entry.key == item.key:
                entry.qty += item.qty
                return
        cart.append(item)

    def find_cart_entry(self, cart, query: str) -> Optional[int]:
        """The index of the cart line a description best matches, or None."""
        candidates = [(i, item.name) for i, item in enumerate(cart)]
        idx, _ = best_name_match(query, candidates)
        return idx

    def cart_to_text(self, cart) -> str:
        if not cart:
            return "(nothing yet)"
        return ", ".join(f"{e.qty} x {e.name}" for e in cart)

    def apply_order(self, order, cart, request: OrderRequest, default_size: str = "large") -> list:
        """Add a request's items to the cart; returns messages about any that didn't match exactly."""
        messages = []
        for pizza in request.pizzas:
            size = pizza.size or default_size
            item = self.provider.match_pizza(order, size, pizza.style)
            if item is None:
                messages.append(f"Couldn't find a {size} pizza on this store's menu.")
                continue
            if pizza.style not in PLAIN_STYLES and item.style in PLAIN_STYLES:
                messages.append(f"This store doesn't have {pizza.style} pizza, so that one's a plain cheese pizza.")
            item.qty = pizza.quantity
            self.add_to_cart(cart, item)
        for extra in request.extras:
            item = self.provider.match_extra(order, extra.name)
            if item is None:
                messages.append(f"Couldn't find \"{extra.name}\" on this store's menu, so it's left out.")
                continue
            item.qty = extra.quantity
            self.add_to_cart(cart, item)
        return messages + list(request.notes)

    def apply_followup(self, order, cart, followup: FollowUp, default_size: str = "large") -> list:
        """Removals, then changes, then additions (or clearing, then additions); returns messages."""
        if followup.action == "clear":
            cart.clear()
            messages = ["Cleared your order."]
            if followup.add:  # "start over with a large hawaiian"
                messages.extend(self.apply_order(order, cart, followup.add, default_size))
            return messages
        if followup.action == "unclear":
            return [followup.message]
        messages = []
        shown = list(cart)  # the numbered order the reply's line numbers refer to
        for removal in followup.remove:
            messages.extend(self._remove(cart, removal, shown))
        for change in followup.change:
            messages.extend(self._change(order, cart, change, default_size, shown))
        if followup.add:
            messages.extend(self.apply_order(order, cart, followup.add, default_size))
        if not (followup.remove or followup.change or followup.add):
            messages.append("Sorry, I didn't catch that. Try \"add a coke\", \"remove the wings\", "
                            "\"make it a small\", or \"no\" to finish.")
        return messages

    @staticmethod
    def _shown_line(cart, shown, index: Optional[int]) -> Optional[int]:
        """Where line `index` of the order as shown is now (earlier edits may have moved it)."""
        if index is None or not 0 <= index < len(shown):
            return None
        return next((i for i, item in enumerate(cart) if item is shown[index]), None)

    def _remove(self, cart, removal: Removal, shown=None) -> list:
        if removal.index is not None:
            index = self._shown_line(cart, cart if shown is None else shown, removal.index)
        else:
            index = self.find_cart_entry(cart, removal.target)
        if index is None:
            what = f"\"{removal.target}\"" if removal.target else "that item"
            return [f"Couldn't find {what} in your order."]
        entry = cart[index]
        if removal.quantity is None or removal.quantity >= entry.qty:
            cart.pop(index)
            return [f"Removed: {entry.qty} x {entry.name}"]
        entry.qty -= removal.quantity
        return [f"Removed: {removal.quantity} x {entry.name} ({entry.qty} left)"]

    def _change(self, order, cart, change: Change, default_size: str, shown=None) -> list:
        index = self._change_target(cart, change, shown)
        if index is None:
            what = (f"\"{change.target}\"" if change.target
                    else "a pizza" if change.index is None or _pizza_only(change) else "that item")
            return [f"Couldn't find {what} in your order to change."]
        line = cart[index]
        before = f"{line.qty} x {line.name}"
        quantity = line.qty
        if change.drop_topping:
            toppings = (line.style or "").split(" & ")
            if not line.is_pizza or change.drop_topping not in toppings:
                return [f"There's no {change.drop_topping} to take off {line.name} -- you can remove it "
                        "and add the pizza you want instead."]
            new_style = " & ".join(t for t in toppings if t != change.drop_topping) or "plain"
            spec = change.new.pizzas[0] if change.new and change.new.pizzas else None
            item = self.provider.match_pizza(order, (spec and spec.size) or line.size, new_style)
        elif change.new is None:  # just a new count: "make it two"
            line.qty = change.quantity
            return [f"Changed: {before} -> {line.qty} x {line.name}"]
        elif change.new.pizzas:
            spec = change.new.pizzas[0]
            size = spec.size or (line.size if line.is_pizza else default_size)
            style = spec.style if (spec.style_given or not line.is_pizza) else line.style
            item = self.provider.match_pizza(order, size, style)
            if spec.quantity != 1:
                quantity = spec.quantity
        else:
            extra = change.new.extras[0]
            item = self.provider.match_extra(order, extra.name)
            if extra.quantity != 1:
                quantity = extra.quantity
        if item is None:
            return [f"Couldn't find that on this store's menu, so {line.name} is unchanged."]
        item.qty = change.quantity or quantity  # "make it 2 smalls" also changes the count
        cart.pop(index)
        same = next((entry for entry in cart if entry.key == item.key), None)
        if same:
            same.qty += item.qty
        else:
            cart.insert(index, item)
        return [f"Changed: {before} -> {item.qty} x {item.name}"]

    def _change_target(self, cart, change: Change, shown=None) -> Optional[int]:
        """The line a change names, else the latest pizza (with that topping, for
        "no pepperoni"), or the latest drink or side for "change it to a sprite".
        A size or topping fits only a pizza, so one Gemini points at another line
        (reading "make it a small" as the coke) goes to the latest pizza instead."""
        if change.index is not None:
            index = self._shown_line(cart, cart if shown is None else shown, change.index)
            if index is None or cart[index].is_pizza or not _pizza_only(change):
                return index
        elif change.target:
            return self.find_cart_entry(cart, change.target)
        pizzas = [i for i, line in enumerate(cart) if line.is_pizza]
        if change.drop_topping:
            with_topping = [i for i in pizzas if change.drop_topping in (cart[i].style or "").split(" & ")]
            return (with_topping or pizzas or [None])[-1]
        if change.new is not None and change.new.extras and not change.new.pizzas:
            others = [i for i, line in enumerate(cart) if not line.is_pizza]
            return (others or [len(cart) - 1] if cart else [None])[-1]
        return pizzas[-1] if pizzas else None

    def rebuild_cart(self, order, items) -> tuple:
        """A saved order matched to this store's menu: (cart, notes on what it lacks)."""
        cart, notes = [], []
        for saved in items:
            if saved.is_pizza:
                item = self.provider.match_pizza(order, saved.size, saved.style)
                if item is not None and saved.style not in PLAIN_STYLES and item.style in PLAIN_STYLES:
                    notes.append(f"{saved.name} isn't on this store's menu, so it's a plain cheese pizza here.")
            else:
                item = self.provider.match_extra(order, saved.name)
            if item is None:
                notes.append(f"{saved.name} isn't on this store's menu, so it's left out.")
                continue
            item.qty = saved.qty
            self.add_to_cart(cart, item)
        return cart, notes

    # Retrieval modes

    def get_options(self, order, cart, prefs: UserPreferences,
                    mode: RetrievalMode, budget: Optional[float] = None) -> OrderOption:
        """The option for this mode (PREMIUM without a budget). The cart isn't changed."""
        cart = [replace(item) for item in cart]
        for item in cart:
            item.unit_price = self.provider.get_price(order, item)

        if budget is None or mode == RetrievalMode.PREMIUM:
            option = self._premium(order, cart, budget)
            if mode != RetrievalMode.PREMIUM:
                option.notes.insert(0, "No budget given, so using Premium retrieval.")
            return option
        if mode in (RetrievalMode.STANDARD, RetrievalMode.SMART):
            return self._fit_budget(order, cart, prefs, mode, budget)
        raise ValueError(f"Unknown retrieval mode: {mode}")

    def _premium(self, order, cart, budget: Optional[float]) -> OrderOption:
        discount = self.provider.find_best_discount(order, cart)
        return self._make_option(RetrievalMode.PREMIUM, cart, [], discount, budget)

    def _fit_budget(self, order, cart, prefs: UserPreferences,
                    mode: RetrievalMode, budget: float) -> OrderOption:
        """STANDARD and SMART, which differ only in which preferences may be given up."""
        allow_high = mode == RetrievalMode.SMART
        per_line = [self._line_options(order, item, prefs, allow_high) for item in cart]
        exact = _Candidate(tuple(options[0].item for options in per_line), (), 0.0, 0.0,
                           sum(options[0].cents for options in per_line))
        budget_cents = _cents(budget)

        # A coupon alone may get the exact order under budget.
        exact_discount = self.provider.find_best_discount(order, list(exact.cart))
        exact_net = exact.cents - _cents(exact_discount.savings if exact_discount else 0)
        if exact_net <= budget_cents:
            return self._make_option(mode, list(exact.cart), [], exact_discount, budget)

        candidates = _frontier(per_line)
        fitting = [c for c in candidates if c.cents <= budget_cents]
        if fitting:  # give up the least preference, high-priority first, then the cheapest
            chosen = min(fitting, key=lambda c: (c.high, c.loss, c.cents))
        else:  # nothing fits: the cheapest allowed, counting the exact order with its coupon
            chosen = min(candidates, key=lambda c: (c.cents, c.high, c.loss))
            if exact_net <= chosen.cents:
                chosen = exact

        if chosen.cart == exact.cart:
            final_cart, discount = list(exact.cart), exact_discount
        else:
            final_cart = _merge_lines(chosen.cart)
            discount = self.provider.find_best_discount(order, final_cart)
        option = self._make_option(mode, final_cart, list(chosen.substitutions), discount, budget)

        if option.within_budget is False:
            if not any(item.is_pizza for item in chosen.cart):
                option.notes.append("Nothing in this order can be swapped for something cheaper, "
                                    "so it stays over budget.")
            elif mode == RetrievalMode.STANDARD:
                kept = self._high_priority_kept(chosen.cart, prefs)
                option.notes.append(
                    "Couldn't get under budget by giving up only low-priority preferences"
                    + (f"; kept high-priority: {', '.join(kept)}." if kept else ".")
                )
            else:
                option.notes.append("No version of this order gets under budget; "
                                    "this is the cheapest one.")
        return option

    def _line_options(self, order, item: CartItem, prefs: UserPreferences,
                      allow_high: bool) -> list:
        """A line's acceptable versions, unchanged first: for pizzas, smaller sizes
        and/or the toppings dropped, where the mode allows giving them up."""
        options = [_LineOption(item, (), 0.0, 0.0, _cents(item.unit_price) * item.qty)]
        if not item.is_pizza or item.size not in SIZE_ORDER:
            return options

        size_idx = SIZE_ORDER.index(item.size)
        has_topping = item.style not in PLAIN_STYLES
        topping_score = prefs.topping_score(item.style) if has_topping else 0.0
        size_is_high = prefs.is_high_priority(prefs.size_priority)
        topping_is_high = prefs.is_high_priority(topping_score)
        can_downsize = allow_high or not size_is_high
        can_drop = has_topping and (allow_high or not topping_is_high)

        sizes = SIZE_ORDER[size_idx::-1] if can_downsize else [item.size]
        styles = [item.style] + (["plain"] if can_drop else [])
        seen = {item.key}
        for size in sizes:
            for style in styles:
                if size == item.size and style == item.style:
                    continue
                match = self.provider.match_pizza(order, size, style)
                if match is None or match.key in seen:
                    continue
                price = self.provider.get_price(order, match)
                if price is None:
                    continue

                subs, loss, high = [], 0.0, 0.0
                if size != item.size:
                    subs.append(Substitution("size", item.size, size))
                    size_loss = (size_idx - SIZE_ORDER.index(size)) * prefs.size_priority
                    loss += size_loss
                    high += size_loss if size_is_high else 0.0
                if has_topping and match.style != item.style:
                    if not can_drop:
                        continue  # the menu has no topped version in this size
                    subs.append(Substitution("topping", item.style, match.style))
                    loss += topping_score
                    high += topping_score if topping_is_high else 0.0

                seen.add(match.key)
                match.qty = item.qty
                match.unit_price = price
                options.append(_LineOption(match, tuple(subs), loss, high, _cents(price) * item.qty))
        return options

    @staticmethod
    def _high_priority_kept(cart, prefs: UserPreferences) -> list:
        kept = []
        for item in cart:
            if not item.is_pizza:
                continue
            if item.style not in PLAIN_STYLES and prefs.is_high_priority(prefs.topping_score(item.style)):
                kept.append(item.style)
            if prefs.is_high_priority(prefs.size_priority) and item.size != SIZE_ORDER[0]:
                kept.append(f"{item.size} size")
        return list(dict.fromkeys(kept))

    @staticmethod
    def _make_option(mode: RetrievalMode, cart, substitutions, discount, budget: Optional[float]) -> OrderOption:
        subtotal = sum((item.unit_price or 0.0) * item.qty for item in cart)
        savings = discount.savings if discount else 0.0
        option = OrderOption(mode=mode, cart=cart, estimated_total=round(max(subtotal - savings, 0.0), 2),
                             budget=budget, discount=discount, substitutions=list(substitutions))
        if any(item.unit_price is None for item in cart):
            option.notes.append("Some items have no listed menu price, so the estimate may be low.")
        return option

