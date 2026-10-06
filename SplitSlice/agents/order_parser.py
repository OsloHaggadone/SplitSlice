import re
from dataclasses import dataclass, field
from typing import Optional

SIZE_WORDS = {"small": "small", "personal": "small", "medium": "medium", "large": "large",
              "x-large": "large", "xl": "large", "extra large": "large", "extra-large": "large"}

NUMBER_WORDS = {"a couple of": 2, "a couple": 2, "couple of": 2, "a pair of": 2, "a dozen": 12, "dozen": 12,
                "another": 1, "one more": 1, "an": 1, "a": 1, "one": 1, "two": 2, "three": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
MAX_QUANTITY = 99  # more of one item than this is a misread

# Topping words -> the names the provider knows.
TOPPINGS = {
    "italian sausage": "sausage", "green peppers": "green pepper", "green pepper": "green pepper",
    "banana peppers": "banana pepper", "banana pepper": "banana pepper", "black olives": "black olive",
    "black olive": "black olive", "jalapenos": "jalapeno", "jalapeno": "jalapeno", "jalapeños": "jalapeno",
    "jalapeño": "jalapeno", "philly steak": "philly steak", "diced tomatoes": "tomato", "tomatoes": "tomato",
    "tomato": "tomato", "peppers": "green pepper", "pepperoni": "pepperoni", "sausage": "sausage",
    "ham": "ham", "beef": "beef", "bacon": "bacon", "chicken": "chicken", "steak": "steak",
    "mushrooms": "mushroom", "mushroom": "mushroom", "onions": "onion", "onion": "onion",
    "pineapple": "pineapple", "olives": "olive", "olive": "olive", "spinach": "spinach",
    "garlic": "garlic", "feta": "feta",
}
# Specialty names -> the style the provider looks up.
SPECIALTIES = {
    "ultimate pepperoni": "ultimate pepperoni", "pacific veggie": "veggie", "honolulu hawaiian": "hawaiian",
    "memphis bbq chicken": "bbq", "bbq chicken": "bbq", "philly cheese steak": "philly",
    "philly cheesesteak": "philly", "cheesesteak": "philly", "wisconsin 6-cheese": "wisconsin",
    "wisconsin 6 cheese": "wisconsin", "wisconsin": "wisconsin", "6 cheese": "wisconsin",
    "six cheese": "wisconsin", "spicy chicken bacon ranch": "chicken bacon ranch",
    "chicken bacon ranch": "chicken bacon ranch", "extravaganzza": "extravaganzza",
    "extravaganza": "extravaganzza", "extravagansa": "extravaganzza", "meatzza": "meat", "meatza": "meat",
    "meat lovers": "meat", "meat lover's": "meat", "meat": "meat", "deluxe": "deluxe", "supreme": "supreme",
    "hawaiian": "hawaiian", "veggie": "veggie", "vegetarian": "veggie", "bbq": "bbq", "buffalo": "buffalo",
    "philly": "philly",
}
# Words that make a clause a drink or side, not a pizza.
SIDE_WORDS = {"wing", "wings", "bread", "breadstick", "breadsticks", "bites", "twists", "twist", "knots",
              "sandwich", "sandwiches", "pasta", "salad", "salads", "cake", "cakes", "brownie", "brownies",
              "cookie", "cookies", "dessert", "desserts", "coke", "cokes", "pepsi", "pepsis", "sprite",
              "sprites", "fanta", "soda", "sodas", "pop", "water", "waters", "drink", "drinks", "juice",
              "tea", "lemonade", "tots", "dip", "dips", "sauce", "sauces", "dressing", "boneless", "tenders",
              "fries", "chips", "dew", "beer", "liter", "cola", "dr"}

LEADING_FILLER = ["i would like", "i'd like", "i want", "i'll have", "i will have", "i'll take", "can i get",
                  "can i have", "could i get", "could i have", "can we get", "we want", "we'd like",
                  "give me", "get me", "let me get", "let me have", "please", "just", "also", "plus", "and",
                  "then", "some", "maybe", "oh", "um", "uh", "as well as", "along with", "now", "ok", "okay",
                  "alright", "so", "hi", "hello", "hey"]
TRAILING_FILLER = ["please", "thanks", "thank you", "too", "as well", "for me", "if possible"]

DONE_PHRASES = {"no", "nope", "nah", "no thanks", "no thank you", "thanks", "thank you", "nothing",
                "nothing else", "nothing more", "no more", "that's all", "thats all", "that's it", "thats it",
                "that's everything", "thats everything", "that will be all", "that'll be all", "all set",
                "all done", "done", "i'm done", "im done", "finished", "i'm finished", "im finished",
                "finish", "checkout", "check out", "good", "i'm good", "im good", "we're good", "were good",
                "for now", "i think", "i'm all set", "im all set"}
DONE_FILLER = {"please", "ok", "okay", "yeah", "yes", "so"}
CLEAR_PHRASES = {"start over", "start again", "cancel everything", "cancel the order", "cancel the whole order",
                 "cancel my order", "cancel all of it", "clear", "clear it", "clear everything", "clear the order",
                 "clear my order", "clear the cart", "remove everything", "delete everything",
                 "empty the cart", "empty my order", "never mind all of it"}

REMOVE_VERBS = ["get rid of", "take away", "take out", "no more", "remove", "delete", "cancel", "drop",
                "lose", "minus"]
NEGATE_VERBS = ["take off", "leave off", "hold the", "skip the", "without", "hold", "skip", "no"]
CHANGE_VERBS = ["change", "swap", "switch", "replace", "make", "turn", "upgrade", "downgrade"]
ADD_VERBS = ["i'd also like", "i also want", "can i also get", "throw in", "include", "add", "also",
             "plus", "another", "one more"]


@dataclass
class PizzaRequest:
    style: str = "plain"
    size: Optional[str] = None  # None: the caller's default size
    quantity: int = 1
    style_given: bool = True    # False if no style was said ("a medium")


@dataclass
class ExtraRequest:
    name: str
    quantity: int = 1


@dataclass
class OrderRequest:
    pizzas: list = field(default_factory=list)  # [PizzaRequest]
    extras: list = field(default_factory=list)  # [ExtraRequest]
    notes: list = field(default_factory=list)   # messages about what can't be done

    def __bool__(self) -> bool:
        return bool(self.pizzas or self.extras)


@dataclass
class Removal:
    target: str = ""                # the item in words (keyword parser)
    quantity: Optional[int] = None  # None: all of it
    index: Optional[int] = None     # the cart line, 0-based (Gemini)


@dataclass
class Change:
    target: Optional[str] = None        # None: the most recent pizza
    new: Optional[OrderRequest] = None  # what it becomes
    drop_topping: Optional[str] = None
    quantity: Optional[int] = None      # the new count
    index: Optional[int] = None         # the cart line, 0-based (Gemini)


@dataclass
class FollowUp:
    action: str  # "done", "clear" (then `add`), "edit", or "unclear"
    add: OrderRequest = field(default_factory=OrderRequest)
    remove: list = field(default_factory=list)  # [Removal]
    change: list = field(default_factory=list)  # [Change]
    message: str = ""
    finish: bool = False  # edits, then done: "add a coke and that's it"


# First message: "a large pepperoni and mushroom pizza, 2 diet cokes"

def parse_order(text: str) -> OrderRequest:
    request = OrderRequest()
    last_size = None
    negating = False  # inside a "without onions and olives" list
    for raw in _clauses(_normalize(text)):
        clause = _strip_filler(raw)
        if not clause:
            continue
        quantity, rest, counted = _leading_quantity(clause)
        if not rest:
            continue
        kind = _kind(rest)
        if kind == "negation":  # "..., no onions" / "..., no coke": nothing to add
            negating = True
            _leave_off(request, _find_phrases(rest, TOPPINGS))
            continue
        if kind == "side":
            negating = False
            request.extras.append(ExtraRequest(name=_item_name(rest), quantity=quantity))
            continue
        if kind == "unknown":
            negating = False
            request.extras.append(ExtraRequest(name=_item_name(rest), quantity=quantity))
            continue
        size = _size_in(rest)
        style, style_given, has_specialty = _style_in(rest)
        previous = request.pizzas[-1] if request.pizzas else None
        continues = (kind == "toppings_only" and not counted and size is None and previous is not None
                     and not has_specialty)
        if continues and negating:
            continue  # "...without onions and olives": olives are negated too
        if continues and _is_topping_list(previous.style):
            previous.style = _merge_styles(previous.style, style)
            previous.style_given = True
            continue
        negating = _has_negation(rest)
        if size:
            last_size = size
        request.pizzas.append(PizzaRequest(style=style, size=size or last_size, quantity=quantity,
                                           style_given=style_given))
        if negating:  # "a large hawaiian without pineapple"
            _leave_off(request, _find_phrases(rest[len(_without_negations(rest)):], TOPPINGS))
    return request


def _leave_off(request: OrderRequest, toppings: list) -> None:
    """Take declined toppings off the last pizza; a specialty can't, so it gets a note."""
    pizza = request.pizzas[-1] if request.pizzas else None
    if pizza is None or not toppings:
        return
    if _is_topping_list(pizza.style):
        kept = [part for part in pizza.style.split(" & ") if part not in toppings and part != "plain"]
        pizza.style = " & ".join(kept) or "plain"
    else:
        request.notes.append(f"Specialty pizzas come with their usual toppings, so the {pizza.style} pizza "
                             f"can't leave off {' or '.join(dict.fromkeys(toppings))}. To leave a topping off, "
                             "ask for a pizza with just the toppings you want instead.")


PIZZA_WORDS = {"pizza", "pizzas", "pie", "pies"}
# Words that can sit beside toppings in a toppings-only clause.
TOPPING_LIST_WORDS = PIZZA_WORDS | {"extra", "more", "double", "with", "on", "top", "and", "the", "some", "no",
                                    "without", "hold"}


def _kind(clause: str) -> str:
    """"negation" ("no onions"), "side", "pizza", "toppings_only", or "unknown"."""
    if not _without_negations(clause).strip():
        return "negation"
    words = set(re.findall(r"[a-z0-9'-]+", clause))
    has_pizza_word = bool(words & PIZZA_WORDS)
    if words & SIDE_WORDS and not has_pizza_word:
        return "side"
    style_text = _without_negations(clause)
    if _find_phrases(style_text, TOPPINGS) or _find_phrases(style_text, SPECIALTIES):
        leftover = clause
        for phrase in sorted({**TOPPINGS, **SPECIALTIES}, key=len, reverse=True):
            leftover = re.sub(rf"\b{re.escape(phrase)}\b", " ", leftover)
        leftover_words = set(re.findall(r"[a-z0-9'-]+", leftover)) - TOPPING_LIST_WORDS
        return "toppings_only" if not leftover_words else "pizza"
    if has_pizza_word or _size_in(clause) or re.search(r"\b(?:cheese|plain)\b", clause):
        return "pizza"
    return "unknown"


def _size_in(clause: str) -> Optional[str]:
    for phrase in sorted(SIZE_WORDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", clause):
            return SIZE_WORDS[phrase]
    return None


def _style_in(clause: str) -> tuple:
    """(style, style_given, has_specialty): the specialty, else the toppings joined by " & ", else "plain"."""
    text = _without_negations(clause)
    specialties = _find_phrases(text, SPECIALTIES)
    if specialties:
        return specialties[0], True, True
    toppings = _find_phrases(text, TOPPINGS)
    if toppings:
        return " & ".join(dict.fromkeys(toppings)), True, False
    return "plain", bool(re.search(r"\b(?:cheese|plain)\b", text)), False


def _find_phrases(text: str, vocabulary: dict) -> list:
    """Values of the vocabulary phrases in `text`, in order; the longest wins an overlap."""
    taken, found = [], []
    for phrase in sorted(vocabulary, key=len, reverse=True):
        for match in re.finditer(rf"\b{re.escape(phrase)}\b", text):
            span = match.span()
            if any(span[0] < end and start < span[1] for start, end in taken):
                continue
            taken.append(span)
            found.append((span[0], vocabulary[phrase]))
    return [value for _, value in sorted(found)]


def _without_negations(clause: str) -> str:
    return re.sub(r"\b(?:no|without|hold the|hold|minus|except|but no|skip the|leave off)\b.*$", "", clause)


def _has_negation(clause: str) -> bool:
    return _without_negations(clause) != clause


def _is_topping_list(style: str) -> bool:
    return style == "plain" or all(part in TOPPINGS.values() for part in style.split(" & "))


def _merge_styles(first: str, second: str) -> str:
    parts = [p for p in (first.split(" & ") + second.split(" & ")) if p and p != "plain"]
    return " & ".join(dict.fromkeys(parts)) or "plain"


def _item_name(clause: str) -> str:
    return re.sub(r"^(?:the|my|some)\s+", "", clause).strip()


# Follow-ups: "remove the coke and add a sprite", "make it a small", ...

def parse_followup(text: str) -> FollowUp:
    t = _normalize(text)
    if is_done(t):
        return FollowUp("done")
    if _strip_filler(re.sub(r"[.!?]+$", "", t)) in CLEAR_PHRASES:
        return FollowUp("clear")
    # Drop a leading "yes," / "actually" / "no," -- but not the "no" of "no pepperoni".
    previous = None
    while previous != t:
        previous = t
        t = re.sub(r"^(?:yes|yeah|yep|sure|ok|okay|alright|actually|wait|oh|um|uh|sorry|now|so)\b[\s,!.]*(?=\w)",
                   "", t)
        t = re.sub(r"^(?:no|nope|nah)\s*[,!.]+\s*(?=\w)", "", t)

    result = FollowUp("edit")
    add_texts = []
    for verb, rest in _segments(t):
        if verb == "remove":
            for target in _clauses(rest):
                quantity, name, counted = _leading_quantity(_strip_filler(target))
                name = _item_name(name)
                if name:
                    result.remove.append(Removal(name, quantity if counted else None))
        elif verb == "negate":
            name = _item_name(rest)
            topping = _find_phrases(name, TOPPINGS)
            if topping and len(_find_phrases(name, TOPPINGS)) == 1 and not _find_phrases(name, SPECIALTIES):
                result.change.append(Change(target=None, drop_topping=topping[0]))
            elif name:
                result.remove.append(Removal(name))
        elif verb == "change":
            change = _parse_change(rest)
            if change is None:
                return FollowUp("unclear", message=CHANGE_HELP)
            result.change.append(change)
        else:  # add
            instead = re.match(r"^(?P<new>.+?)\s+instead(?:\s+of\s+(?P<target>.+))?$", rest)
            if instead:
                change = _change_to(instead.group("target"), instead.group("new"))
                if change is None:
                    return FollowUp("unclear", message=CHANGE_HELP)
                result.change.append(change)
            else:
                add_texts.append(rest)
    result.add = parse_order(" and ".join(add_texts)) if add_texts else OrderRequest()
    return result


CHANGE_HELP = ("Sorry, I couldn't follow that change. You can say things like \"make it a small\", "
               "\"change the coke to a sprite\", or \"no pepperoni\" -- or remove an item and add the one you want.")


# A reply that ends the order after its edits: "add a coke and that's it", "..., nothing else".
_CLOSING = re.compile(
    r"(?:^|[\s,;.!]+)(?:and\s+)?(?:that'?s\s+(?:it|all|everything)|that(?:'ll|\s+will)\s+be\s+all"
    r"|i'?m\s+(?:all\s+)?done|(?:i'?m\s+)?all\s+set|nothing\s+(?:else|more))[\s.!]*$"
    r"|(?:,|\s+and)\s+done[\s.!]*$")


def strip_closing(text: str) -> tuple:
    """(the reply without a closing phrase, whether it had one)."""
    text = clean_text(text)
    match = _CLOSING.search(text.lower().replace("’", "'"))
    return (text[:match.start()].rstrip(" ,;.!"), True) if match else (text, False)


def is_done(text_l: str) -> bool:
    """Whether the reply is only "done" phrases ("nope, that's it") -- not just starting with one ("no pepperoni")."""
    t = " ".join(re.sub(r"[^a-z' ]+", " ", text_l.replace("’", "'")).split())
    phrases = sorted(DONE_PHRASES | DONE_FILLER, key=len, reverse=True)
    said_done = False
    while t:
        match = next((p for p in phrases if t == p or t.startswith(p + " ")), None)
        if match is None:
            return False
        said_done = said_done or match in DONE_PHRASES
        t = t[len(match):].strip()
    return said_done


def _segments(text: str) -> list:
    """(verb, rest) pieces, split only where a new verb starts: "remove the coke and
    the wings" is one removal; "remove the coke and add a sprite" is two pieces."""
    pieces = re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s*", text)
    segments = []
    for piece in pieces:
        piece = re.sub(r"^(?:(?:now|ok|okay|alright|so|oh|um|uh|actually)\b\s*)+", "", piece)
        if not piece:
            continue
        verb, rest = _leading_verb(piece)
        if verb or not segments:
            segments.append([verb or "add", rest if verb else piece])
        else:
            segments[-1][1] += " and " + piece
    return [(verb, rest.strip()) for verb, rest in segments if rest.strip()]


def _leading_verb(piece: str) -> tuple:
    for verb_kind, verbs in (("remove", REMOVE_VERBS), ("change", CHANGE_VERBS), ("negate", NEGATE_VERBS),
                             ("add", ADD_VERBS)):
        for verb in sorted(verbs, key=len, reverse=True):
            match = re.match(rf"^{re.escape(verb)}\b\s*", piece)
            if match:
                rest = piece[match.end():]
                if verb in ("another", "one more"):
                    rest = f"1 {rest}"  # "another coke" -> one coke
                return verb_kind, rest
    return None, piece


def _parse_change(rest: str) -> Optional[Change]:
    """What follows a change verb: "it a small", "the coke to a sprite", "it two"."""
    match = re.match(r"^(?P<target>it|that|them|this|those)\s+(?:(?:to|into|for|with)\s+)?(?P<new>.+)$", rest)
    if match:
        return _change_to(None, match.group("new"))
    match = re.match(r"^(?P<target>.+?)\s+(?:to|for|with|into)\s+(?P<new>.+)$", rest)
    if match:
        return _change_to(match.group("target"), match.group("new"))
    match = re.match(r"^(?P<target>.+?)\s+(?P<new>(?:an?\s+).+)$", rest)
    if match:
        return _change_to(match.group("target"), match.group("new"))
    match = re.match(r"^(?P<target>.+?)\s+(?P<new>(?:small|medium|large|x-large|personal))$", rest)
    if match:
        return _change_to(match.group("target"), match.group("new"))
    return None


def _change_to(target: Optional[str], new_text: str) -> Optional[Change]:
    target = _item_name(target or "")
    if target in ("", "it", "that", "them", "this", "those"):
        target = None
    new_text = _strip_filler(new_text.strip())
    quantity, rest, counted = _leading_quantity(new_text)
    if counted and not rest.strip(" s"):  # "make it two"
        return Change(target=target, quantity=quantity)
    new = parse_order(new_text)
    if not new:
        return None
    if not new.pizzas and not any(set(re.findall(r"[a-z0-9'-]+", e.name)) & SIDE_WORDS for e in new.extras):
        return None  # "make it fancy": no pizza, drink, or side
    return Change(target=target, new=new)


# Gemini's replies -> the same shapes

def order_from_ai(data) -> Optional[OrderRequest]:
    """A reply in ITEMS_SCHEMA's shape, cleaned."""
    if not isinstance(data, dict):
        return None
    pizzas = data.get("pizzas")
    request = OrderRequest()
    for pizza in pizzas if isinstance(pizzas, list) else []:
        if not isinstance(pizza, dict):
            continue
        raw_style = _text(pizza.get("style")).lower()
        style, given, _ = _style_in(raw_style)
        if not given and raw_style not in ("", "plain", "cheese"):
            style, given = raw_style, True  # an unknown name: the menu decides
        request.pizzas.append(PizzaRequest(style=style, size=_clean_size(pizza.get("size")),
                                           quantity=clean_quantity(pizza.get("quantity")), style_given=given))
    extras = data.get("extras")
    for extra in extras if isinstance(extras, list) else []:
        if isinstance(extra, dict) and _text(extra.get("name")):
            request.extras.append(ExtraRequest(_text(extra.get("name")), clean_quantity(extra.get("quantity"))))
    return request


def followup_from_ai(data) -> Optional[FollowUp]:
    """A reply in FOLLOWUP_SCHEMA's shape, cleaned, or None to let the keyword parser decide.
    "done" counts only without edits; "clear" keeps its additions ("start over with a hawaiian")."""
    if not isinstance(data, dict):
        return None
    result = FollowUp("edit")
    for item in data.get("remove") if isinstance(data.get("remove"), list) else []:
        if isinstance(item, dict) and _line_index(item) is not None:
            result.remove.append(Removal(quantity=_optional_quantity(item), index=_line_index(item)))
    for item in data.get("change") if isinstance(data.get("change"), list) else []:
        if isinstance(item, dict):
            change = _change_from_ai(item)
            if change:
                result.change.append(change)
    result.add = order_from_ai(data.get("add")) or OrderRequest()
    if data.get("clear") is True:
        return FollowUp("clear", add=result.add)
    if not (result.add or result.remove or result.change):
        return FollowUp("done") if data.get("done") is True else None
    return result


def _change_from_ai(item: dict) -> Optional[Change]:
    """A new style lists everything on the pizza, so it wins over drop_topping:
    "swap the pepperoni for sausage" (style "sausage", drop "pepperoni") is a sausage pizza."""
    index = _line_index(item)  # None: the most recent pizza
    quantity = _optional_quantity(item)
    size = _clean_size(item.get("size"))
    style_text = _text(item.get("style")).lower()
    drop = _find_phrases(_text(item.get("drop_topping")).lower(), TOPPINGS)
    replace_with = _text(item.get("replace_with"))
    if replace_with:
        return Change(index=index, new=OrderRequest(extras=[ExtraRequest(replace_with)]), quantity=quantity)
    if style_text:
        style, given, _ = _style_in(style_text)
        if not given:
            style, given = style_text, True
        return Change(index=index, quantity=quantity,
                      new=OrderRequest(pizzas=[PizzaRequest(style=style, size=size, style_given=given)]))
    if drop:  # may come with a size: "make it a small without pepperoni"
        new = OrderRequest(pizzas=[PizzaRequest(size=size, style_given=False)]) if size else None
        return Change(index=index, drop_topping=drop[0], new=new, quantity=quantity)
    if size:
        return Change(index=index, quantity=quantity,
                      new=OrderRequest(pizzas=[PizzaRequest(size=size, style_given=False)]))
    if quantity is not None:
        return Change(index=index, quantity=quantity)
    return None


def _optional_quantity(item: dict) -> Optional[int]:
    quantity = item.get("quantity")
    return None if quantity in (None, "", 0) else clean_quantity(quantity)


def _text(value) -> str:
    """A text field, or "" if missing or "null"/"none" (Gemini sometimes writes those out)."""
    text = str(value or "").strip()
    return "" if text.lower() in ("null", "none", "nothing", "n/a", "-") else text


def _line_index(item: dict) -> Optional[int]:
    """The 0-based cart line for a reply's 1-based "line", or None."""
    line = clean_quantity(item.get("line"), default=0)
    return line - 1 if line else None


# Helpers

def clean_quantity(value, default: int = 1) -> int:
    """A count from 1 to MAX_QUANTITY, else `default`."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if 1 <= number <= MAX_QUANTITY else default


def _clean_size(value) -> Optional[str]:
    return SIZE_WORDS.get(str(value or "").strip().lower())


# Invisible characters pipes and pastes carry (byte-order mark, zero-width
# spaces), and the non-breaking space.
_INVISIBLE = {ord(c): None for c in "﻿​‌‍⁠"} | {0xA0: " "}


def clean_text(text) -> str:
    return str(text or "").translate(_INVISIBLE).strip()


def _normalize(text: str) -> str:
    text = clean_text(text).lower().replace("’", "'").replace("&", " and ")
    return " ".join(text.split())


def _clauses(text: str) -> list:
    return [c.strip() for c in re.split(r"\s*(?:,|;|\band\b|\bplus\b|\balong with\b|\bthen\b)\s*", text)
            if c.strip()]


def _strip_filler(clause: str) -> str:
    clause = clause.strip(" .!?")
    changed = True
    while changed:
        changed = False
        for phrase in sorted(LEADING_FILLER, key=len, reverse=True):
            if clause == phrase or clause.startswith(phrase + " "):
                clause = clause[len(phrase):].strip()
                changed = True
                break
        for phrase in sorted(TRAILING_FILLER, key=len, reverse=True):
            if clause == phrase or clause.endswith(" " + phrase):
                clause = clause[:-len(phrase)].strip()
                changed = True
                break
    return clause


_UNIT = r"(?:-|\s)*(?:liter|litre|l|oz|ounce|piece|pc|pcs|inch|in|\")\b"


def _leading_quantity(clause: str) -> tuple:
    """(quantity, rest, counted) from "2 cokes", "a couple of", or a trailing "x2".
    "2 liter coke" and "16 piece wings" aren't counts."""
    trailing = re.search(r"\s+x\s*(\d+)$", clause)
    if trailing:
        return clean_quantity(trailing.group(1)), clause[:trailing.start()].strip(), True
    match = re.match(rf"^(\d+)(?!\d|{_UNIT})\s*x?\b\s*(?P<rest>.*)$", clause)
    if match:
        return clean_quantity(match.group(1)), match.group("rest").strip(), True
    for phrase in sorted(NUMBER_WORDS, key=len, reverse=True):
        if clause == phrase or clause.startswith(phrase + " "):
            return NUMBER_WORDS[phrase], clause[len(phrase):].strip(), True
    return 1, clause, False
