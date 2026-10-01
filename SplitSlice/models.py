"""
Shared data structures used across all agents.

Keeping these in one place gives every agent a single, typed contract
to pass data through, instead of passing around loosely-shaped dicts.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RetrievalMode(str, Enum):
    """The three option-generation strategies Agent 2 can run."""
    SMART = "smart"        # prioritize staying under/near budget
    PREMIUM = "premium"    # prioritize getting exactly what was asked for
    STANDARD = "standard"  # balance preferences and cost


class SplitMode(str, Enum):
    """The two cost-splitting strategies Agent 3 can use for group orders."""
    SLICE_SPLIT = "slice_split"  # weighted by what each person actually ordered
    CLEAN_SPLIT = "clean_split"  # total divided evenly across participants


@dataclass
class CartItem:
    """One line item in an order: a specific purchasable menu variant."""
    code: str
    name: str
    qty: int = 1
    unit_price: Optional[float] = None  # filled in once pricing is known


@dataclass
class ToppingPreference:
    """How much a user likes/dislikes one topping, and how much price
    they'd trade to get or avoid it.

    `score` is a 0-1 strength-of-preference the Preference Agent derives
    over time (TODO: define exactly how this is computed -- see
    agents/preference_agent.py).
    `max_price_tradeoff` is how much extra (in dollars) the user has
    shown they're willing to pay to include this topping, or how much
    they'd give up to avoid it if negative.
    """
    name: str
    score: float = 0.5
    max_price_tradeoff: float = 0.0


@dataclass
class UserPreferences:
    """What the Preference Agent knows/predicts about one user."""
    user_id: str
    preferred_size: Optional[str] = None
    topping_preferences: dict = field(default_factory=dict)  # name -> ToppingPreference
    typical_budget: Optional[float] = None
    price_sensitivity: float = 0.5  # 0 = never sacrifices preference for price, 1 = always does
    order_count: int = 0


@dataclass
class OrderOption:
    """One candidate order Agent 2 can present to the user."""
    mode: RetrievalMode
    cart: list  # list[CartItem]
    estimated_total: Optional[float] = None
    notes: str = ""  # e.g. "swapped large -> medium to stay under budget"


@dataclass
class Participant:
    """One person sharing a group order, for cost-splitting."""
    name: str
    items: list = field(default_factory=list)  # list[CartItem] -- what THEY ordered/chose
    dietary_restrictions: list = field(default_factory=list)


@dataclass
class OrderResult:
    """What Agent 3 hands back after finalizing an order."""
    cart: list  # list[CartItem]
    price_breakdown: dict
    placed: bool = False
    split: Optional[dict] = None  # participant name -> amount owed