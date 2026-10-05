"""
SQLite storage (data/splitslice.db, or SPLITSLICE_DB) for each user's
stated preferences and order history.

Tables: users; recorded_preferences and recorded_topping_preferences
(what users stated); orders (mode, budget, coupon, total, delivery
location, the raw price breakdown); order_items (each order's lines
twice: requested and ordered); order_substitutions (changes they
accepted); contacts (both directions); legacy_json_imports (old JSON
files already imported). Views: order_requests (what each order asked
for) and prediction_pizzas (its pizzas).
"""

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from models import (KNOWN_STYLES, SIZE_ORDER, CartItem, Location, OrderOption, OrderResult,
                    PastOrder, RecordedPreferences, RetrievalMode)

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "splitslice.db"

# Bump when SCHEMA changes. SCHEMA is all CREATE ... IF NOT EXISTS, so rerunning
# it upgrades an older database; columns added to existing tables go in ADDED_COLUMNS.
SCHEMA_VERSION = 3
ADDED_COLUMNS = (  # (table, column, definition), added in v3
    ("orders", "delivery_zip", "TEXT"),
    ("orders", "delivery_state", "TEXT"),
    ("orders", "store_id", "TEXT"),
)

# Run in order in one transaction. CHECKs mirror SIZE_ORDER and RetrievalMode;
# typeof() checks keep text out of number columns.
SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id     TEXT PRIMARY KEY,
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    -- Stated preferences (NULL: not stated); they override learned ones.
    CREATE TABLE IF NOT EXISTS recorded_preferences (
        user_id         TEXT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
        size_priority   REAL CHECK (size_priority IS NULL OR (typeof(size_priority) IN ('real', 'integer')
                                                             AND size_priority BETWEEN 0 AND 1)),
        preferred_size  TEXT CHECK (preferred_size IN ('small', 'medium', 'large'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recorded_topping_preferences (
        user_id  TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
        topping  TEXT NOT NULL,
        score    REAL NOT NULL CHECK (typeof(score) IN ('real', 'integer') AND score BETWEEN 0 AND 1),
        PRIMARY KEY (user_id, topping)
    )
    """,
    """
    -- created_at is NULL for orders imported from old JSON files: older than any since.
    CREATE TABLE IF NOT EXISTS orders (
        order_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id           TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
        created_at        TEXT,
        mode              TEXT CHECK (mode IN ('smart', 'premium', 'standard')),
        budget            REAL CHECK (budget IS NULL OR (typeof(budget) IN ('real', 'integer') AND budget > 0)),
        total             REAL CHECK (total IS NULL OR typeof(total) IN ('real', 'integer')),
        discount_code     TEXT,
        discount_name     TEXT,
        discount_savings  REAL CHECK (discount_savings IS NULL
                                      OR typeof(discount_savings) IN ('real', 'integer')),
        price_breakdown   TEXT NOT NULL DEFAULT '{}',  -- JSON
        delivery_zip      TEXT,
        delivery_state    TEXT,
        store_id          TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS orders_by_user ON orders (user_id, order_id)",
    """
    -- Each order's lines twice: 'requested' and 'ordered'. size/style only for pizzas.
    CREATE TABLE IF NOT EXISTS order_items (
        order_id    INTEGER NOT NULL REFERENCES orders (order_id) ON DELETE CASCADE,
        role        TEXT NOT NULL CHECK (role IN ('requested', 'ordered')),
        line_no     INTEGER NOT NULL,
        code        TEXT NOT NULL,
        name        TEXT NOT NULL,
        qty         INTEGER NOT NULL CHECK (typeof(qty) = 'integer' AND qty > 0),
        unit_price  REAL CHECK (unit_price IS NULL OR (typeof(unit_price) IN ('real', 'integer')
                                                       AND unit_price >= 0)),
        size        TEXT CHECK (size IN ('small', 'medium', 'large')),
        style       TEXT,
        PRIMARY KEY (order_id, role, line_no),
        CHECK ((size IS NULL) = (style IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS order_substitutions (
        order_id     INTEGER NOT NULL REFERENCES orders (order_id) ON DELETE CASCADE,
        seq          INTEGER NOT NULL,
        kind         TEXT NOT NULL CHECK (kind IN ('size', 'topping')),
        original     TEXT NOT NULL,
        replacement  TEXT NOT NULL,
        PRIMARY KEY (order_id, seq)
    )
    """,
    """
    -- People split with, or added by hand; stored both ways.
    CREATE TABLE IF NOT EXISTS contacts (
        user_id     TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
        contact_id  TEXT NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
        source      TEXT NOT NULL CHECK (source IN ('manual', 'split')),
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
        PRIMARY KEY (user_id, contact_id),
        CHECK (user_id <> contact_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS legacy_json_imports (
        user_id          TEXT PRIMARY KEY REFERENCES users (user_id) ON DELETE CASCADE,
        source_file      TEXT NOT NULL,
        orders_imported  INTEGER NOT NULL,
        imported_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
    )
    """,
    """
    -- What each order asked for: its requested lines, or for old imports its ordered ones.
    CREATE VIEW IF NOT EXISTS order_requests AS
    SELECT i.order_id, i.line_no, i.code, i.name, i.qty, i.size, i.style
    FROM order_items AS i
    WHERE i.role = CASE
            WHEN EXISTS (SELECT 1 FROM order_items AS r
                         WHERE r.order_id = i.order_id AND r.role = 'requested')
            THEN 'requested' ELSE 'ordered' END
    """,
    """
    CREATE VIEW IF NOT EXISTS prediction_pizzas AS
    SELECT order_id, line_no, size, style
    FROM order_requests
    WHERE size IS NOT NULL
    """,
)

# No implicit transactions, so _connect() runs exactly one per operation
# (autocommit=True on Python 3.12+, isolation_level=None before).
_NO_IMPLICIT_TRANSACTIONS = (
    {"autocommit": True} if hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL")
    else {"isolation_level": None}
)

MODES = {mode.value for mode in RetrievalMode}

_MAX_INTEGER = 2 ** 63 - 1  # SQLite's INTEGER limit

# Sizes read from a menu name, for pizzas stored without one (old history, a pizza ordered as an extra).
SIZE_NAME_TERMS = {
    "small": ["small", '10"'],
    "medium": ["medium", '12"'],
    "large": ["large", '14"'],
}


class StorageError(Exception):
    """The preference database couldn't be opened, read, or written."""


class PreferenceStore:
    def __init__(self, db_path=None):
        path = Path(db_path or (os.environ.get("SPLITSLICE_DB") or "").strip() or DEFAULT_DB_PATH).expanduser()
        self.db_path = path if path.is_absolute() else PROJECT_ROOT / path
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StorageError(f"Could not create {self.db_path.parent}: {e}") from e
        self._ensure_schema()

    @contextmanager
    def _connect(self, write: bool = False):
        """One connection and transaction per operation: commit, or roll back on
        error, then close. Writers lock up front (BEGIN IMMEDIATE), because SQLite
        only waits out the busy timeout for a lock taken that way."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10, **_NO_IMPLICIT_TRANSACTIONS)
        except sqlite3.Error as e:
            raise StorageError(f"Could not open {self.db_path}: {e}") from e
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")  # has no effect inside a transaction
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            raise StorageError(f"Database error in {self.db_path}: {e}") from e
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            version = self._schema_version(conn)
        if version == SCHEMA_VERSION:
            return
        with self._connect(write=True) as conn:
            if self._schema_version(conn) < SCHEMA_VERSION:  # re-check, holding the lock
                for statement in SCHEMA:
                    conn.execute(statement)
                _upgrade_tables(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _schema_version(self, conn) -> int:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StorageError(
                f"{self.db_path} uses schema v{version}, but this code only "
                f"knows up to v{SCHEMA_VERSION}. Update the code."
            )
        return version

    # Order history

    def record_order(self, user_id: str, result: OrderResult,
                     requested_cart: Optional[list] = None,
                     option: Optional[OrderOption] = None,
                     location: Optional[Location] = None) -> int:
        """Store a finished order (all or nothing); returns its order_id."""
        _check_user_id(user_id)
        entry = {
            "cart": [asdict(item) for item in result.cart],
            "requested": ([asdict(item) for item in requested_cart]
                          if requested_cart is not None else None),
            "price_breakdown": result.price_breakdown,
            "discount": asdict(result.discount) if result.discount else None,
            "mode": option.mode.value if option else None,
            "budget": option.budget if option else None,
            "substitutions": [asdict(s) for s in option.substitutions] if option else [],
            "location": asdict(location) if location else None,
        }
        with self._connect(write=True) as conn:
            _ensure_user(conn, user_id)
            return _insert_order(conn, user_id, entry, imported=False)

    def load_order_history(self, user_id: str) -> list:
        """The user's PastOrders, oldest first."""
        with self._connect() as conn:
            return _history(conn, user_id)

    def load_people(self, user_ids) -> dict:
        """{user_id: (order history, recorded preferences)}."""
        with self._connect() as conn:
            return {user_id: (_history(conn, user_id), _recorded(conn, user_id))
                    for user_id in user_ids}

    def latest_locations(self) -> dict:
        """{user_id: Location of their latest order with one}."""
        with self._connect() as conn:
            return {row["user_id"]: Location(zip=row["delivery_zip"], state=row["delivery_state"],
                                             store_id=row["store_id"])
                    for row in conn.execute(
                        "SELECT user_id, delivery_zip, delivery_state, store_id FROM ("
                        "  SELECT user_id, delivery_zip, delivery_state, store_id, ROW_NUMBER() OVER ("
                        "    PARTITION BY user_id ORDER BY created_at IS NOT NULL DESC, order_id DESC) AS newest"
                        "  FROM orders WHERE COALESCE(delivery_zip, delivery_state, store_id) IS NOT NULL"
                        ") WHERE newest = 1")}

    # Contacts

    def load_contacts(self, user_id: str) -> set:
        with self._connect() as conn:
            return {row["contact_id"] for row in conn.execute(
                "SELECT contact_id FROM contacts WHERE user_id = ?", (user_id,))}

    def add_contacts(self, user_id: str, contact_ids, source: str = "manual") -> list:
        """Link both ways, creating users as needed; returns the new contacts."""
        _check_user_id(user_id)
        contact_ids = [c for c in dict.fromkeys(contact_ids) if c != user_id]
        for contact_id in contact_ids:
            _check_user_id(contact_id)
        with self._connect(write=True) as conn:
            known = {row["contact_id"] for row in conn.execute(
                "SELECT contact_id FROM contacts WHERE user_id = ?", (user_id,))}
            for person in [user_id, *contact_ids]:
                _ensure_user(conn, person)
            conn.executemany(
                "INSERT INTO contacts (user_id, contact_id, source) VALUES (?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                [pair for c in contact_ids for pair in ((user_id, c, source), (c, user_id, source))],
            )
        return [c for c in contact_ids if c not in known]

    def remove_contacts(self, user_id: str, contact_ids) -> list:
        """Unlink both ways; returns the ones that were contacts."""
        contact_ids = list(dict.fromkeys(contact_ids))
        with self._connect(write=True) as conn:
            known = {row["contact_id"] for row in conn.execute(
                "SELECT contact_id FROM contacts WHERE user_id = ?", (user_id,))}
            conn.executemany(
                "DELETE FROM contacts WHERE (user_id = ? AND contact_id = ?) "
                "OR (user_id = ? AND contact_id = ?)",
                [(user_id, c, c, user_id) for c in contact_ids],
            )
        return [c for c in contact_ids if c in known]

    def load_recent_requests(self, user_id: str, limit: int) -> list:
        """What each of the `limit` latest orders asked for, newest first (CartItems without prices)."""
        with self._connect() as conn:
            order_ids = [row["order_id"] for row in conn.execute(
                "SELECT order_id FROM orders WHERE user_id = ? "
                "ORDER BY created_at IS NOT NULL DESC, order_id DESC LIMIT ?",
                (user_id, limit),
            )]
            carts = {order_id: [] for order_id in order_ids}
            if order_ids:
                for row in conn.execute(
                    "SELECT order_id, code, name, qty, size, style FROM order_requests "
                    f"WHERE order_id IN ({', '.join('?' * len(order_ids))}) ORDER BY order_id, line_no",
                    order_ids,
                ):
                    carts[row["order_id"]].append(CartItem(code=row["code"], name=row["name"], qty=row["qty"],
                                                           size=row["size"], style=row["style"]))
        return [carts[order_id] for order_id in order_ids]

    def list_users(self) -> list:
        """(user_id, order count) for every user, alphabetically."""
        with self._connect() as conn:
            return [(row["user_id"], row["orders"]) for row in conn.execute(
                "SELECT u.user_id, COUNT(o.order_id) AS orders FROM users AS u "
                "LEFT JOIN orders AS o ON o.user_id = u.user_id "
                "GROUP BY u.user_id ORDER BY u.user_id"
            )]

    # Stated preferences

    def get_recorded_preferences(self, user_id: str) -> RecordedPreferences:
        with self._connect() as conn:
            return _recorded(conn, user_id)

    def set_recorded_preferences(self, user_id: str, toppings: Optional[dict] = None,
                                 size_priority: Optional[float] = None,
                                 preferred_size: Optional[str] = None) -> None:
        """Record stated preferences; values not passed stay as they are."""
        _check_user_id(user_id)
        values = _validate_recorded(toppings, size_priority, preferred_size)
        with self._connect(write=True) as conn:
            _ensure_user(conn, user_id)
            _write_recorded(conn, user_id, *values, overwrite=True)

    def unset_recorded_preferences(self, user_id: str, toppings=(),
                                   size_priority: bool = False,
                                   preferred_size: bool = False) -> None:
        """Forget stated values, so learned ones apply again."""
        with self._connect(write=True) as conn:
            conn.executemany(
                "DELETE FROM recorded_topping_preferences WHERE user_id = ? AND topping = ?",
                [(user_id, _clean_topping(name)) for name in toppings],
            )
            if size_priority:
                conn.execute("UPDATE recorded_preferences SET size_priority = NULL "
                             "WHERE user_id = ?", (user_id,))
            if preferred_size:
                conn.execute("UPDATE recorded_preferences SET preferred_size = NULL "
                             "WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM recorded_preferences WHERE user_id = ? "
                         "AND size_priority IS NULL AND preferred_size IS NULL",
                         (user_id,))

    # Importing old data/users/<user_id>.json files

    def import_legacy_json(self, path) -> Optional[int]:
        """Import one file's stated preferences (without replacing set ones) and
        history, all or nothing (malformed: ValueError). Returns how many orders
        were imported, or None if the file was imported before."""
        path = Path(path)
        user_id = path.stem
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as e:
            raise ValueError(f"Could not read {path}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"{path} is not a SplitSlice user file")

        history = data.get("order_history") or []
        recorded = data.get("preferences") or {}
        if not isinstance(history, list) or not all(isinstance(e, dict) for e in history):
            raise ValueError(f"{path}: order_history must be a list of orders")
        if not isinstance(recorded, dict):
            raise ValueError(f"{path}: preferences must be an object")
        values = _validate_recorded(recorded.get("toppings"), recorded.get("size_priority"),
                                    recorded.get("preferred_size") or None)

        with self._connect(write=True) as conn:
            if conn.execute("SELECT 1 FROM legacy_json_imports WHERE user_id = ?",
                            (user_id,)).fetchone():
                return None
            _ensure_user(conn, user_id)
            _write_recorded(conn, user_id, *values, overwrite=False)
            for number, entry in enumerate(history, start=1):
                try:
                    _insert_order(conn, user_id, entry, imported=True)
                except (TypeError, AttributeError, KeyError, OverflowError) as e:  # a field of the wrong shape
                    raise ValueError(f"{path}: order {number} is malformed ({e})") from e
            conn.execute(
                "INSERT INTO legacy_json_imports (user_id, source_file, orders_imported) "
                "VALUES (?, ?, ?)",
                (user_id, str(path), len(history)),
            )
        return len(history)


def _upgrade_tables(conn) -> None:
    """Add ADDED_COLUMNS to tables that predate them."""
    for table, column, definition in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


# Reading one user's data, in an open connection

def _history(conn, user_id: str) -> list:
    """The user's PastOrders, oldest first."""
    orders = {
        row["order_id"]: PastOrder(budget=row["budget"], mode=row["mode"])
        for row in conn.execute(
            "SELECT order_id, budget, mode FROM orders WHERE user_id = ? "
            "ORDER BY created_at IS NOT NULL, order_id",
            (user_id,),
        )
    }
    for row in conn.execute(
        "SELECT p.order_id, p.size, p.style FROM prediction_pizzas AS p "
        "JOIN orders AS o ON o.order_id = p.order_id "
        "WHERE o.user_id = ? ORDER BY p.order_id, p.line_no",
        (user_id,),
    ):
        orders[row["order_id"]].pizzas.append((row["size"], row["style"]))
    for row in conn.execute(
        "SELECT s.order_id, s.kind, s.original FROM order_substitutions AS s "
        "JOIN orders AS o ON o.order_id = s.order_id "
        "WHERE o.user_id = ? ORDER BY s.order_id, s.seq",
        (user_id,),
    ):
        past = orders[row["order_id"]]
        if row["kind"] == "topping":
            past.dropped_toppings.add(row["original"])
        else:
            past.downsized = True
    return list(orders.values())


def _recorded(conn, user_id: str) -> RecordedPreferences:
    row = conn.execute(
        "SELECT size_priority, preferred_size FROM recorded_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    toppings = {r["topping"]: r["score"] for r in conn.execute(
        "SELECT topping, score FROM recorded_topping_preferences WHERE user_id = ? ORDER BY topping",
        (user_id,),
    )}
    return RecordedPreferences(
        toppings=toppings,
        size_priority=row["size_priority"] if row else None,
        preferred_size=row["preferred_size"] if row else None,
    )


# Writing rows (shared by record_order() and import_legacy_json())

def _insert_order(conn, user_id: str, entry: dict, imported: bool) -> int:
    """Insert an order given in the old JSON history-entry shape. Imported ones get no timestamp."""
    breakdown = entry.get("price_breakdown") or {}
    discount = entry.get("discount") or {}
    location = entry.get("location") or {}
    budget = _to_float(entry.get("budget"))
    created_at = None if imported else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    order_id = conn.execute(
        "INSERT INTO orders (user_id, created_at, mode, budget, total, discount_code, "
        "discount_name, discount_savings, price_breakdown, delivery_zip, delivery_state, store_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id,
            created_at,
            entry.get("mode") if entry.get("mode") in MODES else None,
            budget if budget is not None and budget > 0 else None,
            _to_float(breakdown.get("total")) if isinstance(breakdown, dict) else None,
            discount.get("code"),
            discount.get("name"),
            _to_float(discount.get("savings")),
            json.dumps(breakdown, default=str),
            clean_zip(location.get("zip")),
            clean_state(location.get("state")),
            _clean_store(location.get("store_id")),
        ),
    ).lastrowid

    for role, key in (("ordered", "cart"), ("requested", "requested")):
        conn.executemany(
            "INSERT INTO order_items (order_id, role, line_no, code, name, qty, "
            "unit_price, size, style) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(order_id, role, line_no, *_item_fields(item))
             for line_no, item in enumerate(entry.get(key) or [], start=1)],
        )

    # Malformed substitutions are skipped rather than failing the order.
    substitutions = [s for s in entry.get("substitutions") or []
                     if s.get("kind") in ("size", "topping") and str(s.get("original") or "").strip()]
    conn.executemany(
        "INSERT INTO order_substitutions (order_id, seq, kind, original, replacement) "
        "VALUES (?, ?, ?, ?, ?)",
        [(order_id, seq, s["kind"],
          _clean_topping(s["original"]) if s["kind"] == "topping" else str(s["original"]),
          str(s.get("replacement") or ""))
         for seq, s in enumerate(substitutions, start=1)],
    )
    return order_id


def _item_fields(item: dict) -> tuple:
    """(code, name, qty, unit_price, size, style); a pizza without a size gets one from its name."""
    size, style = item.get("size"), item.get("style")
    if size is None:
        size, style = _infer_pizza(str(item.get("name") or ""))
    if size in SIZE_ORDER:
        style = str(style or "").strip().lower() or "plain"
    else:
        size, style = None, None

    name = str(item.get("name") or item.get("code") or "")
    try:
        qty = int(item.get("qty", 1))
    except (TypeError, ValueError, OverflowError):
        qty = 0
    if not 1 <= qty <= _MAX_INTEGER:
        raise ValueError(f"{name or 'item'}: quantity must be a whole number of at least 1, "
                         f"got {item.get('qty')!r}")
    return (str(item.get("code") or ""), name, qty,
            _to_float(item.get("unit_price")), size, style)


def _infer_pizza(name: str) -> tuple:
    """(size, style) from a name like "Large Pepperoni Pizza", or (None, None)."""
    name_l = name.lower()
    if "pizza" not in name_l:
        return None, None
    size = next((s for s, terms in SIZE_NAME_TERMS.items()
                 if any(t in name_l for t in terms)), None)
    style = next((s for s in KNOWN_STYLES if s in name_l), "plain")
    return size, style


def _ensure_user(conn, user_id: str) -> None:
    conn.execute("INSERT INTO users (user_id) VALUES (?) ON CONFLICT DO NOTHING", (user_id,))


def _write_recorded(conn, user_id: str, toppings: dict, size_priority: Optional[float],
                    preferred_size: Optional[str], overwrite: bool) -> None:
    """Save stated preferences; with overwrite=False (imports), values already set win."""
    if size_priority is not None or preferred_size is not None:
        new, old = ("excluded.", "") if overwrite else ("", "excluded.")
        conn.execute(
            "INSERT INTO recorded_preferences (user_id, size_priority, preferred_size) "
            "VALUES (?, ?, ?) ON CONFLICT (user_id) DO UPDATE SET "
            f"size_priority = COALESCE({new}size_priority, {old}size_priority), "
            f"preferred_size = COALESCE({new}preferred_size, {old}preferred_size)",
            (user_id, size_priority, preferred_size),
        )
    conn.executemany(
        "INSERT INTO recorded_topping_preferences (user_id, topping, score) VALUES (?, ?, ?) "
        + ("ON CONFLICT (user_id, topping) DO UPDATE SET score = excluded.score" if overwrite
           else "ON CONFLICT (user_id, topping) DO NOTHING"),
        [(user_id, name, score) for name, score in toppings.items()],
    )


def _validate_recorded(toppings, size_priority, preferred_size) -> tuple:
    """Check stated preferences before writing: a readable ValueError, not a constraint error."""
    if toppings is not None and not isinstance(toppings, dict):
        raise ValueError("toppings must map topping names to scores")
    clean_toppings = {_clean_topping(name): _check_score(score, f"topping '{name}'")
                      for name, score in (toppings or {}).items()}
    if size_priority is not None:
        size_priority = _check_score(size_priority, "size_priority")
    if preferred_size is not None and preferred_size not in SIZE_ORDER:
        raise ValueError(f"preferred_size must be one of {', '.join(SIZE_ORDER)}")
    return clean_toppings, size_priority, preferred_size


def _check_score(value, label: str) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label}: score must be a number from 0 to 1, got {value!r}") from None
    if not isfinite(score) or not 0 <= score <= 1:
        raise ValueError(f"{label}: score must be from 0 to 1, got {value!r}")
    return score


def _clean_topping(name) -> str:
    name = str(name).strip().lower()
    if not name:
        raise ValueError("topping name can't be empty")
    return name


def clean_zip(value) -> Optional[str]:
    """A 5-digit ZIP ("95060-1234" -> "95060"), or None."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[:5] if len(digits) >= 5 else None


def clean_state(value) -> Optional[str]:
    """A 2-letter state code, or None."""
    state = str(value or "").strip().upper()
    return state if re.fullmatch(r"[A-Z]{2}", state) else None


def _clean_store(value) -> Optional[str]:
    store_id = str(value).strip() if value is not None else ""
    return store_id or None


def _check_user_id(user_id) -> None:
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id must be a non-empty string")


def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None
