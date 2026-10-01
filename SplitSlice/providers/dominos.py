"""
Domino's implementation of the PizzaProvider interface, built on the
unofficial `pizzapi` package.

Ports the menu-matching and pricing logic we built and tested earlier
in this project: a crash-avoidance patch for pizzapi's menu parser,
fuzzy pizza matching against whatever a store's real menu contains
(via matching.py for the extras lookup), and a defensive pricing-field
reader (Domino's order API is unofficial/undocumented, so field names
below are best-effort, not guaranteed).
"""

from difflib import SequenceMatcher
from typing import Optional

from pizzapi import Address, Customer, Order
from pizzapi.menu import Menu, MenuCategory

from matching import best_name_match
from models import CartItem
from providers.base import PizzaProvider


# ---------------------------------------------------------------------
# Crash-avoidance patch: pizzapi's menu parser raises if a category
# lists a product code it never parsed (commonly a stale/dangling
# coupon). Skip those entries instead of crashing.
# ---------------------------------------------------------------------
def _safe_build_categories(self, category_data, parent=None):
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


class DominosProvider(PizzaProvider):
    def get_store(self, street, city, state, zip_code):
        address = Address(street, city, state, zip_code)
        return address.closest_store(), address

    def new_order(self, store, customer_info: dict, address_obj):
        customer = Customer(
            customer_info["first_name"], customer_info["last_name"],
            customer_info["email"], customer_info["phone"],
        )
        return Order(store, customer, address_obj)

    def match_pizza(self, order, size: str, style: str) -> Optional[CartItem]:
        """Fuzzy-match (size, style) against the store's real menu.
        Searches named/preconfigured pizzas first (e.g. "Pepperoni
        Pizza"), then falls back to a plain pizza in that size."""
        size_terms = {
            "small": ["10", "small"],
            "medium": ["12", "medium"],
            "large": ["14", "large"],
        }[size]

        def score(name):
            name_l = name.lower()
            s = SequenceMatcher(None, style, name_l).ratio()
            if any(t in name_l for t in size_terms):
                s += 0.5
            if style != "plain" and style in name_l:
                s += 0.5
            return s

        candidates = list(getattr(order.menu, "preconfigured", []))
        scored = sorted(
            ((score(item.name), item) for item in candidates
             if any(t in item.name.lower() for t in size_terms)),
            key=lambda x: -x[0],
        )
        if scored and scored[0][0] > 0.9 and scored[0][1].code in order.menu.variants:
            return CartItem(code=scored[0][1].code, name=scored[0][1].name)

        fallback_codes = {"small": "10SCREEN", "medium": "12SCREEN", "large": "14SCREEN"}
        code = fallback_codes[size]
        if code in order.menu.variants:
            return CartItem(code=code, name=order.menu.variants[code].get("Name", code))

        return None

    def match_extra(self, order, query: str) -> Optional[CartItem]:
        """Fuzzy-match a free-text extra (drink, side, dessert) against
        every purchasable variant the store's menu has."""
        candidates = [(code, data.get("Name", "")) for code, data in order.menu.variants.items()]
        code, name = best_name_match(query, candidates)
        if code is None:
            return None
        return CartItem(code=code, name=name)

    def add_items(self, order, cart) -> None:
        """Flush a finalized cart into the order: exactly one
        order.add_item() call per unique code, with its final combined
        quantity.

        Why: pizzapi's add_item() doesn't copy the item dict it pulls
        from the menu, so calling it twice for the SAME code makes both
        entries in order.data['Products'] silently share one object --
        the second call's quantity clobbers the first instead of
        adding to it. The cart is built up elsewhere (retrieval agent)
        and only flushed here, once, to sidestep that entirely.
        """
        for entry in cart:
            order.add_item(entry.code, qty=entry.qty)

    def validate(self, order) -> bool:
        return order.validate()

    def get_price_breakdown(self, order) -> dict:
        """Fetch pricing without placing the order. pay_with() hits
        Domino's price-order endpoint (not place-order) and is the
        library's own documented "safe to call while testing" method
        -- it does not charge anything or submit the order. Domino's
        calculates delivery fee and tax server-side based on the store
        and delivery address already in the order.

        This is an unofficial, undocumented API, so field names below
        are best-effort guesses at common variants. Anything not
        recognized is still returned under "other" rather than
        silently dropped.
        """
        field_aliases = {
            "subtotal": ["Food", "FoodAndBeverage", "Subtotal"],
            "delivery_fee": ["DeliveryFee", "Delivery", "DlvyFee"],
            "tax": ["Tax", "SalesTax"],
            "surcharge": ["Surcharge", "Fees"],
            "total": ["Customer", "Payment", "Total"],
        }

        def first_present(d, keys):
            for k in keys:
                if k in d and d[k] not in (None, ""):
                    return d[k]
            return None

        order.pay_with()
        breakdown = order.data.get("AmountsBreakdown") or {}
        amounts = order.data.get("Amounts") or {}
        combined = {**breakdown, **amounts}

        result = {}
        shown_keys = set()
        for label, aliases in field_aliases.items():
            value = first_present(combined, aliases)
            if value is not None:
                result[label] = value
                shown_keys.update(k for k in aliases if k in combined)

        leftover = {k: v for k, v in combined.items() if k not in shown_keys}
        if leftover:
            result["other"] = leftover
        return result

    def place(self, order, card=None):
        """Actually submit the order to Domino's. Left unimplemented on
        purpose -- every place in this project that could call this is
        guarded or commented out until we deliberately decide to wire
        up real payment and submission."""
        raise NotImplementedError(
            "Placing a real order is intentionally not wired up yet. "
            "See Agent 3 (agents/fulfillment_agent.py) for where this "
            "would be called from."
        )