import re
from difflib import SequenceMatcher

NAME_ALIASES = {  # so "coke" matches "Coca-Cola"
    "coke": ["coca", "cola"],
    "pop": ["soda"],
    "soda": ["pop"],
    "breadsticks": ["bread"],
}


# Words that say nothing about which item is meant.
FILLER_WORDS = {"the", "and", "with", "for", "some", "of", "my", "our", "that", "this", "those", "these",
                "please", "add", "remove", "one", "two", "three", "another", "more"}


def name_words(text_l: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text_l))


def with_singulars(words) -> set:
    """The words plus simple singulars ("cokes" -> "coke", "tomatoes" -> "tomato")."""
    out = set(words)
    for word in words:
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            out.add(word[:-1])
        if len(word) > 4 and word.endswith("es"):
            out.add(word[:-2])
    return out


def best_name_match(query: str, candidates, threshold: float = 0.45):
    """The (key, name) among (key, name) candidates that best matches `query`,
    or (None, None) below `threshold`. Half the score is character similarity,
    half the share of the query's words (plurals and aliases included) in the
    name, so "diet coke" prefers "Diet Coke" over "Coke"."""
    query_l = query.lower()
    query_words = [w for w in name_words(query_l) if len(w) > 2 and w not in FILLER_WORDS]
    wanted = []  # acceptable spellings, one set per query word
    for word in query_words:
        forms = with_singulars([word])
        for form in list(forms):
            forms.update(NAME_ALIASES.get(form, []))
        wanted.append(forms)

    best_key, best_name, best_score = None, None, 0.0
    for key, name in candidates:
        name_l = name.lower()
        score = 0.0
        if wanted:
            have = with_singulars(name_words(name_l))
            score = 0.5 * sum(1 for forms in wanted if forms & have) / len(wanted)
        similarity = SequenceMatcher(None, query_l, name_l)
        # ratio() is slow; skip it when its cheap upper bounds can't beat the best so far.
        if score + 0.5 * similarity.real_quick_ratio() <= best_score or \
                score + 0.5 * similarity.quick_ratio() <= best_score:
            continue
        score += 0.5 * similarity.ratio()
        if score > best_score:
            best_key, best_name, best_score = key, name, score

    if best_score >= threshold:
        return best_key, best_name
    return None, None
