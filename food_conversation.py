"""Deterministic food conversation decisions using the shared response parser."""

from response_parser import classify_response, normalize_response


_BRAND_ONLY_FILLER = {
    "i", "had", "have", "got", "ordered", "drank", "from", "a", "the",
    "please", "drink", "beverage", "food", "item",
}


# ----- decide whether food identity needs one clarification -----
def require_product_clarification(description, result, allow_clarification=True):
    """Ask what was ordered when a recognized seller is the whole entry.

    Brand recognition alone is not enough to estimate a specific product.
    This generic guard intentionally contains no brand vocabulary.
    """
    if not allow_clarification or result.get("clarification_needed"):
        return result
    product = result.get("product_identification") or {}
    brand = normalize_response(product.get("brand") or "")
    if not brand:
        return result
    product_name = normalize_response(product.get("product_name") or "")
    variant = normalize_response(product.get("variant") or "")
    if (
        (product_name and "".join(product_name.split()) != "".join(brand.split()))
        or variant
    ):
        return result

    normalized_description = normalize_response(description).split()
    meaningful = [word for word in normalized_description
                  if word not in _BRAND_ONLY_FILLER]
    if "".join(meaningful) != "".join(brand.split()):
        return result

    updated = dict(result)
    updated.update({
        "clarification_needed": True,
        "question": f"What specific food or drink did you have from {product.get('brand')}?",
        "estimate_reliable": False,
        "reliability_reason": None,
        "items": [],
        "total_calories": 0,
    })
    product_copy = dict(product)
    product_copy["identity_status"] = "uncertain"
    updated["product_identification"] = product_copy
    return updated


# ----- resolve the user’s photo choice without ai -----
def photo_choice_action(state, text):
    response = classify_response(text)
    if response == "affirmative":
        return "await_photo"
    if response == "negative" or response == "skip":
        # During photo selection, conversational cancellation means skip photo.
        return "estimate_without_photo"
    if response == "cancel":
        return "cancel"
    return "invalid"


# ----- resolve a response according to the active food state -----
def food_state_response(state, text):
    """Resolve deterministic food replies before any AI-backed handler."""
    if state in {"awaiting_photo_choice", "awaiting_photo"}:
        return photo_choice_action(state, text)
    if state == "clarifying":
        return clarification_action(text)
    if state == "awaiting_route_choice":
        return food_route_selection(text) or (
            "cancel" if classify_response(text) == "cancel" else "invalid"
        )
    return None


# ----- interpret a food clarification control response -----
def clarification_action(text):
    response = classify_response(text)
    if response == "cancel":
        return "cancel"
    if response in {"negative", "unknown", "skip"}:
        return "estimate_from_original"
    return "merge_reply"


# ----- enforce the one-question food limit -----
def should_ask_food_clarification(clarification_count, result):
    return clarification_count < 1 and result.get("clarification_needed") is True


# ----- resolve an explicit food-or-exercise selection -----
def food_route_selection(text):
    normalized = normalize_response(text)
    if normalized == "food":
        return "food"
    if normalized == "exercise":
        return "exercise"
    return None


# ----- detect messages needing food-or-exercise disambiguation -----
def is_ambiguous_food_exercise_message(text):
    """Recognize exact dual-use food/exercise names before AI routing."""
    return normalize_response(text) in {"dip", "dips"}


# ----- preserve the original meal and clarification reply -----
def clarification_context(pending, answer):
    """Build cumulative context so the original meal is always retained."""
    history = list(pending.get("history", []))
    history.append({"question": pending.get("last_question"), "answer": answer})
    return {
        "original_description": pending["description"],
        "previous_estimate": pending.get("estimate"),
        "clarification_history": history,
        "instruction": (
            "Estimate the COMPLETE original meal using this clarification. "
            "Preserve every original food component unless explicitly removed. "
            "Do not invent major ingredients. This is the only clarification; "
            "make a reasonable estimate or mark it unreliable."
        ),
    }
