"""
Shared free-text-to-name fuzzy matching.

Used by both a provider's extra-item lookup (matching a request like
"coke" against a store's real menu) and the retrieval agent's cart
lookup (matching a removal request against what's already in the
cart). Factored out here so both use the exact same logic rather than
two copies drifting apart.

Weighted toward whole-word/alias overlap rather than raw character
similarity or substring checks:
  - character-level ratio alone misses real matches, e.g. "coke" vs a
    menu item literally named "Coca-Cola" (different spelling)
  - naive substring checks can false-match on coincidental fragments,
    e.g. "cola" is a substring of "chocolate"
Both of these were real bugs caught while testing the original
single-file version of this project; this module is the fix.
"""

import re
from difflib import SequenceMatcher

# A few common brand/generic aliases, so a plain-language description
# like "coke" can match a menu item actually named "Coca-Cola", etc.
NAME_ALIASES = {
    "coke": ["coca", "cola"],
    "pop": ["soda"],
    "soda": ["pop"],
    "breadsticks": ["bread"],
}


def name_words(text_l: str) -> set:
    """Whole lowercase word tokens (letters/digits only)."""
    return set(re.findall(r"[a-z0-9]+", text_l))


def best_name_match(query: str, candidates, threshold: float = 0.45):
    """Find the best match for `query` among `candidates`.

    candidates: an iterable of (key, name) pairs -- key is whatever the
    caller wants back (a menu variant code, a cart index, ...), name is
    the human-readable string to match against.

    Returns (key, name) of the best match, or (None, None) if nothing
    scores at or above threshold.
    """
    query_l = query.lower()
    query_words = [w for w in query_l.split() if len(w) > 2]
    expanded_words = set(query_words)
    for w in query_words:
        expanded_words.update(NAME_ALIASES.get(w, []))

    best_key, best_name, best_score = None, None, 0.0
    for key, name in candidates:
        name_l = name.lower()
        score = SequenceMatcher(None, query_l, name_l).ratio() * 0.5
        if expanded_words & name_words(name_l):
            score += 0.5
        if score > best_score:
            best_key, best_name, best_score = key, name, score

    if best_score >= threshold:
        return best_key, best_name
    return None, None