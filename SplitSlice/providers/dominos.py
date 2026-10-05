"""
Domino's, through the unofficial `pizzapi` package (its field names are
best-effort guesses, not a documented API).

Pizzas: toppings become the store's hand-tossed pizza with those toppings
as Domino's "Options" (there's no separate pepperoni-pizza item); a
specialty becomes its hand-tossed variant; anything else becomes a plain
pizza, style "plain". Prices come from each variant's "Pricing" table, so
pricing needs no network call.

Coupons: pizzapi lists a store's coupons but not which fit a cart, so
find_best_discount() prices the cart with and without the best-matching
ones and keeps the biggest saving. Every price check is cached, so the
final receipt reuses it instead of asking again.
"""

import copy
import re
import threading
from functools import partial
from types import SimpleNamespace
from typing import Optional

import pizzapi.order
import pizzapi.utils
import requests
from pizzapi import Address, Customer, Order
from pizzapi.menu import Menu, MenuCategory
from pizzapi.store import Store
from pizzapi.utils import request_json

from matching import FILLER_WORDS, best_name_match, name_words, with_singulars
from models import PLAIN_STYLES, CartItem, Discount
from providers.base import PizzaProvider, StoreNotFound

MAX_COUPON_TRIES = 4             # coupons priced per cart: one request each
DOMINOS_TIMEOUT = (10, 30)       # seconds to connect, and to wait for each part of a reply

SIZE_CODES = {"small": "10", "medium": "12", "large": "14"}
SIZES_BY_CODE = {code: size for size, code in SIZE_CODES.items()}
BUILD_YOUR_OWN = "S_PIZZA"       # the product whose variants are plain pizzas
HAND_TOSSED = "HANDTOSS"         # the default crust ("FlavorCode")

# Topping words -> Domino's codes (plurals are handled in lookup), and codes -> display names.
TOPPING_CODES = {
    "pepperoni": "P", "sausage": "S", "italian sausage": "S", "ham": "H", "beef": "B",
    "bacon": "K", "chicken": "Du", "premium chicken": "Du", "steak": "Pm", "philly steak": "Pm",
    "mushroom": "M", "onion": "O", "green pepper": "G", "pineapple": "N", "jalapeno": "J",
    "jalapeno pepper": "J", "banana pepper": "Z", "olive": "R", "black olive": "R",
    "spinach": "Si", "tomato": "Td", "diced tomato": "Td", "garlic": "F", "feta": "Fe",
}
TOPPING_NAMES = {
    "P": "Pepperoni", "S": "Italian Sausage", "H": "Ham", "B": "Beef", "K": "Bacon",
    "Du": "Premium Chicken", "Pm": "Philly Steak", "M": "Mushrooms", "O": "Onions",
    "G": "Green Peppers", "N": "Pineapple", "J": "Jalapeno Peppers", "Z": "Banana Peppers",
    "R": "Black Olives", "Si": "Spinach", "Td": "Diced Tomatoes", "F": "Garlic", "Fe": "Feta Cheese",
}

# A word in a request -> a word in the specialty's menu name. Other names match
# by their words ("cheese steak" -> Philly Cheese Steak).
SPECIALTY_NAMES = {
    "hawaiian": "hawaiian", "veggie": "veggie", "bbq": "bbq", "meat": "meatzza", "meatza": "meatzza",
    "supreme": "deluxe", "deluxe": "deluxe", "extravaganzza": "extravaganzza", "extravaganza": "extravaganzza",
    "philly": "philly", "buffalo": "buffalo", "ultimate pepperoni": "ultimate pepperoni",
    "wisconsin": "wisconsin", "chicken bacon ranch": "chicken bacon ranch",
}

# Coupon-name words that say nothing about what a coupon is for.
COUPON_STOPWORDS = {
    "a", "an", "and", "or", "the", "of", "for", "with", "any", "each", "just", "get", "only", "on", "in",
    "at", "to", "from", "your", "you", "our", "more", "all", "up", "per", "every", "plus", "add", "free",
    "price", "priced", "individually", "order", "orders", "ordered", "item", "items", "menu", "choose",
    "receive", "favorite", "may", "local", "store", "stores", "charge", "extra", "excludes", "varies",
    "availability", "by", "some", "piece", "pc", "now", "deal", "offer", "one", "two", "three",
}
SIZE_WORDS = {"small", "medium", "large", "xl"}

_TOPPING_SEPARATORS = re.compile(r"\s*(?:,|&|\+|/|\band\b|\bwith\b)\s*")


def _safe_build_categories(self, category_data, parent=None):
    """pizzapi's menu parser, minus its crash on a product code it never parsed (often a stale coupon)."""
    category = MenuCategory(category_data, parent)
    for sub in category_data["Categories"]:
        category.subcategories.append(self.build_categories(sub, category))
    for product_code in category_data["Products"]:
        product = self.menu_by_code.get(product_code)
        if product is None:
            continue
        category.products.append(product)
        product.categories.append(category)
    return category


Menu.build_categories = _safe_build_categories

# pizzapi sets no timeout, so a stalled connection would hang the app. Running
# out raises requests.Timeout, which callers handle like a dropped connection.
_requests_with_timeout = SimpleNamespace(get=partial(requests.get, timeout=DOMINOS_TIMEOUT),
                                         post=partial(requests.post, timeout=DOMINOS_TIMEOUT))
pizzapi.utils.requests = _requests_with_timeout  # store lookup, menu
pizzapi.order.requests = _requests_with_timeout  # validating and pricing


def _cart_key(cart) -> tuple:
    return tuple((item.key, item.qty) for item in cart)


class DominosProvider(PizzaProvider):
    def __init__(self):
        self._discounts = {}  # (order, cart) -> Optional[Discount]
        self._prices = {}     # (order, cart, coupon code) -> Domino's amounts

    def get_store(self, street, city, state, zip_code):
        """The nearest store that delivers there, preferably one open now. If none
        is open, the nearest anyway, with store.closed_now set: closed stores
        still validate and price orders (pizzapi's closest_store() gives up)."""
        address = Address(street, city, state, zip_code)
        try:
            found = request_json(address.urls.find_url(), line1=address.line1, line2=address.line2, type="Delivery")
        except ValueError as e:  # an answer that isn't JSON
            raise StoreNotFound(f"the store locator's answer couldn't be read ({e})") from e
        stores = (found.get("Stores") if isinstance(found, dict) else None) or []

        def open_now(data):
            return bool(data.get("IsOnlineNow") and (data.get("ServiceIsOpen") or {}).get("Delivery"))

        ranked = ([s for s in stores if open_now(s) and s.get("IsDeliveryStore")]
                  or [s for s in stores if open_now(s)]
                  or [s for s in stores if s.get("IsDeliveryStore")]
                  or stores)
        if not ranked:
            raise StoreNotFound("no Domino's store delivers to that address")
        store = Store(ranked[0], address.country)
        store.closed_now = not open_now(ranked[0])
        return store, address

    def new_order(self, store, customer_info: dict, address_obj):
        customer = Customer(customer_info["first_name"], customer_info["last_name"],
                            customer_info["email"], customer_info["phone"])
        return Order(store, customer, address_obj)

    def match_pizza(self, order, size: str, style: str) -> Optional[CartItem]:
        size_code = SIZE_CODES.get(size)
        if size_code is None:
            return None
        base = self._base_pizza(order, size_code)
        style = " ".join(str(style or "").lower().split())

        if style and style not in PLAIN_STYLES:
            toppings = _topping_codes(style)
            if toppings and base:
                available = _available_toppings(order.menu.variants[base])
                if all(code in available for code in toppings):
                    names = ", ".join(TOPPING_NAMES.get(code, code) for code in toppings)
                    return CartItem(code=base, name=f"{order.menu.variants[base].get('Name', base)} with {names}",
                                    size=size, style=style,
                                    options={code: {"1/1": "1"} for code in toppings})
            specialty = self._specialty_pizza(order, style, size_code)
            if specialty:
                return CartItem(code=specialty, name=order.menu.variants[specialty].get("Name", specialty).strip(),
                                size=size, style=style)

        if base is None:
            return None
        return CartItem(code=base, name=order.menu.variants[base].get("Name", base), size=size, style="plain")

    def match_extra(self, order, query: str) -> Optional[CartItem]:
        """The closest-named menu item; one that's a pizza gets its size and style."""
        candidates = [(code, data.get("Name", "")) for code, data in order.menu.variants.items()]
        code, name = best_name_match(query, candidates)
        if code is None:
            return None
        size, style = self._pizza_identity(order, code)
        return CartItem(code=code, name=name, size=size, style=style)

    def get_price(self, order, item: CartItem) -> Optional[float]:
        """The listed price with this many toppings, from the variant's "Pricing" table."""
        variant = order.menu.variants.get(item.code)
        if not variant:
            return None
        extra = len(item.options)
        pricing = variant.get("Pricing") or {}
        price = pricing.get(f"Price1-{extra}")
        if price is None and extra:  # more toppings than the table lists: extend its last step
            known = sorted((int(key.rsplit("-", 1)[1]), float(value)) for key, value in pricing.items()
                           if re.fullmatch(r"Price1-\d+", key))
            if len(known) >= 2:
                (n1, p1), (n2, p2) = known[-2], known[-1]
                price = p2 + (extra - n2) * (p2 - p1) / (n2 - n1)
        if price is None:
            price = variant.get("Price")
        try:
            return round(float(price), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _base_pizza(order, size_code: str) -> Optional[str]:
        """The plain hand-tossed pizza in this size."""
        for code, variant in order.menu.variants.items():
            if (variant.get("ProductCode") == BUILD_YOUR_OWN and variant.get("SizeCode") == size_code
                    and variant.get("FlavorCode") == HAND_TOSSED):
                return code
        code = f"{size_code}SCREEN"
        return code if code in order.menu.variants else None

    @staticmethod
    def _specialty_pizza(order, style: str, size_code: str) -> Optional[str]:
        """The named specialty in this size, hand tossed if possible."""
        keyword = next((name for word, name in SPECIALTY_NAMES.items()
                        if re.search(rf"\b{re.escape(word)}\b", style)), None)
        wanted = [word for word in name_words(style) if word not in FILLER_WORDS and word != "pizza"]

        def named(data) -> bool:
            name = str(data.get("Name", "")).lower()
            if keyword is not None:
                return keyword in name
            have = with_singulars(name_words(name))
            return bool(wanted) and all(with_singulars([word]) & have for word in wanted)

        for product in getattr(order.menu, "products", []):
            data = product.menu_data
            if data.get("ProductType") != "Pizza" or product.code == BUILD_YOUR_OWN or not named(data):
                continue
            sized = [code for code in data.get("Variants", [])
                     if order.menu.variants.get(code, {}).get("SizeCode") == size_code]
            hand_tossed = [code for code in sized
                           if order.menu.variants[code].get("FlavorCode") == HAND_TOSSED]
            if hand_tossed or sized:
                return (hand_tossed or sized)[0]
        return None

    @staticmethod
    def _pizza_identity(order, code: str) -> tuple:
        """(size, style) if a variant is a pizza, else (None, None)."""
        variant = order.menu.variants.get(code) or {}
        product = getattr(order.menu, "menu_by_code", {}).get(variant.get("ProductCode"))
        size = SIZES_BY_CODE.get(variant.get("SizeCode"))
        if product is None or size is None or product.menu_data.get("ProductType") != "Pizza":
            return None, None
        style = "plain" if product.code == BUILD_YOUR_OWN else product.name.strip().lower()
        return size, style

    def find_best_discount(self, order, cart) -> Optional[Discount]:
        """Price the cart without a coupon and with each candidate, all at once
        (each check takes over a second), and keep the biggest saving. Any
        failure just means no discount: the API is unofficial."""
        if not cart:
            return None
        key = (id(order), _cart_key(cart))
        if key in self._discounts:
            return self._discounts[key]

        def net(code):
            try:
                amounts = self._price(order, cart, code)
                return float(amounts.get("Net", amounts.get("Customer")))
            except Exception:
                return None

        best = None
        coupons = self._candidate_coupons(order, cart)
        if coupons:
            baseline, *nets = _all_at_once(net, [None] + [code for code, _ in coupons])
            if baseline is not None:
                for (code, name), coupon_net in zip(coupons, nets):
                    savings = round(baseline - coupon_net, 2) if coupon_net is not None else 0
                    if savings > 0 and (best is None or savings > best.savings):
                        best = Discount(code=code, name=name, savings=savings)
        self._discounts[key] = best
        return best

    def _candidate_coupons(self, order, cart) -> list:
        """Delivery coupons whose names fit the cart, best first: each word shared
        with the cart counts for a coupon, each other word half against it, and
        a coupon for only sizes the cart doesn't have is skipped."""
        cart_words, pizzas = set(), 0
        for entry in cart:
            cart_words |= _coupon_words(entry.name)
            if entry.size:
                pizzas += entry.qty
                cart_words |= {entry.size, "pizza", "topping"}
                if entry.options:
                    cart_words.add(str(len(entry.options)))  # "2-Topping"
                if (order.menu.variants.get(entry.code) or {}).get("Tags", {}).get("Specialty"):
                    cart_words.add("specialty")
        if pizzas:
            cart_words.add(str(pizzas))

        scored = []
        for coupon in getattr(order.menu, "coupons", []):
            methods = (coupon.menu_data.get("Tags") or {}).get("ValidServiceMethods")
            if methods and "Delivery" not in methods:
                continue
            words = _coupon_words(coupon.name)
            sizes = words & SIZE_WORDS
            if sizes and not sizes & cart_words:
                continue
            overlap = len(words & cart_words)
            if overlap:
                scored.append((overlap - 0.5 * (len(words) - overlap), coupon.code, coupon.name))
        scored.sort(key=lambda x: -x[0])
        return [(code, name) for _, code, name in scored[:MAX_COUPON_TRIES]]

    def _price(self, order, cart, coupon_code: Optional[str]) -> dict:
        """Domino's amounts for the cart and coupon (cached). pay_with() only
        prices the order: nothing is placed or charged."""
        key = (id(order), _cart_key(cart), coupon_code)
        if key not in self._prices:
            draft = _draft(order, cart, coupon_code)
            try:
                draft.pay_with()
            except requests.HTTPError as e:
                raise ValueError(f"Domino's couldn't price the order (HTTP {_status(e)})") from e
            except requests.RequestException:
                raise  # a dropped connection: the caller reports it
            except Exception as e:  # pizzapi's bare Exception when Domino's refuses to price it
                raise ValueError(f"Domino's couldn't price the order: {e}") from e
            self._prices[key] = {**(draft.data.get("AmountsBreakdown") or {}), **(draft.data.get("Amounts") or {})}
        return self._prices[key]

    def validate(self, order, cart, discount: Optional[Discount]) -> bool:
        """Domino's prices only valid orders, so a cached price for this cart and
        coupon (from the coupon search) means no request is needed. An HTTP error
        becomes a ValueError; a dropped connection stays a requests error."""
        code = discount.code if discount else None
        if (id(order), _cart_key(cart), code) in self._prices:
            return True
        try:
            return _draft(order, cart, code).validate()
        except requests.HTTPError as e:
            raise ValueError(f"Domino's rejected the order (HTTP {_status(e)})") from e

    def get_price_breakdown(self, order, cart, discount: Optional[Discount]) -> dict:
        """Domino's price, by field. Amounts with no known name land under "other"."""
        amounts = self._price(order, cart, discount.code if discount else None)
        result, named = {}, set()
        for label, aliases in (("subtotal", ["Food", "FoodAndBeverage", "Subtotal"]),
                               ("delivery_fee", ["DeliveryFee", "Delivery", "DlvyFee"]),
                               ("tax", ["Tax", "SalesTax"]),
                               ("surcharge", ["Surcharge", "Fees"]),
                               ("total", ["Customer", "Payment", "Total"])):
            value = next((amounts[k] for k in aliases if amounts.get(k) not in (None, "")), None)
            if value is not None:
                result[label] = value
                named.update(k for k in aliases if k in amounts)
        leftover = {k: v for k, v in amounts.items() if k not in named}
        if leftover:
            result["other"] = leftover
        return result


def _status(error) -> str:
    return str(getattr(getattr(error, "response", None), "status_code", "error"))


def _all_at_once(fn, items) -> list:
    """[fn(item) for item in items], each on a daemon thread (Ctrl+C never waits). fn must not raise."""
    results = [None] * len(items)

    def run(i, item):
        results[i] = fn(item)

    threads = [threading.Thread(target=run, args=(i, item), daemon=True) for i, item in enumerate(items)]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            thread.join(0.1)  # short steps, so Ctrl+C gets through on Windows
    return results


def _draft(order, cart, coupon_code: Optional[str]):
    """A copy of the order holding the cart and coupon. Domino's writes its reply
    into the copy; the menu, store, and customer are shared (only read)."""
    draft = copy.copy(order)
    draft.data = copy.deepcopy(order.data)
    draft.data["Products"] = _products(cart)
    draft.data["Coupons"] = [{"Code": coupon_code, "Qty": 1, "ID": 1}] if coupon_code else []
    return draft


def _products(cart) -> list:
    """Domino's "Products" for a cart. Built here because pizzapi's add_item()
    can't send options, and shares one dict between lines with the same code."""
    products = []
    for product_id, entry in enumerate(cart, start=1):
        product = {"Code": entry.code, "Qty": entry.qty, "ID": product_id, "isNew": True, "AutoRemove": False}
        if entry.options:
            product["Options"] = copy.deepcopy(entry.options)
        products.append(product)
    return products


def _coupon_words(text: str) -> set:
    """What a coupon or item name is for, singular, without prices, dates, and
    filler ("2 Large 2-Topping Pizzas for $11.99" -> 2, large, topping, pizza)."""
    text = re.sub(r"\$\s*\d+(?:\.\d+)?|\d+/\d+", " ", str(text).lower())
    words = set()
    for word in name_words(text):
        if word in COUPON_STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.add(word)
    return words


def _topping_codes(style: str) -> Optional[list]:
    """Topping codes for "pepperoni & mushrooms", or None unless every part is a topping."""
    codes = []
    for part in _TOPPING_SEPARATORS.split(style):
        part = part.strip()
        if not part:
            continue
        code = next((TOPPING_CODES[word] for word in (part, part[:-1], part[:-2]) if word in TOPPING_CODES), None)
        if code is None:
            return None
        if code not in codes:
            codes.append(code)
    return codes or None


def _available_toppings(variant: dict) -> set:
    """Topping codes a variant takes, from e.g. "B,Bq,C,X=0:0.5:1:1.5"."""
    return {part.split("=")[0] for part in str(variant.get("AvailableToppings") or "").split(",") if part}
