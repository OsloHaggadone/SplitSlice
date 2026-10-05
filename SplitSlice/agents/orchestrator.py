"""
Runs an order from start to finish:

  1. Offer the user's usual order, or ask what they want and take
     follow-ups until they're done (RetrievalAgent).
  2. Ask for a budget (none means Premium) and a retrieval mode.
  3. Build that mode's order from the user's learned preferences
     (PreferenceAgent, RetrievalAgent) and confirm any changes.
  4. Validate and price it (FulfillmentAgent), optionally split the
     cost, and save it to the user's history.

Network work runs in the background while the user reads and types: the
store lookup and menu download from the start, and the coupon search
(several seconds) each time the order is shown. If the preference
database fails, the order still goes ahead, unsaved.
"""

import math
import re
from dataclasses import replace
from typing import Optional

from agents.fulfillment_agent import FulfillmentAgent
from agents.order_parser import clean_text
from agents.preference_agent import PreferenceAgent
from agents.retrieval_agent import RetrievalAgent
from background import Background
from models import Location, Participant, RetrievalMode, SplitMode, UserPreferences
from providers.base import PizzaProvider
from storage import StorageError

MODE_CHOICES = {
    "1": RetrievalMode.STANDARD, "standard": RetrievalMode.STANDARD,
    "2": RetrievalMode.SMART, "smart": RetrievalMode.SMART,
    "3": RetrievalMode.PREMIUM, "premium": RetrievalMode.PREMIUM,
}
MODE_LABELS = {
    RetrievalMode.STANDARD: "1) Standard -- get under budget by giving up only things you care less about",
    RetrievalMode.SMART: "2) Smart    -- get under budget first, even if it means giving up a favorite",
    RetrievalMode.PREMIUM: "3) Premium  -- exactly what you asked for, plus any coupons",
}
SPLIT_CHOICES = {
    "1": SplitMode.CLEAN_SPLIT, "clean": SplitMode.CLEAN_SPLIT,
    "2": SplitMode.SLICE_SPLIT, "slice": SplitMode.SLICE_SPLIT,
}
SLICES = {"small": 6, "medium": 8, "large": 8}  # Domino's cuts, shown when splitting by slices
MAX_PORTIONS = 1000  # slices or portions one person can claim of one item

EVERYONE = {"everyone", "everybody", "all"}
# Yes/no answers are judged by how they start ("yes please", "no thanks").
YES = {"y", "yes", "yeah", "yea", "ya", "yep", "yup", "sure", "ok", "okay", "k", "alright", "fine",
       "definitely", "absolutely", "certainly", "go ahead", "sounds good", "of course", "why not", "do it",
       "for sure", "all right"}
NO = {"n", "no", "nope", "nah", "not", "don't", "dont", "never", "nevermind", "cancel", "stop"}
NO_ONE = {"none", "nobody", "no one", "noone", "just me", "only me", "me", "myself", "skip", "n/a"}


class Orchestrator:
    def __init__(self, provider: PizzaProvider, preferences: Optional[PreferenceAgent] = None):
        self.provider = provider
        if preferences is None:
            try:
                preferences = PreferenceAgent()
            except StorageError as e:
                print(f"[Couldn't open the preference database ({e}); "
                      "ordering without saved preferences, and this order won't be saved]")
        self.preferences = preferences  # None if the database is unavailable
        self.retrieval = RetrievalAgent(provider)
        self.fulfillment = FulfillmentAgent(provider)
        self._searches = {}  # cart -> its coupon search (Background), for the current order

    def run(self, user_id: str, store_lookup, customer_info: dict, location: Optional[Location] = None,
            announce=None):
        """`store_lookup` finds the store in the background (its result() is the provider's
        (store, address)); nothing waits for it until the order needs the menu, when
        `announce(store)` says which store it is. `location` is saved with the order and
        finds a new user's neighbors; its store_id is filled in then."""
        location = location or Location()
        self._searches = {}
        pending_order = Background(lambda: self.provider.new_order(store_lookup.result()[0], customer_info,
                                                                   store_lookup.result()[1]))
        announced = False

        def get_order():
            nonlocal announced
            order = pending_order.result()
            if not announced:
                announced = True
                store = store_lookup.result()[0]
                location.store_id = str(getattr(store, "id", "") or "") or None
                if announce:
                    announce(store)
            return order

        prefs = self._load_preferences(user_id, location)
        if prefs.prior and prefs.order_count < 3:
            print(f"[New here? Until your own orders take over, your preferences start from "
                  f"{prefs.prior.source}: the averages of {prefs.prior.people} people's "
                  f"{prefs.prior.orders} orders.]")
        default_size = prefs.preferred_size or "large"  # for pizzas asked for without a size

        cart = self._offer_usual(get_order, user_id) or self._collect_order(get_order, default_size)
        if not cart:
            return None
        order = get_order()
        # Every mode starts from the best coupon for the order as asked; the
        # receipt is then ready too if the order goes ahead unchanged.
        coupon_search = self._coupon_search(order, cart)
        preparing = Background(lambda: self.fulfillment.prepare(order, cart, coupon_search.result()))

        budget = self._ask_budget(prefs.typical_budget, prefs.prior.typical_budget if prefs.prior else None)
        if budget is None:
            print("No budget given -- using Premium retrieval (exactly what you asked for, plus any coupons).")
            mode = RetrievalMode.PREMIUM
        else:
            mode = self._ask_mode(prefs.recommended_mode())

        coupon_search.result()  # usually done by now; get_options() finds it cached
        option = self.retrieval.get_options(order, cart, prefs, mode, budget)
        self._print_option(option)

        if option.substitutions:
            if not self._confirm("Go with this? (y/n)"):
                if not self._confirm("Order exactly what you asked for instead? (y/n)"):
                    print("Order cancelled.")
                    return None
                option = self.retrieval.get_options(order, cart, prefs, RetrievalMode.PREMIUM, budget)
                self._print_option(option)

        try:
            preparing.result()  # only starts the work, so finalize() can pick it up
            result = self.fulfillment.finalize(order, option.cart, option.discount)
        except ValueError as e:
            print(f"Order validation failed: {e}")
            return None

        print("Order validated successfully.")
        self._print_receipt(result)
        split_with = self._split_cost(result)

        if self.preferences is not None:
            try:
                self.preferences.record_order(user_id, result, requested_cart=cart, option=option,
                                              location=location)
            except (StorageError, ValueError) as e:
                print(f"[Your order is fine, but saving it to your order history failed: {e}]")
            if split_with:
                try:
                    self.preferences.link_contacts(user_id, split_with)
                except (StorageError, ValueError) as e:
                    print(f"[Couldn't save the people you split with as contacts: {e}]")
        return result

    # Step 1: what they want

    def _load_preferences(self, user_id: str, location: Optional[Location] = None) -> UserPreferences:
        if self.preferences is not None:
            try:
                return self.preferences.predict_preferences(user_id, location)
            except StorageError as e:
                print(f"[Couldn't load saved preferences ({e}); continuing without them]")
        return UserPreferences(user_id=user_id)

    def _offer_usual(self, get_order, user_id: str) -> list:
        """The user's usual order rebuilt from this store's menu, or [] to ask from scratch."""
        if self.preferences is None:
            return []
        try:
            usual = self.preferences.usual_order(user_id)
        except StorageError:
            return []  # _load_preferences() already warned
        if not usual:
            return []

        print(f"Welcome back! Your usual is: {self.retrieval.cart_to_text(usual)}")
        if not self._confirm("Order your usual? (y/n)"):
            return []
        cart, notes = self.retrieval.rebuild_cart(get_order(), usual)
        for note in notes:
            print(f"  {note}")
        if not cart:
            print("None of your usual is available at this store -- let's start fresh.")
            return []
        self._print_cart(cart)
        return cart

    def _collect_order(self, get_order, default_size: str = "large") -> list:
        print("What would you like to order?")
        while True:
            text = _read()
            if not text:
                print("Nothing entered.")
                return []
            request = self.retrieval.parse_request(text)  # while the menu may still be downloading
            order = get_order()
            cart = []
            for message in self.retrieval.apply_order(order, cart, request, default_size):
                print(message)
            if cart:
                break
            print("Sorry, I couldn't match that to this store's menu. Try something like "
                  "\"a large pepperoni pizza and a 2 liter coke\" (or press Enter to stop).")

        self._print_cart(cart)

        while True:
            self._coupon_search(order, cart)  # runs while they answer; "no" will need it
            print("Anything you'd like to add, remove, or change? (say 'no' to finish)")
            followup_text = _read()
            if not followup_text:
                continue

            followup = self.retrieval.parse_followup(followup_text, cart)
            if followup.action == "done":
                break
            for message in self.retrieval.apply_followup(order, cart, followup, default_size):
                print(message)
            self._print_cart(cart)
            if followup.finish and cart:  # "add a coke and that's it"; an emptied order keeps asking
                break

        if not cart:
            print("Order is empty -- nothing to place.")
        return cart

    def _coupon_search(self, order, cart) -> Background:
        """The best-coupon search for this cart, started unless it already was. It
        searches a copy, since edits change the cart's items in place."""
        key = tuple((item.key, item.qty) for item in cart)
        if key not in self._searches:
            self._searches[key] = Background(self.provider.find_best_discount, order,
                                             [replace(item) for item in cart])
        return self._searches[key]

    # Step 2: budget and retrieval mode

    def _ask_budget(self, typical: Optional[float], nearby: Optional[float] = None) -> Optional[float]:
        if typical:
            hint = f" (you usually spend about ${typical:.2f})"
        elif nearby:
            hint = f" (people near you usually spend about ${nearby:.2f})"
        else:
            hint = ""
        while True:
            print(f"What's your budget for the food, before tax and delivery?{hint}")
            print("Leave blank for no budget.")
            answer = _read()
            if not answer:
                return None
            text = answer.lstrip("$").strip()
            # "15,50" is a decimal comma; "1,000" a thousands separator.
            text = text.replace(",", ".") if re.fullmatch(r"\d+,\d{2}", text) else text.replace(",", "")
            try:
                budget = float(text)
            except ValueError:
                print("Please enter a dollar amount, like 15 or 15.50.")
                continue
            if not math.isfinite(budget) or budget <= 0:
                print("Budget must be a dollar amount more than $0.")
                continue
            return budget

    def _ask_mode(self, recommended: RetrievalMode) -> RetrievalMode:
        while True:
            print("Choose a retrieval mode:")
            for mode, label in MODE_LABELS.items():
                print(f"  {label}{'  <- recommended for you' if mode == recommended else ''}")
            print("Press Enter for the recommended one.")
            choice = _read().lower()
            if not choice:
                return recommended
            if choice in MODE_CHOICES:
                return MODE_CHOICES[choice]
            print("Please enter 1, 2, or 3.")

    # Splitting the cost

    def _split_cost(self, result) -> list:
        """Offer to split the bill; returns who it was split between ([] if not), to become contacts."""
        names = self._ask_names()
        if len(names) < 2:
            return []
        mode = self._ask_split_mode()
        participants = [Participant(name) for name in names]
        if mode == SplitMode.SLICE_SPLIT:
            self._ask_portions(result.cart, participants)

        split = self.fulfillment.split_cost(result, participants, mode)
        if not split:
            print("Couldn't split the cost: the order total isn't known.")
            return names
        print("Each person owes:")
        for name, amount in split.items():
            print(f"  {name}: ${amount:.2f}")
        return names

    def _ask_names(self) -> list:
        print("Splitting the cost with anyone? Enter everyone's names, including yours, "
              "separated by commas. Leave blank to skip.")
        while True:
            answer = _read()
            if not answer or _answer(answer) is False or answer.lower().strip(" .!") in NO_ONE:
                return []
            names = []
            for raw in _split_names(_drop_yes(answer)):  # "yes: Alice, Bob" -> Alice, Bob
                name = raw.strip(" .!")
                if name and name.lower() not in {n.lower() for n in names}:
                    names.append(name)
            if len(names) >= 2:
                return names
            if names:
                print("That's just one person. Enter everyone's names, including yours, separated by "
                      "commas -- or leave blank to skip.")
            else:
                print("Great -- who's splitting it? Enter everyone's names, including yours, separated "
                      "by commas (or leave blank to skip).")

    def _ask_split_mode(self) -> SplitMode:
        while True:
            print("How should it be split?")
            print("  1) Clean split -- everyone pays the same")
            print("  2) Slice split -- everyone pays for what they had")
            choice = _read().lower()
            if choice in SPLIT_CHOICES:
                return SPLIT_CHOICES[choice]
            print("Please enter 1 or 2.")

    def _ask_portions(self, cart, participants: list) -> None:
        by_name = {p.name.lower(): p for p in participants}
        print('For each item, who had it? List names, with how many slices or portions each '
              'if it wasn\'t shared evenly -- e.g. "Alice 3, Bob 5". Leave blank if everyone shared it.')
        for line, item in enumerate(cart):
            slices = f" ({SLICES[item.size]} slices each)" if item.size in SLICES else ""
            while True:
                print(f"  {item.qty} x {item.name}{slices}:")
                text = _read()
                if not text:
                    break  # shared by everyone
                claims, problem = _parse_claims(text, by_name)
                if problem:
                    print(f"  {problem}")
                    continue
                for key, portion in claims.items():
                    by_name[key].portions[line] = portion
                break

    # Output

    @staticmethod
    def _confirm(prompt: str) -> bool:
        """Ask until the answer is clearly yes or no."""
        while True:
            print(prompt)
            answer = _answer(_read())
            if answer is not None:
                return answer
            print("Please answer y or n.")

    def _print_option(self, option) -> None:
        print(f"\n{option.mode.value.capitalize()} retrieval:")
        for e in option.cart:
            price = f"  ${e.unit_price * e.qty:.2f}" if e.unit_price is not None else ""
            print(f"  {e.qty} x {e.name}{price}")
        if option.substitutions:
            print("Changes from your order:")
            for s in option.substitutions:
                print(f"  - {s.describe()}")
        if option.discount:
            print(f"Coupon: {option.discount.name} (-${option.discount.savings:.2f})")
        if option.estimated_total is not None:
            line = f"Estimated food total: ${option.estimated_total:.2f}"
            if option.budget is not None:
                status = "within budget" if option.within_budget else "OVER budget"
                line += f" (budget ${option.budget:.2f} -- {status})"
            print(line)
        for note in option.notes:
            print(f"Note: {note}")
        print()

    @staticmethod
    def _print_receipt(result) -> None:
        breakdown = result.price_breakdown
        print("Price from Domino's:")
        lines = [(label, _money(breakdown.get(key)))
                 for label, key in (("Food", "subtotal"), ("Delivery fee", "delivery_fee"), ("Tax", "tax"))]
        for label, amount in lines:
            if amount is not None:
                print(f"  {label:<13} ${amount:,.2f}")
        total = _money(breakdown.get("total"))
        if total is not None and lines[0][1] is not None:
            # Anything else charged (a bottle deposit) or taken off, so the lines add up.
            rest = round(total - sum(amount for _, amount in lines if amount is not None), 2)
            if rest >= 0.01:
                print(f"  {'Other charges':<13} ${rest:,.2f}")
            elif rest <= -0.01:
                print(f"  {'Savings':<13} -${-rest:,.2f}")
        print(f"  {'Total':<13} ${total:,.2f}" if total is not None else "  Total         unknown")
        if result.discount:
            print(f"  (includes coupon: {result.discount.name})")
        print("[Dry run: Domino's checked and priced this order, but it wasn't placed and "
              "nothing was charged -- this app never places real orders.]")

    def _print_cart(self, cart) -> None:
        if not cart:
            print("Cart is empty.")
            return
        print("Current order:")
        for e in cart:
            print(f"  {e.qty} x {e.name}")


def _read() -> str:
    """One answer, minus the invisible characters pipes and pastes slip in."""
    return clean_text(input("> "))


def _money(value) -> Optional[float]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if math.isfinite(amount) else None


def _answer(text: str) -> Optional[bool]:
    """True for yes ("yes please"), False for no ("no thanks"), None if neither."""
    words = re.findall(r"[a-z']+", text.lower())
    if words[:1] == ["please"]:  # "please" alone is yes; "please don't" is no
        words = words[1:] or ["yes"]
    if not words:
        return None
    if any(" ".join(words[:n]) in YES for n in (1, 2, 3)):
        return True
    if words[0] in NO:
        return False
    return None


def _drop_yes(text: str) -> str:
    """An answer without its leading yes ("yes please, Alice" -> "Alice")."""
    words = text.split()
    while words and _answer(words[0]):
        words = words[1:]
    return " ".join(words).lstrip(",.!:- ")


def _split_names(text: str) -> list:
    """"Alice, Bob and Cara" -> ["Alice", "Bob", "Cara"]."""
    return [part for part in re.split(r"\s*(?:,|;|&|\+|\band\b)\s*", text, flags=re.IGNORECASE) if part.strip()]


def _parse_claims(text: str, by_name: dict) -> tuple:
    """"Alice 3, Bob 5" / "Alice and Bob" / "everyone" -> ({lowercased name: portion}, None),
    or (None, what's wrong)."""
    claims = {}
    known = ", ".join(getattr(person, "name", key) for key, person in by_name.items())
    for entry in _split_names(text):
        entry = entry.strip()
        if not entry:
            continue
        if entry.lower() in EVERYONE:
            for key in by_name:
                claims[key] = claims.get(key, 0) + 1
            continue
        if entry.lower() in by_name:  # a bare name, even one ending in a number
            key, portion = entry.lower(), 1.0
        else:
            match = re.fullmatch(r"(.+?)\s*[:=]?\s*(\d+(?:\.\d+)?)", entry)
            if not match or match.group(1).strip().lower() not in by_name:
                name = match.group(1).strip() if match else entry
                return None, f"I don't know \"{name}\" -- use the names you entered ({known})."
            key, portion = match.group(1).strip().lower(), float(match.group(2))
        if not 0 < portion <= MAX_PORTIONS:
            return None, (f"\"{entry}\": the number of slices or portions has to be more than 0"
                          f" and at most {MAX_PORTIONS:,}.")
        claims[key] = claims.get(key, 0) + portion
    if not claims:
        return None, "Please list who had it, like \"Alice 3, Bob 5\" or \"Alice and Bob\"."
    return claims, None
