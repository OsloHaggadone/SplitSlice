"""
Abstract interface every pizza provider (Domino's, eventually others)
must implement, so Agents 2 and 3 can work with any provider without
caring which chain's API is behind it.

Only Domino's is implemented right now (providers/dominos.py), since
it's the only chain with an accessible unofficial API we've found. If
another provider is added later, it plugs in here without Agents 2-4
needing to change.
"""

from abc import ABC, abstractmethod
from typing import Optional

from models import CartItem


class PizzaProvider(ABC):
    @abstractmethod
    def get_store(self, street: str, city: str, state: str, zip_code: str):
        """Look up the nearest store for a delivery address. Returns
        whatever (store, address) representation this provider's
        other methods expect to receive back."""

    @abstractmethod
    def new_order(self, store, customer_info: dict, address_obj):
        """Create an empty order tied to a store/customer/address."""

    @abstractmethod
    def match_pizza(self, order, size: str, style: str) -> Optional[CartItem]:
        """Find the best real menu item for a requested pizza size/style."""

    @abstractmethod
    def match_extra(self, order, query: str) -> Optional[CartItem]:
        """Find the best real menu item for a free-text extra (drink, side, etc.)."""

    @abstractmethod
    def add_items(self, order, cart) -> None:
        """Flush a finalized cart (list[CartItem]) into the order."""

    @abstractmethod
    def validate(self, order) -> bool:
        """Check the order is valid with the provider, without placing it."""

    @abstractmethod
    def get_price_breakdown(self, order) -> dict:
        """Fetch pricing (subtotal, delivery fee, tax, total) without placing."""

    @abstractmethod
    def place(self, order, card=None):
        """Actually submit the order. Every call site for this in the
        project is deliberately disabled until we decide to go live --
        see agents/fulfillment_agent.py."""