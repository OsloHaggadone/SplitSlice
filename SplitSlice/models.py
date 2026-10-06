from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

SIZE_ORDER = ["small", "medium", "large"]  # smallest first; downsizing walks left
PLAIN_STYLES = {"plain", "cheese"}  # styles with no toppings

# Style words read from old menu-item names (storage._infer_pizza). The first
# match wins, so new words go at the end to keep old orders reading the same.
KNOWN_STYLES = ["pepperoni", "cheese", "supreme", "hawaiian",
                "meat", "veggie", "bbq", "buffalo", "plain",
                "sausage", "mushroom", "ham", "bacon", "chicken", "beef", "pineapple",
                "onion", "olive", "jalapeno", "spinach", "extravaganzza", "deluxe", "philly"]

# Preference scores run 0-1. High priority: Standard never gives it up; Smart only if nothing else fits the budget.
HIGH_PRIORITY = 0.7
DEFAULT_PRIORITY = 0.5

# price_sensitivity at or above this recommends Smart, at or below PRICE_INSENSITIVE Premium, else Standard.
PRICE_SENSITIVE = 0.7
PRICE_INSENSITIVE = 0.3


class RetrievalMode(str, Enum):
    SMART = "smart"        # get under budget first, even giving up high-priority preferences
    PREMIUM = "premium"    # exactly what was asked for, plus the best coupon
    STANDARD = "standard"  # get under budget giving up only low-priority preferences


class SplitMode(str, Enum):
    SLICE_SPLIT = "slice_split"  # everyone pays for what they had
    CLEAN_SPLIT = "clean_split"  # everyone pays the same


@dataclass
class CartItem:
    """One order line: a menu variant. size/style are set only for pizzas;
    options holds provider add-ons such as toppings."""
    code: str
    name: str
    qty: int = 1
    unit_price: Optional[float] = None
    size: Optional[str] = None
    style: Optional[str] = None
    options: dict = field(default_factory=dict)

    @property
    def is_pizza(self) -> bool:
        return self.size is not None

    @property
    def key(self) -> tuple:
        """Two lines are the same item only if code and options match."""
        return self.code, repr(sorted(self.options.items()))


@dataclass
class ToppingPreference:
    name: str
    score: float = DEFAULT_PRIORITY  # learned from history, or recorded by the user


@dataclass
class Location:
    """Where an order is delivered: enough to find people nearby."""
    zip: Optional[str] = None       # 5 digits
    state: Optional[str] = None     # 2 letters
    store_id: Optional[str] = None

    @property
    def zip_area(self) -> Optional[str]:
        return self.zip[:3] if self.zip and len(self.zip) >= 3 else None


@dataclass
class PreferencePrior:
    """A new user's starting point: the averaged preferences of people near them."""
    source: str  # e.g. "people in your ZIP code"
    people: int
    orders: int
    toppings: dict = field(default_factory=dict)  # name -> average score
    size_priority: float = DEFAULT_PRIORITY
    price_sensitivity: float = DEFAULT_PRIORITY
    preferred_size: Optional[str] = None
    typical_budget: Optional[float] = None


@dataclass
class UserPreferences:
    user_id: str
    preferred_size: Optional[str] = None
    size_priority: float = DEFAULT_PRIORITY  # how strongly they keep the size they asked for
    topping_preferences: dict = field(default_factory=dict)  # name -> ToppingPreference
    typical_budget: Optional[float] = None
    price_sensitivity: float = DEFAULT_PRIORITY  # 0: never trades preferences for price; 1: always does
    order_count: int = 0
    prior: Optional[PreferencePrior] = None

    def topping_score(self, name: Optional[str]) -> float:
        pref = self.topping_preferences.get((name or "").strip().lower())
        return pref.score if pref else DEFAULT_PRIORITY

    @staticmethod
    def is_high_priority(score: float) -> bool:
        return score >= HIGH_PRIORITY

    def recommended_mode(self) -> RetrievalMode:
        if self.price_sensitivity >= PRICE_SENSITIVE:
            return RetrievalMode.SMART
        if self.price_sensitivity <= PRICE_INSENSITIVE:
            return RetrievalMode.PREMIUM
        return RetrievalMode.STANDARD


@dataclass
class RecordedPreferences:
    """Preferences the user stated; they override learned ones."""
    toppings: dict = field(default_factory=dict)  # name -> score
    size_priority: Optional[float] = None
    preferred_size: Optional[str] = None


@dataclass
class PastOrder:
    """One past order as the prediction model reads it."""
    budget: Optional[float] = None
    mode: Optional[str] = None                          # None for old imported orders
    pizzas: list = field(default_factory=list)          # [(size, style)] as asked for
    dropped_toppings: set = field(default_factory=set)  # dropped by accepted substitutions
    downsized: bool = False                             # accepted a smaller size


@dataclass
class Substitution:
    kind: str         # "size" or "topping"
    original: str     # e.g. "large" or "pepperoni"
    replacement: str  # e.g. "medium" or "plain"

    def describe(self) -> str:
        if self.kind == "size":
            return f"size {self.original} -> {self.replacement}"
        return f"dropped {self.original}"


@dataclass
class Discount:
    code: str
    name: str
    savings: float


@dataclass
class OrderOption:
    """The order proposed for one retrieval mode."""
    mode: RetrievalMode
    cart: list  # [CartItem]
    estimated_total: Optional[float] = None  # menu prices minus coupon savings
    budget: Optional[float] = None
    discount: Optional[Discount] = None
    substitutions: list = field(default_factory=list)  # [Substitution]
    notes: list = field(default_factory=list)

    @property
    def within_budget(self) -> Optional[bool]:
        if self.budget is None or self.estimated_total is None:
            return None
        return self.estimated_total <= self.budget


@dataclass
class Participant:
    name: str  # unique within an order
    portions: dict = field(default_factory=dict)  # cart line index -> how much they had, e.g. slices


@dataclass
class OrderResult:
    cart: list  # [CartItem]
    price_breakdown: dict
    discount: Optional[Discount] = None
