# SplitSlice

A command-line pizza ordering assistant for Domino's. Describe your order in
plain English, set a budget, and SplitSlice builds the best order it can, finds
the best coupon, and splits the bill between friends.

**Built with:** Python, SQLite, the Google Gemini API, and
[pizzapi](https://pypi.org/project/pizzapi/) (an unofficial Domino's API).

> **Dry run only.** SplitSlice checks and prices orders with Domino's but never
> places them. It isn't affiliated with Domino's.

## Features

- **Natural-language ordering.** Type "2 lg pep and a couple sprites", then edit
  with "swap the sprites for 3 cokes" or "make it a small". Gemini turns each
  message into structured JSON; a built-in keyword parser takes over when
  there's no API key or Gemini is unavailable.
- **Budget modes.** Premium orders exactly what you asked for. Standard and
  Smart find a version of your order that fits your budget, giving up what you
  care about least (a larger size, a topping).
- **Preference learning.** Learns from your order history how much each topping
  and pizza size matters to you. New users start from the averages of people
  near them.
- **Coupon search.** Finds the coupon that saves the most by pricing the best
  candidates with Domino's in parallel.
- **Bill splitting.** Split evenly, or by what each person had, exact to the cent.
- **Your usual.** Offers to reorder the order you place most often.

## Example

```text
What would you like to order?
> 2 lg pep and a couple sprites
Ordering from Domino's #1234 (123 Main St, Springfield)
Current order:
  2 x Large (14") Hand Tossed Pizza with Pepperoni
  2 x 2-Liter Sprite®
Anything you'd like to add, remove, or change? (say 'no' to finish)
> swap the sprites for 3 cokes
Changed: 2 x 2-Liter Sprite® -> 3 x 2-Liter Coke®
Current order:
  2 x Large (14") Hand Tossed Pizza with Pepperoni
  3 x 2-Liter Coke®
Anything you'd like to add, remove, or change? (say 'no' to finish)
> add garlic bread and that's it
Current order:
  2 x Large (14") Hand Tossed Pizza with Pepperoni
  3 x 2-Liter Coke®
  1 x 16-Piece Garlic Bread Bites
What's your budget for the food, before tax and delivery? (people near you usually spend about $35.83)
Leave blank for no budget.
>
No budget given -- using Premium retrieval (exactly what you asked for, plus any coupons).

Premium retrieval:
  2 x Large (14") Hand Tossed Pizza with Pepperoni  $42.98
  3 x 2-Liter Coke®  $12.27
  1 x 16-Piece Garlic Bread Bites  $7.99
Coupon: 16 Piece Parmesan or Garlic Bread Bites and a 2-Liter (-$4.09)
Estimated food total: $59.15

Order validated successfully.
Price from Domino's:
  Food          $59.15
  Delivery fee  $5.99
  Tax           $6.35
  Other charges $0.30
  Total         $71.79
  (includes coupon: 16 Piece Parmesan or Garlic Bread Bites and a 2-Liter)
[Dry run: Domino's checked and priced this order, but it wasn't placed and nothing was charged -- this app never places real orders.]
Splitting the cost with anyone? Enter everyone's names, including yours, separated by commas. Leave blank to skip.
> Alice, Bob
How should it be split?
  1) Clean split -- everyone pays the same
  2) Slice split -- everyone pays for what they had
> 1
Each person owes:
  Alice: $35.90
  Bob: $35.89
```

## How it works

`main.py` starts the orchestrator, which runs the conversation and calls on the
other components:

| Module | Role |
|---|---|
| `agents/orchestrator.py` | Runs the conversation: order, edits, budget, mode, confirmation, receipt, bill split |
| `agents/retrieval_agent.py` | Reads messages (Gemini or the keyword parser), keeps the cart, and builds the order for each budget mode |
| `agents/order_parser.py` | The keyword parser, and validation of Gemini's replies |
| `agents/preference_agent.py` | Learns preference scores from order history |
| `agents/fulfillment_agent.py` | Validates and prices the order with Domino's, and splits the cost |
| `providers/dominos.py` | Domino's menu, pizza and topping matching, prices, and coupon search |
| `providers/base.py` | The interface another pizza chain would implement |
| `storage.py` | SQLite database of users, orders, preferences, and contacts |
| `matching.py` | Fuzzy matching of item names ("coke" finds "Coca-Cola") |
| `background.py` | Runs network calls on background threads |
| `preferences_cli.py` | Command-line tool for viewing and editing preferences |

### Budget modes

| Mode | What it does |
|---|---|
| **Premium** | Exactly what you asked for, plus the best coupon. Used automatically when there's no budget. |
| **Standard** | Gets under budget by giving up only low-priority preferences, such as a larger size or a topping you don't care much about. |
| **Smart** | Gets under budget first, giving up even high-priority preferences if it has to. If nothing fits, it picks the cheapest version. |

Among the options that fit, Standard and Smart pick the one that gives up the
least. Each line's alternatives (smaller sizes, no toppings) are combined into
whole-order candidates line by line, keeping only those no other candidate
beats on both price and preference, so large orders stay fast. If anything
changes, the user confirms it or falls back to the exact order.

### Preference learning

Each topping and the pizza size get a priority score from 0 to 1, learned from
order history with an exponential moving average. Ordering a topping and
keeping it raises its score; leaving it out, or accepting a substitution that
drops it, lowers it. Scores of 0.7 or more are high priority, which takes three
consistent orders from the neutral 0.5. Price sensitivity is learned the same
way from the modes a user picks, and decides which mode is recommended.

New users start from the averages of the closest group of people with enough
orders: their contacts (people they've split a bill with), then people in
their ZIP code, at their Domino's store, in their ZIP area, in their state, and
finally everyone. Their own orders gradually replace those averages.
Preferences set with `preferences_cli.py` override learned ones.

### Natural-language parsing

Each message goes to Gemini (`gemini-3.5-flash-lite` by default) along with a
JSON schema, so the reply is always structured data the app can apply to the
cart. Follow-ups include the current order as numbered lines, so the reply can
refer to items by line number. Requests stay small: the instructions live in
the schema rather than in a prompt, replies like "no" or "start over" are
handled locally, and the API is called directly over REST. A typical request
uses about 40 tokens and comes back in under a second.

Without an API key, or when Gemini fails or hits its rate limit, the keyword
parser in `agents/order_parser.py` reads messages instead.

### Background work

Network calls run while the user reads and types. The store lookup and menu
download start at launch, and the coupon search (several seconds, since Domino's
prices only a couple of carts at a time) starts each time the order is shown.
Every price check is cached, so the final receipt usually needs no extra
request.

## Getting started

Requires Python 3.10 or newer. A Gemini API key is optional; you can get a free
one from [Google AI Studio](https://aistudio.google.com).

```bash
pip install -r requirements.txt
cp .env.example .env                    # Windows: copy .env.example .env
python preferences_cli.py import-json   # optional: load the sample order history
python main.py
```

Fill in `.env` with your Gemini API key and your name, contact details, and
delivery address, which Domino's needs to find the nearest store and price the
order. Anything left blank falls back to placeholder values. `.env` holds
secrets, so it's excluded by `.gitignore`; never commit it.

## Usage

```bash
python main.py                    # order as the default user, jane_doe
python main.py --user alice       # order with another user's history and preferences
```

Viewing and editing preferences:

```bash
python preferences_cli.py users                                   # all users, with order counts
python preferences_cli.py show jane_doe                           # learned and recorded preferences
python preferences_cli.py set jane_doe --topping pepperoni=0.9 --size-priority 0.2
python preferences_cli.py unset jane_doe --topping pepperoni
python preferences_cli.py contacts add jane_doe alice bob
```

## Configuration

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Gemini API key. Without one, the keyword parser is used. |
| `GEMINI_MODEL` | Optional model override (default `gemini-3.5-flash-lite`). |
| `SPLITSLICE_FIRST_NAME`, `SPLITSLICE_LAST_NAME`, `SPLITSLICE_EMAIL`, `SPLITSLICE_PHONE` | Customer details sent to Domino's for pricing. |
| `SPLITSLICE_STREET`, `SPLITSLICE_CITY`, `SPLITSLICE_STATE`, `SPLITSLICE_ZIP` | Delivery address, used to find the nearest store. |
| `SPLITSLICE_DB` | Optional database path (default `data/splitslice.db`). |

## Data

Everything is stored in SQLite at `data/splitslice.db` (created on first run)
using Python's built-in `sqlite3`. Each order keeps both what was requested and
what was ordered, along with the substitutions the user accepted, so preference
learning can tell what they gave up. Each operation runs in a single
transaction, and the schema's constraints reject invalid sizes, modes, and
scores. `data/users/` holds sample order history as JSON, which
`preferences_cli.py import-json` loads.

## Limitations

- `pizzapi` is unofficial and undocumented, so Domino's could change its API at
  any time.
- Pizzas are always hand tossed, and toppings always cover the whole pizza
  (no half-and-half or extra toppings).
- Budget modes treat a pizza's toppings as one preference: they can drop all of
  "pepperoni & mushroom", but not just the mushroom.
- The keyword parser knows common pizza vocabulary but not shorthand like
  "lg pep". When it doesn't understand a reply, it says so rather than guessing.
- Only Domino's is implemented; `providers/base.py` defines what another chain
  would need.
