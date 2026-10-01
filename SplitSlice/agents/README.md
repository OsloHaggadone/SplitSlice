# AI Pizza Ordering Agents

A 4-agent project built on top of `pizzapi` (unofficial Domino's API)
and Gemini for natural-language parsing.

## Architecture

```
main.py                      entry point
models.py                    shared dataclasses used by every agent
matching.py                  shared fuzzy name-matching (menu + cart)
providers/
  base.py                    abstract interface any pizza provider implements
  dominos.py                 Domino's implementation (the only provider so far)
agents/
  preference_agent.py        Agent 1: tracks orders, (eventually) predicts preferences
  retrieval_agent.py         Agent 2: NLU parsing + Smart/Premium/Standard retrieval
  fulfillment_agent.py       Agent 3: validate, price, place (disabled), split cost
  orchestrator.py            Agent 4: runs the other three, start to finish
data/users/<user_id>.json    per-user order history (created automatically)
```

**Agent 4 doesn't execute the other agents as separate scripts** -- it
imports them as modules and calls their methods directly, in one
running process. That's the standard pattern for a single-language,
single-process multi-agent project like this one; subprocess-based
execution would only make sense if agents needed different languages,
independent scaling, or fault isolation.

## Setup

```
pip install -r requirements.txt
cp .env.example .env     # then put your real key in it
python main.py
```

Get a free Gemini key at https://aistudio.google.com/apikey. Without
one, Agent 2 automatically falls back to a keyword-based parser --
everything still runs, just with less flexible language understanding.

## What's working vs. what's a stub

**Working now**, ported and tested from the single-file version of
this project:
- `providers/dominos.py` -- menu matching, order validation, pricing
- `agents/retrieval_agent.py` -- NLU parsing (initial + follow-up),
  cart management, and `RetrievalMode.STANDARD`
- `agents/fulfillment_agent.py` -- `finalize()` and `clean_split()`
- `agents/preference_agent.py` -- loading/saving/recording order history
- `agents/orchestrator.py` -- the full interactive add/remove/done flow

**Stubbed, raises `NotImplementedError`, to design together next:**
- `RetrievalAgent._smart()` -- budget-aware substitution logic, plus
  sourcing actual discounts/coupons (not exposed by `pizzapi` directly)
- `RetrievalAgent._premium()` -- exact-match + apply-discounts-after
- `FulfillmentAgent.slice_split()` -- the weighted cost-split formula
- `PreferenceAgent.predict_preferences()` -- currently returns mostly
  empty defaults; real prediction needs a decided update rule first

**Deliberately disabled, not a stub:**
- `DominosProvider.place()` and `FulfillmentAgent.place_order()` both
  raise `NotImplementedError` on purpose. Nothing in this project
  places a real order. Don't wire these up without discussing it.

## Known limitations carried over from the single-file version

- `pizzapi` is unofficial and undocumented; pricing field names in
  `get_price_breakdown()` are best-effort guesses, not guaranteed.
- Fuzzy matching (`matching.py`) can mis-map ambiguous requests --
  e.g. "cinnamon twists" can match a menu item just called "Bread
  Twists" if a store doesn't carry a separate cinnamon item, since the
  match is scored on shared words, not true product identity.
- Only Domino's is implemented. `providers/base.py` exists so another
  chain could be added without changing Agents 2-4, but no second
  provider has actually been built.