"""Local, per-user food memory matching and evidence handling."""

import re
import unicodedata

import database
from response_parser import classify_response


_WORD_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "half": "0.5",
}
_ALIASES = {
    "grams": "g", "gram": "g", "kilograms": "kg", "kilogram": "kg",
    "milliliters": "ml", "milliliter": "ml", "millilitres": "ml",
    "millilitre": "ml", "liters": "l", "litres": "l", "ounces": "oz",
    "ounce": "oz", "tablespoons": "tbsp", "tablespoon": "tbsp",
    "teaspoons": "tsp", "teaspoon": "tsp", "pieces": "piece", "pcs": "piece",
    "slices": "slice", "servings": "serving", "cups": "cup",
    "bottles": "bottle", "cans": "can",
    "pounds": "lb", "pound": "lb", "lbs": "lb",
}
_FILLER = {
    "i", "ate", "had", "have", "drank", "drink", "ordered", "got",
    "a", "an", "the", "please", "and", "with",
}
_PORTION_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|half)\s*(?:g|grams?|kg|ml|millilit(?:er|re)s?|l|lit(?:er|re)s?|lb|lbs|"
    r"oz|ounces?|cups?|tbsp|tablespoons?|tsp|teaspoons?|pieces?|pcs|slices?|"
    r"servings?|bottles?|cans?|eggs?|bananas?|apples?|bowls?|scoops?)\b"
    r"|\b(?:small|medium|large|regular|mini|king\s+size)\b"
    r"|\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:eggs?|bananas?|apples?|bowls?|scoops?|slices?|pieces?|servings?)\b"
    r"|\b\d+\s*x\b",
    re.IGNORECASE,
)
_PREPARATION_TERMS = (
    "deep fried", "air fried", "stir fried", "pan fried", "grilled",
    "roasted", "baked", "steamed", "boiled", "poached", "raw", "smoked",
)
_CUSTOMIZATION_TERMS = (
    "less sugar", "less sweet", "no sugar", "sugar free", "no ice",
    "less ice", "extra ice", "oat milk", "soy milk", "almond milk",
    "skim milk", "whole milk", "extra shot", "decaf", "light sauce",
    "extra sauce", "no sauce",
)


# ----- normalize food descriptions into comparable tokens -----
def normalized_tokens(text):
    value = unicodedata.normalize("NFKC", text or "").casefold()
    value = value.replace("&", " and ")
    value = value.replace("⁄", "/")
    value = re.sub(r"\b1\s*/\s*2\b", " 0.5 ", value)
    value = re.sub(r"\b1\s*/\s*4\b", " 0.25 ", value)
    value = re.sub(r"\b3\s*/\s*4\b", " 0.75 ", value)
    value = re.sub(r"\bless\s+sweet\b", "less sugar", value)
    value = re.sub(r"\bwithout\s+ice\b", "no ice", value)
    value = re.sub(r"\b(?:i\s+)?(?:ate|had|drank|ordered|got)\b", " ", value)
    raw = re.findall(r"\d+(?:\.\d+)?|[a-z]+", value)
    tokens = []
    for token in raw:
        token = _WORD_NUMBERS.get(token, _ALIASES.get(token, token))
        if token not in _FILLER:
            tokens.append(token)
    return tokens


# ----- build a stable identity for a described food -----
def canonical_identity(text):
    """Normalize punctuation, word order, number words, and common units."""
    return " ".join(sorted(normalized_tokens(text)))


# ----- combine a meal with its clarification history -----
def memory_description(description, clarification_history=None):
    parts = [description or ""]
    for exchange in clarification_history or []:
        answer = exchange.get("answer", "")
        if classify_response(answer) in {
            "affirmative", "negative", "unknown", "skip", "cancel"
        }:
            continue
        if answer:
            parts.append(answer)
    return " ".join(parts).strip()


# ----- check that a food has enough detail for personal reuse -----
def exact_match_ready(description, product_info=None, items=None):
    """Require a concrete food identity and explicit portion/size."""
    text = unicodedata.normalize("NFKC", description or "")
    if not _PORTION_RE.search(text):
        return False
    tokens = normalized_tokens(text)
    product = product_info or {}
    if product.get("identity_status") == "uncertain":
        return False
    brand = canonical_identity(product.get("brand") or "")
    named_product = canonical_identity(
        f"{product.get('product_name') or ''} {product.get('variant') or ''}"
    )
    if brand and (not named_product or named_product == brand):
        return False
    known_tokens = set(tokens)
    if brand and not set(brand.split()).issubset(known_tokens):
        return False
    if named_product and not set(named_product.split()).issubset(known_tokens):
        return False
    generic_items = {"meal", "food", "drink", "beverage", "serving", "portion"}
    for item in items or []:
        item_tokens = {
            token for token in normalized_tokens(item.get("name", ""))
            if token not in generic_items and not token.isdigit()
        }
        if item_tokens and not item_tokens.issubset(known_tokens):
            return False
    identity_tokens = [token for token in tokens if not token.isdigit() and token not in {
        "g", "kg", "ml", "l", "oz", "tbsp", "tsp", "piece", "slice",
        "serving", "cup", "bottle", "can", "small", "medium", "large",
        "regular", "mini", "size", "over",
    }]
    if len(identity_tokens) < 1:
        return False
    if any(token in {"rice", "noodle", "pasta", "soup", "salad", "tea", "coffee", "sushi", "sandwich"}
           for token in identity_tokens) and len(identity_tokens) < 2:
        return False
    return True


# ----- prepare confirmed food evidence for personal memory -----
def prepare_memory(description, result, evidence_category="ai_confirmed",
                   source_origin="ai_estimated", clarification_history=None,
                   photo_included=False, original_source_origin=None):
    """Build a serializable, confirmed-only memory entry."""
    if evidence_category not in {"verified", "user_corrected", "ai_confirmed"}:
        return None
    full_description = memory_description(description, clarification_history)
    identity = canonical_identity(full_description)
    if not identity:
        return None
    product = result.get("product_identification") or {}
    description_lower = unicodedata.normalize("NFKC", full_description).casefold()
    attributes = {
        "description": full_description,
        "meal_identity": identity,
        "tokens": normalized_tokens(full_description),
        "items": result.get("items", []),
        "major_ingredients": [item.get("name", "") for item in result.get("items", [])],
        "product_identification": product,
        "brand": product.get("brand"),
        "portion": _PORTION_RE.findall(full_description),
        "preparation": [term for term in _PREPARATION_TERMS if term in description_lower],
        "customizations": [term for term in _CUSTOMIZATION_TERMS if term in description_lower],
        "clarification_history": clarification_history or [],
        "photo_included": bool(photo_included),
    }
    return {
        "canonical_identity": identity,
        "attributes": attributes,
        "description": full_description,
        "items": result.get("items", []),
        "product_info": product or None,
        "calories": int(result["total_calories"]),
        "evidence_category": evidence_category,
        "source_origin": source_origin,
        "original_source_origin": (
            original_source_origin or
            ("ai_estimated" if source_origin == "manual_correction" else source_origin)
        ),
        "exact_ready": int(exact_match_ready(
            full_description, product, result.get("items", [])
        )),
    }


# ----- find this user’s exact confirmed food match -----
def find_exact_memory(user_id, description):
    identity = canonical_identity(description)
    if not identity or not exact_match_ready(description):
        return None
    return database.get_food_memory_exact(user_id, identity)


# ----- retrieve a few similar foods for context -----
def find_similar_memories(user_id, description, limit=3):
    query_tokens = set(normalized_tokens(description))
    if not query_tokens:
        return []
    query_text = " ".join(query_tokens)
    candidates = []
    for memory in database.get_food_memories(user_id, limit=200):
        attributes = memory.get("attributes") or {}
        tokens = set(attributes.get("tokens") or normalized_tokens(memory.get("description")))
        if not tokens:
            continue
        product = attributes.get("product_identification") or {}
        brand = " ".join(normalized_tokens(product.get("brand") or ""))
        # Avoid offering a branded memory when the current description names
        # a different or unspecified brand.
        if brand and not set(brand.split()).issubset(query_tokens):
            continue
        overlap = len(query_tokens & tokens)
        score = overlap / len(query_tokens | tokens)
        if score >= 0.18:
            memory["similarity"] = score
            candidates.append(memory)
    candidates.sort(
        key=lambda item: (
            item["similarity"],
            item["evidence_category"] == "user_corrected",
            item["last_used_at"] or item["created_at"],
        ),
        reverse=True,
    )
    return candidates[:max(0, min(int(limit), 3))]


# ----- rebuild an estimate from a confirmed personal match -----
def exact_match_estimate(memory):
    items = memory.get("items") or []
    if not items:
        items = [{"name": memory["description"], "calories": memory["calories"]}]
    else:
        # Keep stored components while ensuring their displayed sum matches
        # the corrected personal reference total.
        items = [dict(item) for item in items]
        old_total = sum(int(item.get("calories", 0)) for item in items)
        if old_total != int(memory["calories"]):
            items = [{"name": memory["description"], "calories": int(memory["calories"])}]
    return {
        "items": items,
        "total_calories": int(memory["calories"]),
        "clarification_needed": False,
        "question": None,
        "food_identity_status": "sufficient",
        "estimate_reliable": True,
        "reliability_reason": None,
        "product_identification": memory.get("product_info"),
        "calorie_basis": "personal_memory",
        "nutrition_verified": memory.get("evidence_category") == "verified",
    }


# ----- format prior food examples for estimation context -----
def memory_context(examples, image_included=False):
    if not examples:
        return None
    history = []
    for entry in examples[:3]:
        history.append({
            "meal": entry["description"],
            "items": entry.get("items") or [],
            "calories": int(entry["calories"]),
            "evidence_category": entry["evidence_category"],
            "original_source": entry.get("original_source_origin") or entry["source_origin"],
        })
    if image_included:
        instruction = (
            "Use the attached meal photo and current description as primary evidence. "
            "Compare visible components and approximate portions with these personal "
            "historical examples. Adjust the estimate when the image shows a material "
            "difference. If the photo is unclear, state that it does not verify a match. "
            "Historical calories are estimates, not nutrition facts."
        )
    else:
        instruction = (
            "These are up to three same-user historical estimates for context, not "
            "verified nutrition facts. Use them to improve consistency while adjusting "
            "for meaningful differences in ingredients, portion, brand, preparation, "
            "or customization. Repeated use does not increase their evidence quality."
        )
    return {"similar_meal_examples": history, "instruction": instruction}


