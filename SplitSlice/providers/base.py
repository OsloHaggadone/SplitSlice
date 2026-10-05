"""The interface a pizza chain implements, so the rest of the app isn't tied to Domino's."""

from abc import ABC, abstractmethod
from typing import Optional

from models import CartItem, Discount


class StoreNotFound(Exception):
    """No store delivers to the address (or the store locator's answer couldn't be read)."""


class PizzaProvider(ABC):
    @abstractmethod
    def get_store(self, street: str, city: str, state: str, zip_code: str):
        """The nearest store for a delivery address: (store, address) as the other methods
        take them. Raises StoreNotFound, or a requests error if the provider can't be reached."""

    @abstractmethod
    def new_order(self, store, customer_info: dict, address_obj):
        """An empty order for a store, customer, and address (it carries the store's menu)."""

    @abstractmethod
    def match_pizza(self, order, size: str, style: str) -> Optional[CartItem]:
        """The menu item for a pizza size and style. Its size and style must say
        what it really is (style "plain" if the toppings weren't found)."""

    @abstractmethod
    def match_extra(self, order, query: str) -> Optional[CartItem]:
        """The menu item for a drink or side described in words."""

    @abstractmethod
    def get_price(self, order, item: CartItem) -> Optional[float]:
        """Menu price of one unit, options included, without a network call."""

    @abstractmethod
    def find_best_discount(self, order, cart) -> Optional[Discount]:
        """The coupon that saves the most on this cart, or None. Worth caching per
        cart: the search starts early, and later steps reuse its result."""

    @abstractmethod
    def validate(self, order, cart, discount: Optional[Discount]) -> bool:
        """Whether the provider accepts this cart and coupon, without placing anything."""

    @abstractmethod
    def get_price_breakdown(self, order, cart, discount: Optional[Discount]) -> dict:
        """The provider's price (subtotal, delivery_fee, tax, total), without placing anything."""
