"""Shared deterministic parser for short conversational responses."""

import re
import unicodedata


RESPONSE_GROUPS = {
    "affirmative": {
        "yes", "y", "yeah", "yep", "yup", "sure", "okay", "ok",
        "affirmative", "go ahead", "add", "add more", "more", "another",
        "please do", "sounds good","ye","yeb","yee","yebski","yessir","k","kay","yea","yeaah","yeaa","yeaaah","yeaahh","yeaaahh","yesss","yessss","yesssss",
    },
    "negative": {
        "no", "n", "nope", "nah", "done", "finish", "finished",
        "that is all", "i am done", "all done", "nothing else", "no more",
        "that is it", "thats it", "no thanks", "no thank you","nop","na","nahhh",
    },
    "cancel": {
        "cancel", "cancel workout", "stop", "discard", "abort",
    },
    "skip": {"skip", "skip photo", "never mind", "nevermind", "forget it"},
    "unknown": {
        "i do not know", "do not know", "not sure", "unsure", "unknown",
        "no idea", "i have no idea", "i am not sure",
    },
}


# ----- standardize conversational text before matching -----
def normalize_response(text):
    """Apply Unicode, contraction, punctuation, and whitespace normalization."""
    value = unicodedata.normalize("NFKC", str(text)).lower().replace("’", "'")
    contractions = {
        r"\bthat's\b": "that is", r"\bthats\b": "that is",
        r"\bi'm\b": "i am", r"\bi've\b": "i have",
        r"\bi'll\b": "i will", r"\bi'd\b": "i would",
        r"\bdon't\b": "do not", r"\bdont\b": "do not",
        r"\bcan't\b": "cannot", r"\bcant\b": "cannot",
        r"\bisn't\b": "is not", r"\baren't\b": "are not",
    }
    for pattern, replacement in contractions.items():
        value = re.sub(pattern, replacement, value)
    value = re.sub(r"\bidk\b", "i do not know", value)
    value = re.sub(r"\bdunno\b", "do not know", value)
    value = "".join(
        " " if unicodedata.category(char).startswith("P") else char
        for char in value
    )
    return re.sub(r"\s+", " ", value).strip()


# ----- match a normalized response to a deterministic action -----
def classify_response(text):
    """Match complete normalized phrases only; never fuzzy-match descriptions."""
    normalized = normalize_response(text)
    for category, phrases in RESPONSE_GROUPS.items():
        if normalized in phrases:
            return category
    return "other"
