
import os
import logging
import secrets
import asyncio

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from ai_estimator import (
    estimate_food_calories,
    classify_food_reply,
)
from response_parser import classify_response, normalize_response
from food_conversation import (
    photo_choice_action,
    clarification_action,
    should_ask_food_clarification,
    food_route_selection,
    clarification_context,
)

from database import (
    init_db,
    save_food,
    get_food_by_date,
    get_daily_total,
    update_food_calories,
    delete_food,
    get_weight_history,
    get_latest_weight,
    get_effective_calorie_target,
    set_pending_date_override,
    get_pending_date_override,
    consume_pending_date_override,
    clear_pending_date_override,
)

from intent_classifier import classify_message
from exercise_estimator import (
    estimate_exercise,
    extract_strength_exercise,
    merge_strength_exercise,
)

from database import (
    save_exercise,
    get_exercise_by_date,
    save_workout_session,
)
from strength_training import (
    estimate_session_calories,
    infer_duration_minutes,
    known_cardio_session_duration,
    cardio_met,
    strength_session_met,
    is_duration_unknown,
    parse_duration_minutes,
    workout_text_action,
    should_ask_exercise_clarification,
    merge_known_exercise_fields,
    is_unknown_answer,
)
from calorie_bank import (
    DEFAULT_BASE_TARGET,
    get_bank_deposits,
    calculate_daily_summary,
    recalculate_all,
    recalculate_user,
)
from weight_tracking import (
    STARTING_WEIGHT_KG,
    STARTING_TARGET_KCAL,
    extract_weight_update,
    can_auto_record_weight,
    record_weight as record_weight_measurement,
    rolling_average as rolling_weight_average,
    weight_for_exercise,
    validate_weight_kg,
)
from goal_tracking import (
    cancel_goal as cancel_weight_goal,
    create_goal as create_weight_goal,
    format_infeasible_plan,
    goal_overview,
    parse_goal_deadline,
    refresh_all_goals,
)
from monthly_report import (
    parse_month,
    monthly_data,
    render_calendar,
    format_caption,
)
from food_memory import (
    exact_match_estimate,
    find_exact_memory,
    find_similar_memories,
    memory_context,
    prepare_memory,
)
from historical_logging import previous_logging_date, day_summary_date
from clear_service import clear_user_data


# ----- configuration -----

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

TIMEZONE = ZoneInfo("Asia/Singapore")
DAILY_TARGET = DEFAULT_BASE_TARGET
CLEAR_REQUEST_TTL_SECONDS = 300
_clear_request_locks = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# ----- initialize state for a meal awaiting review -----
def new_meal(description):
    return {
        "id": secrets.token_hex(6),
        "description": description,
        "base_description": description,
        "image_bytes": None,
        "estimate": None,
        "clarifications": 0,
        "history": [],
        "last_question": None,
    }


# ----- clear the user’s pending conversation state -----
def clear_pending(context):
    context.user_data.pop("pending_food", None)
    context.user_data.pop("food_state", None)


# ----- retrieve the current pending meal entry -----
def get_pending(context):
    return context.user_data.get("pending_food")


# ----- consume a user's next-entry date or use today's singapore date -----
def _consume_logging_date(user_id):
    return (
        consume_pending_date_override(user_id)
        or datetime.now(TIMEZONE).date().isoformat()
    )


# ----- start a new food entry and offer photo input -----
async def begin_food_entry(
    message, context, description, memory_match=None, logging_date=None
):
    pending = new_meal(description)
    pending["memory_match"] = memory_match or find_exact_memory(
        message.from_user.id, description
    )
    pending["logging_date"] = logging_date or _consume_logging_date(
        message.from_user.id
    )
    context.user_data["pending_food"] = pending
    context.user_data["food_state"] = "awaiting_photo_choice"
    await message.reply_text("Would you like to add a photo?")


# ----- build the food estimate action buttons -----
def confirmation_keyboard(meal_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Confirm",
                callback_data=f"food_confirm:{meal_id}",
            ),
            InlineKeyboardButton(
                "Edit",
                callback_data=f"food_edit:{meal_id}",
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=f"food_cancel:{meal_id}",
            ),
        ]
    ])


# ----- build the standalone exercise action buttons -----
def exercise_keyboard(entry_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Confirm",
                callback_data=f"exercise_confirm:{entry_id}",
            ),
            InlineKeyboardButton(
                "Edit",
                callback_data=f"exercise_edit:{entry_id}",
            ),
            InlineKeyboardButton(
                "Cancel",
                callback_data=f"exercise_cancel:{entry_id}",
            ),
        ]
    ])


# ----- build the workout summary action buttons -----
def workout_confirm_keyboard(session_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm", callback_data=f"workout_confirm:{session_id}"),
        InlineKeyboardButton("Edit", callback_data=f"workout_edit:{session_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"workout_cancel:{session_id}"),
    ]])



# ----- format a food estimate for telegram review -----
def format_estimate(result):
    lines = ["Estimated calories:\n"]

    product = result.get("product_identification")
    if product:
        details = [product.get(key) for key in (
            "brand", "product_name", "variant", "customization", "serving_size"
        ) if product.get(key)]
        if details:
            lines.append("Product: " + " · ".join(details))
        if product.get("serving_assumption"):
            lines.append("Serving assumption: " + product["serving_assumption"])

    for item in result["items"]:
        lines.append(
            f"{item['name']}: {item['calories']} kcal"
        )

    lines.append(
        f"\nTotal: {result['total_calories']} kcal"
    )

    lines.append(
        "\nThese are approximate calorie estimates."
    )
    if result.get("memory_reused"):
        evidence = result.get("memory_evidence")
        if evidence == "user_corrected":
            lines.append("Reused from your corrected personal meal history; this is not verified nutrition data.")
        else:
            lines.append("Reused from your confirmed meal history; this is not independently verified nutrition data.")
    if product and product.get("brand"):
        basis = result.get("calorie_basis")
        if basis == "personal_memory":
            lines.append("Personal history estimate; official nutrition was not verified.")
        else:
            estimate_label = (
                "Comparable-product estimate"
                if basis == "approximate_comparable"
                else "Typical-serving estimate"
            )
            lines.append(f"{estimate_label}; official nutrition was not verified.")

    return "\n".join(lines)


# ----- present a meal estimate and its confirmation actions -----
async def show_estimate(message, context, pending, result):

    pending.setdefault("logging_date", datetime.now(TIMEZONE).date().isoformat())
    pending["estimate"] = result
    if pending.get("estimate_source") != "user_corrected":
        pending["memory_basis"] = result

    if result["clarification_needed"]:
        if should_ask_food_clarification(pending.get("clarifications", 0), result):
            pending["clarifications"] += 1
            context.user_data["food_state"] = "clarifying"
            pending["last_question"] = result["question"]
            await message.reply_text(result["question"])
            return
        # The Python state limit has been reached. Continue with available
        # information, or report that a reliable estimate is unavailable.
        result["clarification_needed"] = False
        result["question"] = None
        if not result["items"]:
            result["estimate_reliable"] = False

    if not result.get("estimate_reliable", True):
        clear_pending(context)
        reason = result.get("reliability_reason")
        message_text = "I can't estimate this meal reliably from the available details without guessing ingredients. Please restart with a clearer meal description or use /cancel."
        if reason:
            message_text += f"\n\n{reason}"
        await message.reply_text(message_text)
        return

    context.user_data["food_state"] = "confirming"

    await message.reply_text(
        f"Logging date: {pending['logging_date']}\n\n{format_estimate(result)}",
        reply_markup=confirmation_keyboard(pending["id"]),
    )


# ----- estimate a pending meal and manage its clarification -----
async def analyze_meal(message, context, pending):

    context.user_data["food_processing"] = True

    try:
        await message.reply_text("Analyzing your food...")

        memory_match = pending.get("memory_match")
        if memory_match and pending["image_bytes"] is None:
            result = exact_match_estimate(memory_match)
            result["memory_reused"] = True
            result["memory_evidence"] = memory_match["evidence_category"]
            pending["memory_reuse_id"] = memory_match["id"]
            pending["estimate_source"] = "memory_reused"
            await show_estimate(message, context, pending, result)
            return

        pending["memory_reuse_id"] = None
        pending["estimate_source"] = "ai_estimated"
        similar = find_similar_memories(
            message.from_user.id, pending.get("base_description", pending["description"])
        )
        context_with_memory = memory_context(
            similar, image_included=pending["image_bytes"] is not None
        )
        if similar:
            pending["similar_memory_ids"] = [item["id"] for item in similar]

        result = await estimate_food_calories(
            pending["description"],
            previous_context=context_with_memory,
            allow_clarification=(pending.get("clarifications", 0) < 1),
            image_bytes=pending["image_bytes"],
        )

        await show_estimate(
            message, context, pending, result
        )

    except Exception:
        logger.exception("Food estimation failed")

        await message.reply_text(
            "Sorry, food analysis failed. "
            "Use /cancel to restart or try again."
        )

    finally:
        context.user_data["food_processing"] = False


# ----- combine a meal clarification and estimate once -----
async def process_food_clarification(message, context, pending, reply, action):
    """Apply the single allowed reply, or estimate from original details only."""
    context.user_data["food_processing"] = True
    context.user_data["food_state"] = "clarification_processing"
    try:
        if action == "estimate_from_original":
            previous_context = {
                "original_description": pending["description"],
                "previous_estimate": pending.get("estimate"),
                "instruction": (
                    "The user cannot or will not provide more detail. Do not "
                    "treat the reply as an ingredient. Estimate only from the "
                    "original meal, using explicit reasonable assumptions. If "
                    "the food cannot be identified without inventing major "
                    "ingredients, mark the estimate unreliable."
                ),
            }
            examples = find_similar_memories(
                message.from_user.id, pending.get("base_description", pending["description"])
            )
            if examples:
                previous_context["personal_food_memory"] = memory_context(
                    examples, image_included=pending["image_bytes"] is not None
                )
            result = await estimate_food_calories(
                pending["description"],
                previous_context=previous_context,
                allow_clarification=False,
                image_bytes=pending["image_bytes"],
            )
            pending["history"].append({
                "question": pending.get("last_question"),
                "answer": reply,
            })
        else:
            response_type = classify_response(reply)
            if response_type == "affirmative":
                reply_type = "clarification"
            else:
                reply_type = await classify_food_reply(
                    original_description=pending["description"],
                    question=pending.get("last_question", ""),
                    reply=reply,
                )

            if reply_type == "replacement":
                clear_pending(context)
                await begin_food_entry(
                    message,
                    context,
                    reply,
                    logging_date=pending.get("logging_date"),
                )
                return

            previous_context = clarification_context(pending, reply)
            examples = find_similar_memories(
                message.from_user.id,
                f"{pending.get('base_description', pending['description'])} {reply}",
            )
            if examples:
                previous_context["personal_food_memory"] = memory_context(
                    examples, image_included=pending["image_bytes"] is not None
                )
            result = await estimate_food_calories(
                reply,
                previous_context=previous_context,
                allow_clarification=False,
                image_bytes=pending["image_bytes"],
            )
            pending["history"].append({
                "question": pending.get("last_question"),
                "answer": reply,
            })

        pending["estimate_source"] = "ai_estimated"
        pending["memory_reuse_id"] = None

        await show_estimate(message, context, pending, result)
    except Exception:
        logger.exception("Food clarification processing failed")
        context.user_data["food_state"] = "clarification_error"
        await message.reply_text(
            "I couldn't finish that estimate. The clarification has already "
            "been used, so I won't ask again. Use /cancel or send a new meal description to restart."
        )
    finally:
        context.user_data["food_processing"] = False


# ----- format a body-weight value for display -----
def _format_weight(value):
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


# ----- build the latest-weight and target summary -----
def _weight_overview(user_id, today_date):
    latest = get_latest_weight(user_id)
    average = rolling_weight_average(user_id, today_date)
    current_target = get_effective_calorie_target(
        user_id, today_date.isoformat(), STARTING_TARGET_KCAL
    )
    lines = ["Weight overview"]
    if latest:
        lines.append(
            f"Latest weight: {_format_weight(latest['weight_kg'])} kg "
            f"({latest['measurement_date']} {latest['measurement_time']} Singapore)"
        )
    else:
        lines.append(
            f"No weight recorded yet. Exercise estimates use the configured "
            f"starting weight of {STARTING_WEIGHT_KG:g} kg."
        )
    if average["average_kg"] is None:
        lines.append("Seven-day average: no measurements in the current seven-day window")
    else:
        lines.append(
            f"Seven-day average: {_format_weight(average['average_kg'])} kg "
            f"({average['measured_days']} measured days)"
        )
    lines.append(f"Current daily calorie target: {current_target} kcal")
    return "\n".join(lines)


# ----- format a newly recorded weigh-in response -----
def _format_weight_record(result):
    lines = [
        f"Weight recorded: {_format_weight(result['weight_kg'])} kg "
        f"({result['measured_at'].strftime('%Y-%m-%d %H:%M')} Singapore)",
    ]
    if result["average_kg"] is None:
        lines.append("Seven-day average: unavailable; no measurements in the current window")
    else:
        lines.append(
            f"Seven-day average: {_format_weight(result['average_kg'])} kg "
            f"({result['measured_days']} measured days)"
        )
    lines.append(f"Current daily calorie target: {result['current_target']} kcal")
    if result["target_changed"]:
        lines.append(
            f"New target: {result['new_target']} kcal/day, effective "
            f"{result['target_date'].isoformat()}."
        )
    else:
        lines.append(
            f"Target effective tomorrow remains {result['target_tomorrow']} kcal/day."
        )
    goal_update = result.get("goal_update")
    if goal_update and goal_update.get("plan") and not goal_update["plan"].get("feasible"):
        lines.append(format_infeasible_plan(goal_update["plan"]))
        lines.append(
            "The goal deadline was not changed. The configured fixed deficit "
            f"target is used from {goal_update['target_date']}."
        )
    elif goal_update and goal_update["goal"].get("status") == "reached":
        lines.append("Goal reached. Future targets use the configured fixed deficit.")
    return "\n".join(lines)


# ----- show or record the user’s body weight -----
async def weight_command(update, context):
    user_id = update.effective_user.id
    if not context.args:
        today_date = datetime.now(TIMEZONE).date()
        await update.message.reply_text(_weight_overview(user_id, today_date))
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /weight or /weight 80.5")
        return
    try:
        weight = validate_weight_kg(context.args[0])
        result = record_weight_measurement(user_id, weight)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return
    await update.message.reply_text(_format_weight_record(result))


# ----- list the user’s recent weight measurements -----
async def weight_history_command(update, context):
    rows = get_weight_history(update.effective_user.id, limit=10)
    if not rows:
        await update.message.reply_text("No weight measurements recorded yet.")
        return
    lines = ["Recent weight measurements:"]
    lines.extend(
        f"{row['measurement_date']} {row['measurement_time']} — "
        f"{_format_weight(row['weight_kg'])} kg"
        for row in rows
    )
    await update.message.reply_text("\n".join(lines))


# ----- show, create, or cancel a weight-loss calorie goal -----
async def goal_command(update, context):
    user_id = update.effective_user.id
    arguments = list(getattr(context, "args", []) or [])
    today = datetime.now(TIMEZONE).date()
    if len(arguments) == 1 and arguments[0].casefold() == "cancel":
        result = cancel_weight_goal(user_id, today)
        if result is None:
            await update.message.reply_text("You do not have an active weight-loss goal.")
            return
        note = (
            " The configured starting weight was used because there is no measurement in the current seven-day window."
            if result["used_starting_weight"] else ""
        )
        await update.message.reply_text(
            "Goal cancelled. Your configured 400 kcal/day deficit target will be "
            f"{result['target_kcal']} kcal/day effective {result['target_date']}." + note
        )
        return
    if not arguments:
        await update.message.reply_text(_format_goal_overview(goal_overview(user_id, today)))
        return
    if len(arguments) != 2:
        await update.message.reply_text(
            "Usage: /goal, /goal cancel, /goal <target kg> <days>, or "
            "/goal <target kg> YYYY-MM-DD. Example: /goal 75 120"
        )
        return
    try:
        target_weight = validate_weight_kg(arguments[0])
        deadline = parse_goal_deadline(arguments[1], today)
        plan = create_weight_goal(user_id, target_weight, deadline, today)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return
    await update.message.reply_text(_format_goal_created(plan))


# ----- format the details of a newly scheduled weight-loss goal -----
def _format_goal_created(plan):
    return (
        "🎯 Weight-loss goal set\n"
        f"Current seven-day average: {_format_weight(plan['current_weight_kg'])} kg\n"
        f"Target weight: {_format_weight(plan['target_weight_kg'])} kg\n"
        f"Weight to lose: {_format_weight(plan['weight_to_lose_kg'])} kg\n"
        f"Deadline: {plan['deadline']} ({plan['days_remaining']} days)\n"
        f"Estimated required deficit: {plan['required_deficit_kcal']:.1f} kcal/day\n"
        f"New base target: {plan['daily_target_kcal']} kcal/day\n"
        f"Effective from: {plan['target_date']}\n"
        "The 7,700 kcal/kg conversion is an approximate planning estimate, "
        "not a prediction."
    )


# ----- format current goal progress and safety status -----
def _format_goal_overview(overview):
    if not overview["goal"]:
        average = overview["average_kg"]
        average_text = (
            f"{_format_weight(average)} kg" if average is not None
            else "unavailable (no measurements in the current seven-day window)"
        )
        return (
            "No active weight-loss goal.\n"
            f"Seven-day average: {average_text}\n"
            f"Current daily target: {overview['current_target']} kcal/day "
            "using the configured 400 kcal/day deficit."
        )
    goal = overview["goal"]
    average = overview["average_kg"]
    lines = [
        "🎯 Weight-loss goal",
        f"Status: {goal['status']} ({goal['calculation_status']})",
        f"Current seven-day average: {_format_weight(average) + ' kg' if average is not None else 'unavailable'}",
        f"Target weight: {_format_weight(goal['target_weight_kg'])} kg",
        f"Deadline: {goal['deadline']} ({overview['days_remaining']} days remaining)",
        f"Progress: {overview['progress_percent']:.0f}%",
        f"Current daily target: {overview['current_target']} kcal/day",
    ]
    plan = overview.get("plan")
    if plan and not plan.get("feasible"):
        lines.append(format_infeasible_plan(plan))
        lines.append("Future targets use the configured fixed deficit until the goal is safely feasible.")
    elif plan:
        lines.append(f"Estimated required deficit: {plan['required_deficit_kcal']:.1f} kcal/day")
    elif goal["calculation_status"] == "needs_weight":
        lines.append("Record a weight in the current seven-day window to recalculate the goal target.")
    elif goal["status"] == "reached":
        lines.append("Target reached. Future targets use the configured fixed deficit again.")
    elif goal["status"] == "deadline_passed":
        lines.append("The deadline passed; it has not been extended. Future targets use the configured fixed deficit.")
    elif goal["status"] == "cancelled":
        lines.append("This goal was cancelled. Future targets use the configured fixed deficit.")
    return "\n".join(lines)


TELEGRAM_TEXT_LIMIT = 4096


# ----- split a long reply within telegram message limits -----
def _split_telegram_text(text, limit=TELEGRAM_TEXT_LIMIT):
    """Split plain text into Telegram-sized chunks without dropping characters."""
    if limit < 1:
        raise ValueError("Message limit must be positive.")
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:]
    if text or not chunks:
        chunks.append(text)
    return chunks


# ----- serialize reset requests for a single telegram user -----
def _clear_request_lock(user_id):
    user_id = int(user_id)
    if user_id not in _clear_request_locks:
        _clear_request_locks[user_id] = asyncio.Lock()
    return _clear_request_locks[user_id]


# ----- build the first-stage reset scope buttons -----
def _clear_scope_keyboard(request_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Clear Logged History",
            callback_data=f"clear:history:{request_id}",
        )],
        [InlineKeyboardButton(
            "Clear Everything",
            callback_data=f"clear:everything:{request_id}",
        )],
        [InlineKeyboardButton("Cancel", callback_data=f"clear:cancel:{request_id}")],
    ])


# ----- build the final reset confirmation buttons -----
def _clear_confirmation_keyboard(request_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, Delete", callback_data=f"clear:confirm:{request_id}"),
        InlineKeyboardButton("Cancel", callback_data=f"clear:cancel:{request_id}"),
    ]])


# ----- build the command and feature guide -----
def _help_text():
    return (
        "📖 Calorie Tracker Help\n\n"
        "🍽️ FOOD LOGGING\n"
        "Send a meal in your own words, for example: “chicken rice and iced tea”. "
        "The bot estimates calories and may ask one combined clarification "
        "question. Answer with the missing detail, or say “not sure” to estimate "
        "from what is known. Review the result and choose Confirm, Edit, or Cancel. "
        "Confirmed meals are saved to your food history.\n\n"
        "📷 PHOTOS\n"
        "After describing a meal, answer Yes to send a photo or No to continue "
        "from text. A caption is optional. While waiting for a photo, send “skip”, "
        "“no”, or “never mind” to continue without one. Confirm the estimate "
        "before it is saved.\n\n"
        "🏋️ EXERCISE LOGGING\n"
        "Describe an activity naturally, for example “bench press 50 kg, 4 sets "
        "of 8” or “ran 5 km in 30 minutes”. Strength workouts can include "
        "multiple exercises; cardio and strength may be combined. After each "
        "exercise, send another activity or say “done” / “no” to finish. The bot "
        "may ask at most one clarification per exercise, then asks once for total "
        "session duration including rests if it is unknown. Say “not sure” for a "
        "conservative duration estimate. Review the result and choose Confirm, "
        "Edit, or Cancel.\n\n"
        "🔥 EXERCISE CALORIE BANK\n"
        "With no outstanding debt, estimated additional exercise calories are "
        "split 50/50: half adds to that day's allowance and half becomes a "
        "positive bank credit. Credits are available on the three calendar days "
        "after earning and expire at the end of the third day (Singapore time); "
        "the earliest-expiring credit is used first. While debt remains, 75% of "
        "exercise calories repays the oldest debt and 25% adds to that day's "
        "allowance. If an exercise clears all debt, its remaining calories return "
        "to the 50/50 split. Debt does not expire. A fully repaid debt within "
        "seven calendar days marks its original day as recovered.\n\n"
        "⚖️ WEIGHT\n"
        "The target uses the seven-day rolling average: the latest measurement "
        "from each measured day in the current and previous six Singapore-time "
        "calendar days. Missing days are not filled in. New target changes take "
        "effect tomorrow; exercise calorie estimates use your latest measured "
        "weight. Use /goal to plan weight loss by a chosen deadline; its target "
        "updates daily within configured safety limits.\n\n"
        "🧹 CLEAR DATA\n"
        "Choose Clear Logged History to remove food/exercise logs, workouts, "
        "calorie-bank activity, and daily progress while keeping saved meals, "
        "weight history, goals, and targets. Choose Clear Everything to also remove "
        "personalized meal memory, weights, goals, and targets. First choose a scope, "
        "then press Yes, Delete on the warning to proceed. Cancel at either step "
        "keeps your data. Deletion is permanent.\n\n"
        "📅 MONTHLY CALENDAR\n"
        "Green = finalized day within allowance; red = finalized day over "
        "allowance; teal = debt fully recovered within seven days; grey = no food "
        "logged / incomplete; yellow = today, still "
        "provisional; future dates are neutral. The success rate counts only "
        "finalized on-target and over-target days.\n\n"
        "📋 COMMANDS\n"
        "/start — Open the tracker. Example: /start\n"
        "/help — Show this guide. Example: /help\n"
        "/cancel — Cancel the pending food or exercise entry and unused /prev date. "
        "Example: /cancel\n"
        "/clear — Choose a reset scope, then confirm with Yes, Delete. "
        "Example: /clear\n"
        "/day [N] — View the summary N calendar days ago; omit N or use 0 for "
        "today. This only views data. Examples: /day, /day 0, /day 1\n"
        "/prev N — Set the next new food or exercise entry to N calendar days ago. "
        "This changes the logging date. Example: /prev 1 (log the next entry for yesterday)\n"
        "/bank — Show available bank calories and deposit expiry dates. "
        "Example: /bank\n"
        "/month [YYYY-MM] — Send this month's calendar, or a selected month. "
        "Example: /month 2026-03\n"
        "/history — Show food entries from the last seven days. Example: /history\n"
        "/editfood <food_id> <calories> — Change a saved meal's calories. "
        "Example: /editfood 12 550\n"
        "/deletefood <food_id> — Delete a saved meal. Example: /deletefood 12\n"
        "/weight — Show latest weight, seven-day average, and current target. "
        "Example: /weight\n"
        "/weight <kg> — Record a measurement in kilograms. Example: /weight 80.5\n"
        "/weighthistory — Show up to ten recent measurements. "
        "Example: /weighthistory\n"
        "/goal — View goal progress and target. Examples: /goal\n"
        "/goal <kg> <days|YYYY-MM-DD> — Set a weight-loss target. "
        "Example: /goal 75 120\n"
        "/goal cancel — Cancel the goal and return to the configured 400 kcal/day deficit."
    )


# ----- welcome the user and point to the help guide -----
async def start(update, context):
    await update.message.reply_text(
        "Welcome! Send a meal or exercise to log it. Use /help for commands, "
        "including /goal and /clear reset options."
    )


# ----- send the help guide in telegram-sized messages -----
async def help_command(update, context):
    for part in _split_telegram_text(_help_text()):
        await update.message.reply_text(part)


# ----- begin a two-stage request to clear personal tracker data -----
async def clear_command(update, context):
    if getattr(context, "args", None):
        await update.message.reply_text("Usage: /clear")
        return
    user_id = update.effective_user.id
    if context.user_data.get("food_processing"):
        await update.message.reply_text(
            "Please wait for the current analysis or save to finish before requesting a reset."
        )
        return
    async with _clear_request_lock(user_id):
        request_id = secrets.token_hex(8)
        context.user_data["clear_request"] = {
            "id": request_id,
            "user_id": int(user_id),
            "stage": "scope",
            "expires_at": datetime.now(TIMEZONE).timestamp()
            + CLEAR_REQUEST_TTL_SECONDS,
        }
        await update.message.reply_text(
            "Choose what to clear. Any deletion is permanent and cannot be undone.",
            reply_markup=_clear_scope_keyboard(request_id),
        )


# ----- validate reset callbacks and execute the selected deletion -----
async def handle_clear_buttons(update, context):
    query = update.callback_query
    user_id = update.effective_user.id
    async with _clear_request_lock(user_id):
        request = context.user_data.get("clear_request")
        if getattr(query.from_user, "id", None) != user_id:
            await query.answer("This reset request belongs to another user.", show_alert=True)
            return
        if (
            not request
            or request.get("user_id") != int(user_id)
            or query.data.rsplit(":", 1)[-1] != request.get("id")
        ):
            await query.answer("This reset button is stale or already used.", show_alert=True)
            return
        if datetime.now(TIMEZONE).timestamp() > request.get("expires_at", 0):
            context.user_data.pop("clear_request", None)
            await query.answer(
                "This reset request expired. Send /clear to start again.",
                show_alert=True,
            )
            return

        action = query.data.split(":")[1]
        if action == "cancel":
            context.user_data.pop("clear_request", None)
            await query.answer()
            await query.edit_message_text("Clear request cancelled. No data was deleted.")
            return

        if action in {"history", "everything"} and request.get("stage") == "scope":
            request["stage"] = "confirm"
            request["scope"] = action
            if action == "history":
                warning = (
                    "⚠️ Permanently delete your food and exercise logs, workouts, "
                    "calorie-bank activity, and daily progress? Saved meals, weight "
                    "history, goals, and calorie targets will be preserved. This cannot be undone."
                )
            else:
                warning = (
                    "⚠️ Permanently delete all food and exercise history, saved meal "
                    "memory, weight history, goals, calorie targets, and derived progress? "
                    "Your tracker will return to its initial settings. This cannot be undone."
                )
            await query.answer()
            await query.edit_message_text(
                warning,
                reply_markup=_clear_confirmation_keyboard(request["id"]),
            )
            return

        if action != "confirm" or request.get("stage") != "confirm":
            await query.answer("This reset action is no longer available.", show_alert=True)
            return
        if context.user_data.get("food_processing"):
            await query.answer(
                "Please wait for the current analysis or save to finish, then confirm again.",
                show_alert=True,
            )
            return

        await query.answer()
        if context.user_data.get("food_processing"):
            await query.message.reply_text(
                "Please wait for the current analysis or save to finish, then confirm again."
            )
            return
        try:
            clear_user_data(user_id, request["scope"])
        except Exception:
            logger.exception("Personal data reset failed for user %s", user_id)
            await query.message.reply_text(
                "The reset could not be completed. No partial deletion was kept; "
                "you can try confirming again."
            )
            return

        scope = request["scope"]
        context.user_data.clear()
        if scope == "history":
            response = (
                "Your food and exercise history has been cleared. Your saved meals, "
                "weight history and personal settings have been preserved."
            )
        else:
            response = (
                "Your tracking data and personalized memory have been cleared. "
                "Your calorie tracker has been reset to its initial settings."
            )
        await query.edit_message_text(response)


# ----- set a one-time date for the next new entry -----
async def prev_command(update, context):
    user_data = context.user_data
    if (
        user_data.get("food_processing")
        or user_data.get("pending_food")
        or user_data.get("pending_exercise")
        or user_data.get("pending_workout")
    ):
        await update.message.reply_text(
            "Finish or cancel your current food or exercise entry before using /prev."
        )
        return

    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /prev N (for example, /prev 1 for yesterday)."
        )
        return
    try:
        target_date = previous_logging_date(context.args[0])
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    user_id = update.effective_user.id
    replaced_date = get_pending_date_override(user_id)
    set_pending_date_override(user_id, target_date)
    replacement = (
        f" Replaced the unused date {replaced_date}." if replaced_date else ""
    )
    await update.message.reply_text(
        f"Your next new food or exercise entry will be logged for "
        f"{target_date}.{replacement}"
    )


# ----- discard the active food or workout entry -----
async def cancel_command(update, context):
    clear_pending_date_override(update.effective_user.id)

    if context.user_data.get("food_processing"):
        await update.message.reply_text(
            "Please wait for the current analysis to finish."
        )
        return

    had_workout = context.user_data.get("pending_workout") is not None
    had_exercise = context.user_data.get("pending_exercise") is not None
    clear_pending(context)
    context.user_data.pop("pending_exercise", None)
    context.user_data.pop("exercise_state", None)
    context.user_data.pop("pending_workout", None)
    context.user_data.pop("workout_state", None)
    context.user_data.pop("pending_route_text", None)

    if had_workout:
        response = "Workout session cancelled."
    elif had_exercise:
        response = "Exercise entry cancelled."
    else:
        response = "Pending food entry cancelled."
    await update.message.reply_text(response)


# ----- estimate a standalone exercise and request review -----
async def process_exercise(
    message,
    context,
    description,
    previous_context=None,
):
    context.user_data["food_processing"] = True

    try:
        await message.reply_text(
            "Analyzing your exercise..."
        )

        current_weight = weight_for_exercise(message.from_user.id)
        result = await estimate_exercise(
            description=description,
            weight_kg=current_weight,
            previous_context=previous_context,
        )

        pending = context.user_data.get("pending_exercise")

        if pending is None:
            pending = {
                "id": secrets.token_hex(6),
                "description": description,
                "history": [],
                "logging_date": _consume_logging_date(message.from_user.id),
            }
            context.user_data["pending_exercise"] = pending

        if result["clarification_needed"]:
            context.user_data["exercise_state"] = "clarifying"
            pending["last_question"] = result["question"]

            await message.reply_text(
                result["question"]
            )
            return

        pending["estimate"] = result
        context.user_data["exercise_state"] = "confirming"

        await message.reply_text(
            f"Logging date: {pending['logging_date']}\n\n"
            "Estimated Exercise Calories\n\n"
            f"Activity: {result['activity']}\n"
            f"Duration: {result['duration_minutes']:g} minutes\n"
            f"Estimated additional calories: "
            f"{result['additional_calories']} kcal\n\n"
            "This is an estimate, not a measured value.",
            reply_markup=exercise_keyboard(pending["id"]),
        )

    except Exception:
        logger.exception("Exercise estimation failed")

        await message.reply_text(
            "Couldn't estimate the exercise. "
            "Please try again or use /cancel."
        )

    finally:
        context.user_data["food_processing"] = False


# ----- apply a correction or clarification to an exercise -----
async def handle_exercise_reply(
    update,
    context,
):
    pending = context.user_data["pending_exercise"]
    state = context.user_data["exercise_state"]
    reply = update.message.text.strip()

    if state == "editing":
        numeric = reply.replace(",", "")

        if numeric.isdigit():
            calories = int(numeric)

            if not 0 <= calories <= 10000:
                await update.message.reply_text(
                    "Enter a calorie value between 0 and 10000."
                )
                return

            pending["estimate"]["additional_calories"] = calories
            context.user_data["exercise_state"] = "confirming"

            await update.message.reply_text(
                f"Logging date: {pending.get('logging_date', datetime.now(TIMEZONE).date().isoformat())}\n"
                f"Updated exercise calories: {calories} kcal",
                reply_markup=exercise_keyboard(pending["id"]),
            )
            return

    previous_context = {
        "original_description": pending["description"],
        "previous_estimate": pending.get("estimate"),
        "clarification_history": pending["history"],
        "latest_question": pending.get("last_question"),
        "instruction": (
            "Combine the original exercise with the user's "
            "latest clarification or correction. "
            "Do not treat the reply as a separate workout."
        ),
    }

    await process_exercise(
        update.message,
        context,
        reply,
        previous_context=previous_context,
    )

    if context.user_data.get("pending_exercise") is pending:
        pending["history"].append({
            "question": previous_context["latest_question"],
            "answer": reply,
        })


# ----- start the correct workout flow for an activity -----
async def route_exercise_description(message, context, description):
    context.user_data["food_processing"] = True
    try:
        extracted = await extract_strength_exercise(description)
    except Exception:
        logger.exception("Exercise type extraction failed")
        await message.reply_text("Couldn't identify that exercise. Please try again.")
        return
    finally:
        context.user_data["food_processing"] = False
    if extracted.get("is_workout_activity"):
        pending_workout = {
            "id": secrets.token_hex(6),
            "exercises": [],
            "logging_date": _consume_logging_date(message.from_user.id),
        }
        context.user_data["pending_workout"] = pending_workout
        await add_strength_exercise(message, context, description, extracted)
    else:
        await process_exercise(message, context, description)


# ----- format recorded activities and totals for review -----
def workout_summary(pending):
    pending.setdefault("logging_date", datetime.now(TIMEZONE).date().isoformat())
    lines = ["Workout summary", f"Logging date: {pending['logging_date']}", ""]
    for index, exercise in enumerate(pending["exercises"], 1):
        details = []
        activity_type = exercise.get("activity_type", "strength")
        if activity_type == "cardio":
            if exercise.get("duration_minutes") is not None:
                details.append(f"{exercise['duration_minutes']:g} min")
            if exercise.get("distance_km") is not None:
                details.append(f"{exercise['distance_km']:g} km")
            if exercise.get("pace_min_per_km") is not None:
                details.append(f"{exercise['pace_min_per_km']:g} min/km")
            if exercise.get("intensity"):
                details.append(exercise["intensity"])
        if exercise.get("sets") is not None:
            details.append(f"{exercise['sets']} sets")
        if exercise.get("repetitions") is not None:
            details.append(f"{exercise['repetitions']} reps")
        if exercise.get("weight_kg") is not None:
            details.append(f"{exercise['weight_kg']:g} kg")
        if exercise.get("resistance_type"):
            details.append(exercise["resistance_type"])
        if exercise.get("resistance_level"):
            details.append(f"resistance {exercise['resistance_level']}")
        if exercise.get("power_watts") is not None:
            details.append(f"{exercise['power_watts']:g} W")
        if exercise.get("speed_kmh") is not None:
            details.append(f"{exercise['speed_kmh']:g} km/h")
        if exercise.get("hold_duration_seconds") is not None:
            details.append(f"{exercise['hold_duration_seconds']} sec hold")
        suffix = f" ({', '.join(details)})" if details else ""
        type_label = "Cardio: " if activity_type == "cardio" else "Strength: "
        met = exercise.get("met") if activity_type == "cardio" else pending.get("met")
        stated_effort = (exercise.get("intensity") or "").lower()
        if any(word in stated_effort for word in ("light", "easy", "low", "gentle")):
            effort = "light"
        elif any(word in stated_effort for word in (
            "vigorous", "hard", "intense", "sprint", "hiit", "high effort",
        )):
            effort = "vigorous"
        else:
            effort = "light" if met and met < 3.5 else (
                "light-to-moderate" if met and met < 5.0 else (
                    "moderate" if met and met < 7.0 else "vigorous"
                )
            )
        detail = f"; {effort}" if met else ""
        lines.append(f"{index}. {type_label}{exercise['name']}{suffix}{detail}")
    source_labels = {
        "inferred": "estimated from exercise details and rest assumptions",
        "reported": (
            "reported by you, including rest periods"
            if any(item.get("activity_type") == "strength" for item in pending["exercises"])
            else "reported by you"
        ),
        "activity_reported": "sum of reported cardio durations",
        "activity_estimated": "calculated from reported distance and pace",
    }
    source = source_labels.get(pending["duration_source"], "estimated")
    lines.extend([
        "", f"Duration: {pending['duration_minutes']:g} minutes ({source})",
        f"Estimated additional calories: {pending['estimate']} kcal",
        "Approximate net MET estimate; cardio and strength minutes do not overlap.",
    ])
    if any(
        item.get("activity_type") == "cardio"
        and item.get("distance_km") is not None
        and item.get("duration_minutes") is None
        and item.get("pace_min_per_km") is None
        for item in pending["exercises"]
    ):
        lines.append("Assumption: distance-only cardio uses a conservative activity-specific pace.")
    if any(item.get("met_source") == "estimated" for item in pending["exercises"]):
        lines.append("Assumption: light-to-moderate activity-specific METs used where effort was not stated.")
    if any(item.get("power_watts") is not None for item in pending["exercises"]):
        lines.append("Assumption: cycling MET uses the reported power and your recorded weight.")
    if any(item.get("resistance_level") or item.get("resistance_type")
           for item in pending["exercises"]):
        lines.append("Resistance details are recorded as stated; machine levels are not converted to watts or weight.")
    if any(item.get("activity_type") == "strength" for item in pending["exercises"]):
        lines.append("Strength MET includes the logged rest periods; the default is light-to-moderate effort.")
    return "\n".join(lines)


# ----- extract and add one activity to the workout session -----
async def add_strength_exercise(message, context, description, extracted=None):
    pending = context.user_data.get("pending_workout")
    if extracted is None:
        context.user_data["food_processing"] = True
        try:
            extracted = await extract_strength_exercise(description)
        except Exception:
            logger.exception("Strength exercise extraction failed")
            await message.reply_text("Couldn't read that exercise. Please try again or /cancel.")
            return
        finally:
            context.user_data["food_processing"] = False

    if not extracted.get("is_workout_activity"):
        await message.reply_text("I couldn't identify that as a strength or cardio activity. Please describe the exercise again.")
        return
    if pending is None:
        pending = {
            "id": secrets.token_hex(6),
            "exercises": [],
            "logging_date": _consume_logging_date(message.from_user.id),
        }
        context.user_data["pending_workout"] = pending
    if should_ask_exercise_clarification(extracted, clarification_count=0):
        pending["active_exercise"] = extracted
        pending["exercise_clarification_count"] = 1
        context.user_data["workout_state"] = "awaiting_exercise_clarification"
        await message.reply_text(extracted["question"])
        return
    pending["exercises"].append(extracted)
    context.user_data["workout_state"] = "awaiting_more"
    await message.reply_text(
        f"Recorded: {extracted['name']}.\n\nAnything else?",
    )


# ----- record an exercise after its single clarification -----
async def finish_exercise_clarification(message, context, reply):
    pending = context.user_data.get("pending_workout")
    if not pending or not pending.get("active_exercise"):
        context.user_data["workout_state"] = "awaiting_more"
        await message.reply_text("Exercise recorded. Anything else?")
        return
    original = pending["active_exercise"]
    if is_unknown_answer(reply):
        merged = merge_known_exercise_fields(original, {})
    else:
        context.user_data["food_processing"] = True
        try:
            reply_fields = await merge_strength_exercise(original, reply)
            merged = merge_known_exercise_fields(original, reply_fields)
        except Exception:
            logger.exception("Exercise clarification merge failed")
            # The one permitted reply has been consumed. Keep known values and
            # leave unknown fields null rather than asking again or inventing.
            merged = merge_known_exercise_fields(original, {})
        finally:
            context.user_data["food_processing"] = False
    pending["exercises"].append(merged)
    pending.pop("active_exercise", None)
    pending.pop("exercise_clarification_count", None)
    context.user_data["workout_state"] = "awaiting_more"
    await message.reply_text(f"Recorded: {merged['name']}. Anything else?")


# ----- ask once for any missing whole-session duration -----
async def request_workout_duration(message, context):
    pending = context.user_data.get("pending_workout")
    if pending:
        known_duration = known_cardio_session_duration(pending["exercises"])
        if known_duration is not None:
            minutes, source = known_duration
            source = "activity_reported" if source == "reported" else "activity_estimated"
            await finalize_workout_duration(
                message, context, "", duration_override=(minutes, source)
            )
            return
    context.user_data["workout_state"] = "awaiting_duration"
    if pending:
        pending["button_id"] = secrets.token_hex(6)
    await message.reply_text(
        "How long was the whole session, including rest periods?\n"
        "For example: 45 min or 1 hour 10 minutes. You can also say “Not sure”."
    )


# ----- calculate the complete workout estimate for review -----
async def finalize_workout_duration(message, context, text, duration_override=None):
    pending = context.user_data.get("pending_workout")
    if not pending:
        context.user_data.pop("workout_state", None)
        return
    try:
        if duration_override is not None:
            duration, source = duration_override
        elif is_duration_unknown(text):
            duration = infer_duration_minutes(pending["exercises"])
            source = "inferred"
        else:
            duration = parse_duration_minutes(text)
            if duration is None:
                raise ValueError("Please include a duration, such as 45 min, or say “Not sure”.")
            source = "reported"
    except ValueError as error:
        await message.reply_text(str(error))
        return
    pending["duration_minutes"] = duration
    pending["duration_source"] = source
    current_weight = weight_for_exercise(message.from_user.id)
    pending["weight_kg"] = current_weight
    pending["met"] = strength_session_met(pending["exercises"])
    for exercise in pending["exercises"]:
        if exercise.get("activity_type") == "cardio":
            exercise["met"] = cardio_met(exercise, current_weight)
            if exercise.get("met_source") != "user_reported":
                exercise["met_source"] = "estimated"
    pending["estimate"] = estimate_session_calories(
        current_weight,
        duration,
        met=pending.get("met", 3.5),
        activities=pending["exercises"],
    )
    pending["button_id"] = secrets.token_hex(6)
    context.user_data["workout_state"] = "confirming"
    await message.reply_text(workout_summary(pending), reply_markup=workout_confirm_keyboard(pending["button_id"]))


# ----- route incoming text through active conversations and logging -----
async def handle_text(update, context):

    description = update.message.text.strip()

    if not description:
        return

    if context.user_data.get("food_processing"):
        await update.message.reply_text(
            "Please wait for the current analysis."
        )
        return

    workout_state = context.user_data.get("workout_state")
    if workout_state == "awaiting_exercise_clarification":
        await finish_exercise_clarification(update.message, context, description)
        return

    if workout_state in {"awaiting_more", "collecting_exercises"}:
        control = workout_text_action(workout_state, description)
        if control == "cancel":
            context.user_data.pop("pending_workout", None)
            context.user_data.pop("workout_state", None)
            await update.message.reply_text("Workout session cancelled.")
            return
        if control == "finish":
            await request_workout_duration(update.message, context)
            return
        if control == "add":
            context.user_data["workout_state"] = "collecting_exercises"
            await update.message.reply_text("Send the next exercise, or say “done” to finish.")
            return
        await add_strength_exercise(update.message, context, description)
        return

    if workout_state == "awaiting_duration":
        if workout_text_action(workout_state, description) == "duration":
            await finalize_workout_duration(update.message, context, description)
        return

    if workout_state == "confirming":
        await update.message.reply_text("Please use Confirm, Edit, or Cancel for this workout.")
        return

    if workout_state == "editing":
        pending = context.user_data.get("pending_workout")
        try:
            numeric = description.replace(",", "")
            if numeric.isdigit() and 0 <= int(numeric) <= 10000:
                pending["estimate"] = int(numeric)
            else:
                duration = parse_duration_minutes(description)
                if duration is None:
                    raise ValueError("Enter corrected calories or a duration such as 45 min.")
                pending["duration_minutes"] = duration
                pending["duration_source"] = "reported"
                pending["estimate"] = estimate_session_calories(
                    pending["weight_kg"], duration, pending["met"],
                    activities=pending.get("exercises", []),
                )
        except (ValueError, TypeError) as error:
            await update.message.reply_text(str(error))
            return
        context.user_data["workout_state"] = "confirming"
        pending["button_id"] = secrets.token_hex(6)
        await update.message.reply_text(workout_summary(pending), reply_markup=workout_confirm_keyboard(pending["button_id"]))
        return

    # Current food states take priority over new-message routing.
    food_state = context.user_data.get("food_state")
    food_pending = get_pending(context)
    if food_state == "awaiting_route_choice":
        route = food_route_selection(description)
        if route == "food":
            original = context.user_data.pop("pending_route_text", description)
            await begin_food_entry(update.message, context, original)
        elif route == "exercise":
            original = context.user_data.pop("pending_route_text", description)
            context.user_data.pop("food_state", None)
            await route_exercise_description(update.message, context, original)
        elif classify_response(description) == "cancel":
            context.user_data.pop("pending_route_text", None)
            context.user_data.pop("food_state", None)
            await update.message.reply_text("Entry cancelled.")
        else:
            await update.message.reply_text("Please type “food” or “exercise”.")
        return

    if food_state == "clarification_error":
        clear_pending(context)

    if food_state in {"awaiting_photo_choice", "awaiting_photo"} and food_pending:
        action = photo_choice_action(food_state, description)
        if action == "await_photo":
            context.user_data["food_state"] = "awaiting_photo"
            await update.message.reply_text(
                "Please send one photo of your meal. Reply “skip” or “never mind” to continue without a photo."
            )
        elif action == "estimate_without_photo":
            context.user_data["food_state"] = "analyzing"
            await analyze_meal(update.message, context, food_pending)
        elif action == "cancel":
            clear_pending(context)
            await update.message.reply_text("Food entry cancelled.")
        else:
            prompt = (
                "Would you like to add a photo?"
                if food_state == "awaiting_photo_choice"
                else "Please send a photo, or reply “no”, “skip”, or “never mind” to continue without one."
            )
            await update.message.reply_text(prompt)
        return

    if food_state == "clarifying" and food_pending:
        action = clarification_action(description)
        if action == "cancel":
            clear_pending(context)
            await update.message.reply_text("Food clarification cancelled.")
            return
        await process_food_clarification(update.message, context, food_pending, description, action)
        return

    
    # Exercise conversations take priority
    exercise_state = context.user_data.get("exercise_state")

    if exercise_state in {"clarifying", "editing"}:
        await handle_exercise_reply(update, context)
        return

    if exercise_state == "confirming":
        await update.message.reply_text(
            "Please Confirm, Edit, or Cancel "
            "your exercise entry first."
        )
        return


    state = context.user_data.get("food_state")
    pending = get_pending(context)

    # Waiting for a photo choice
    if state in {"awaiting_photo_choice", "awaiting_photo"}:
        return

    # Existing estimate waiting for confirmation
    if state == "confirming":
        await update.message.reply_text(
            "Please Confirm, Edit, or Cancel "
            "your current estimate first."
        )
        return

# ----- editing -----

    if state == "editing" and pending:

        context.user_data["food_processing"] = True

        try:
            numeric_text = description.replace(",", "")

            if numeric_text.isdigit():
                calories = int(numeric_text)

                if calories <= 0:
                    await update.message.reply_text(
                        "Enter a positive calorie amount."
                    )
                    return

                result = {
                    "items": [{
                        "name": "Manually adjusted meal",
                        "calories": calories,
                    }],
                    "total_calories": calories,
                    "clarification_needed": False,
                    "question": None,
                    "product_identification": (
                        pending.get("memory_basis", {}).get("product_identification")
                    ),
                }
                pending["estimate_source"] = "user_corrected"
                pending["memory_reuse_id"] = None

            else:
                await update.message.reply_text(
                    "Updating your estimate..."
                )

                result = await estimate_food_calories(
                    description,
                    previous_context={
                        "original_description":
                            pending["description"],
                        "previous_estimate":
                            pending["estimate"],
                        "instruction":
                            "Apply this correction to the "
                            "complete existing meal.",
                    },
                    allow_clarification=False,
                    image_bytes=pending["image_bytes"],
                )
                pending["description"] = (
                    f"{pending['description']}\nCorrection: {description}"
                )
                pending["history"].append({
                    "question": "Meal correction",
                    "answer": description,
                })
                pending["estimate_source"] = "ai_estimated"
                pending["memory_reuse_id"] = None

            await show_estimate(
                update.message, context, pending, result
            )

        except Exception:
            logger.exception("Food edit failed")
            await update.message.reply_text(
                "Couldn't update the estimate. Try again."
            )

        finally:
            context.user_data["food_processing"] = False

        return

# ----- new meal -----

    if can_auto_record_weight(context.user_data):
        weight_update = extract_weight_update(description)
        if weight_update is not None:
            try:
                result = record_weight_measurement(
                    update.effective_user.id, weight_update
                )
            except ValueError as error:
                await update.message.reply_text(str(error))
                return
            await update.message.reply_text(_format_weight_record(result))
            return

    exact_memory = find_exact_memory(update.effective_user.id, description)
    if exact_memory:
        await begin_food_entry(
            update.message, context, description, memory_match=exact_memory
        )
        return

    # Classify only when starting a new entry
    context.user_data["food_processing"] = True
    try:
        category = await classify_message(description)

    except Exception:
        logger.exception("Message classification failed")

        await update.message.reply_text(
            "Couldn't identify the message type. "
            "Please try again."
        )
        return
    finally:
        context.user_data["food_processing"] = False

    if category == "exercise":
        await route_exercise_description(update.message, context, description)
        return

    if category == "ambiguous":
        context.user_data["pending_route_text"] = description
        context.user_data["food_state"] = "awaiting_route_choice"
        await update.message.reply_text(
            "Is this food or exercise? Please type “food” or “exercise”."
        )
        return

    if category == "unclear":
        await update.message.reply_text(
            "I couldn't tell what you're logging. Please describe the meal or activity more clearly."
        )
        return

    # Otherwise continue with existing food workflow


    await begin_food_entry(update.message, context, description)


# ----- process a food photo and its optional caption -----
async def handle_photo(update, context):

    state = context.user_data.get("food_state")
    pending = get_pending(context)

    if context.user_data.get("food_processing"):
        await update.message.reply_text(
            "Please wait for the current analysis."
        )
        return

    if state != "awaiting_photo" or not pending:
        await update.message.reply_text(
            "Please describe your meal and choose to add a photo first."
        )
        return

    context.user_data["food_processing"] = True
    try:
        # Telegram provides multiple photo resolutions.
        # Use the largest available version.
        photo = update.message.photo[-1]

        telegram_file = await photo.get_file()

        image_data = await telegram_file.download_as_bytearray()

        pending["image_bytes"] = bytes(image_data)
        caption = (update.message.caption or "").strip()
        if caption:
            pending["photo_caption"] = caption
            pending["description"] = (
                f"{pending['base_description']}\nPhoto caption: {caption}"
            )

        context.user_data["food_state"] = "analyzing"

        await analyze_meal(
            update.message, context, pending
        )

    except Exception:
        logger.exception("Photo processing failed")

        context.user_data["food_state"] = "awaiting_photo"

        await update.message.reply_text(
            "Couldn't process the photo. "
            "Please try another image, reply “skip”, or use /cancel."
        )
    finally:
        context.user_data["food_processing"] = False


# ----- apply the selected food confirmation action -----
async def handle_food_buttons(update, context):

    query = update.callback_query
    await query.answer()

    pending = get_pending(context)

    if not pending:
        await query.message.reply_text(
            "This food entry is no longer active."
        )
        return

    action, meal_id = query.data.split(":", 1)

    if meal_id != pending["id"]:
        await query.message.reply_text(
            "This button belongs to an older meal."
        )
        return

    if context.user_data.get("food_processing"):
        return

    if context.user_data.get("food_state") != "confirming":
        return

    if action == "food_confirm":

        result = pending["estimate"]
        now = datetime.now(TIMEZONE)
        estimate_source = pending.get("estimate_source", "ai_estimated")
        memory_reuse_id = (
            pending.get("memory_reuse_id")
            if estimate_source == "memory_reused" else None
        )
        memory_payload = None
        if memory_reuse_id is None:
            evidence = "user_corrected" if estimate_source == "user_corrected" else "ai_confirmed"
            origin = "manual_correction" if evidence == "user_corrected" else "ai_estimated"
            memory_match = pending.get("memory_match") or {}
            memory_payload = prepare_memory(
                pending.get("base_description", pending["description"]),
                pending.get("memory_basis", result),
                evidence_category=evidence,
                source_origin=origin,
                clarification_history=pending.get("history", []),
                photo_included=pending.get("image_bytes") is not None,
                original_source_origin=(
                    memory_match.get("original_source_origin")
                    or memory_match.get("source_origin")
                ),
            )
            if memory_payload:
                memory_payload["calories"] = int(result["total_calories"])

        try:
            food_id = save_food(
                user_id=query.from_user.id,
                date=pending.get("logging_date") or now.date().isoformat(),
                time=now.strftime("%H:%M:%S"),
                description=pending["description"],
                items=result["items"],
                calories=result["total_calories"],
                product_info=(
                    {
                        **(result.get("product_identification") or {}),
                        "calorie_basis": result.get("calorie_basis"),
                        "nutrition_verified": bool(result.get("nutrition_verified", False)),
                    }
                    if result.get("product_identification") else (
                        pending.get("memory_basis", {}).get("product_identification")
                    )
                ),
                confirmation_key=pending["id"],
                memory=memory_payload,
                memory_reuse_id=memory_reuse_id,
            )

        except Exception:
            logger.exception("Database save failed")

            await query.message.reply_text(
                "Couldn't save the meal. "
                "Please try again."
            )
            return

        clear_pending(context)
        await query.edit_message_reply_markup(reply_markup=None)

        await query.message.reply_text(
            "Food recorded successfully!\n\n"
            f"Entry ID: {food_id}\n"
            f"Calories: {result['total_calories']} kcal\n"
            f"Date: {pending.get('logging_date') or now.date().isoformat()}"
        )

    elif action == "food_edit":

        context.user_data["food_state"] = "editing"
        await query.edit_message_reply_markup(reply_markup=None)

        await query.message.reply_text(
            "Enter the corrected total calories, "
            "or describe what needs changing.\n\n"
            "Examples:\n"
            "750\n"
            "I only ate half the rice."
        )

    elif action == "food_cancel":

        clear_pending(context)
        await query.edit_message_reply_markup(reply_markup=None)

        await query.message.reply_text(
            "Food entry cancelled."
        )


# ----- show a selected singapore-day calorie and bank summary -----
async def day_command(update, context):
    args = getattr(context, "args", []) or []
    if len(args) > 1:
        await update.message.reply_text(
            "Usage: /day [N], where N is a whole number from 0 to 3650."
        )
        return
    try:
        selected_date = day_summary_date(args[0] if args else 0)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    user_id = update.effective_user.id
    entries = get_food_by_date(user_id, selected_date)
    summary = calculate_daily_summary(user_id, selected_date)
    selected_is_today = selected_date == datetime.now(TIMEZONE).date().isoformat()
    label = " (provisional)" if selected_is_today else " (finalized/recalculated)"
    lines = [f"Calorie Summary — {selected_date}{label}\n"]

    if not entries:
        lines.append("No food recorded — incomplete/no-records day.")
    else:
        for entry in entries:
            lines.append(
                f"#{entry['id']} - {entry['description']}: {entry['calories']} kcal"
            )

    lines.append(f"\nBase target: {summary['base_target']} kcal")
    lines.append(f"Food consumed: {summary['food_consumed']} kcal")
    lines.append(f"Additional exercise: {summary['additional_exercise']} kcal")
    lines.append(f"Exercise calories added to allowance: {summary['exercise_bonus']} kcal")
    lines.append(f"Allowance before bank: {summary['allowance_before_bank']} kcal")
    lines.append(f"Available positive credits: {summary['bank_available']} kcal")
    lines.append(f"Positive credits used: {summary['bank_used']} kcal")
    if "debt_added" in summary:
        lines.append(f"New calorie debt: {summary['debt_added']} kcal")
        lines.append(f"Debt repaid today: {summary['debt_repaid']} kcal")
        lines.append(f"Outstanding calorie debt: {summary['debt_outstanding']} kcal")
    if "historical_debt_added" in summary:
        lines.append(
            f"Imported historical debt: {summary['historical_debt_added']} kcal"
        )
        lines.append(
            f"Historical debt outstanding: "
            f"{summary['historical_debt_outstanding']} kcal"
        )
        lines.append(
            f"Recovery deadline: {summary['historical_recovery_deadline']}"
        )
    lines.append(f"Remaining allowance: {summary['remaining_allowance']} kcal")
    status = (
        "Recovered Within 7 Days"
        if summary["status"] == "recovered_within_7_days"
        else summary["status"].replace("_", " ").title()
    )
    if not entries:
        status = "Incomplete (no food records)"
    lines.append(f"Status: {status}{label}")
    await update.message.reply_text("\n".join(lines))


# ----- show active calorie-bank deposits and expiry dates -----
async def bank_command(update, context):
    user_id = update.effective_user.id
    today_date = datetime.now(TIMEZONE).date()
    from unified_calorie_bank import account_summary

    account = account_summary(user_id, today_date)
    if account is None:
        recalculate_user(user_id, today_date)
        account = account_summary(user_id, today_date)
    deposits = account["deposits"] if account else get_bank_deposits(
        user_id, today_date.isoformat()
    )
    balance = sum(item["remaining_calories"] for item in deposits)
    expiring = sum(item["remaining_calories"] for item in deposits
                   if item["expiration_date"] == today_date.isoformat())
    debt_rows = account["debts"] if account else []
    debt_balance = sum(item["remaining_calories"] for item in debt_rows)
    lines = [
        f"Positive calorie credits: {balance} kcal available",
        f"Expiring today: {expiring} kcal",
        f"Outstanding calorie debt: {debt_balance} kcal",
    ]
    if deposits:
        lines.append("\nActive deposits:")
        for item in deposits:
            lines.append(
                f"{item['remaining_calories']}/{item['original_calories']} kcal — "
                f"earned {item['earning_date']}, expires {item['expiration_date']}"
            )
    else:
        lines.append("\nNo active deposits.")
    if debt_rows:
        lines.append("\nOutstanding debts:")
        for debt in debt_rows:
            origin = "Imported historical debt" if debt["origin"] == "imported" else "Debt"
            lines.append(
                f"{origin}: {debt['remaining_calories']}/"
                f"{debt['original_calories']} kcal — "
                f"from {debt['source_date']}, recovery deadline {debt['recovery_deadline']}"
            )
    else:
        lines.append("\nNo outstanding debt.")
    message = "\n".join(lines)
    if len(message) > 3900:
        message = message[:3850] + "\nDeposit list truncated."
    await update.message.reply_text(message)


# ----- send the selected monthly progress calendar -----
async def month_command(update, context):
    if len(context.args) > 1:
        await update.message.reply_text(
            "Usage: /month or /month YYYY-MM (for example, /month 2026-03)."
        )
        return
    try:
        year, month = parse_month(context.args[0] if context.args else None)
    except ValueError as error:
        await update.message.reply_text(str(error))
        return

    report = monthly_data(update.effective_user.id, year, month)
    image = render_calendar(report)
    await update.message.reply_photo(photo=image, caption=format_caption(report))


# ----- finalize each singapore day at midnight -----
async def midnight_finalization(application):
    while True:
        now = datetime.now(TIMEZONE)
        next_midnight = datetime.combine(
            now.date() + timedelta(days=1), datetime.min.time(), TIMEZONE
        )
        await asyncio.sleep(max(1, (next_midnight - now).total_seconds()))
        try:
            refresh_all_goals(datetime.now(TIMEZONE).date())
            recalculate_all(datetime.now(TIMEZONE).date())
        except Exception:
            logger.exception("Midnight calorie finalization failed")


# ----- schedule the daily finalization task -----
async def schedule_midnight_finalization(application):
    application.create_task(midnight_finalization(application))


# ----- show recent confirmed food entries -----
async def history(update, context):

    user_id = update.effective_user.id
    current_date = datetime.now(TIMEZONE).date()

    lines = ["Food History (Last 7 Days)\n"]
    found = False

    for days_ago in range(7):

        date = (
            current_date - timedelta(days=days_ago)
        ).isoformat()

        entries = get_food_by_date(user_id, date)

        if not entries:
            continue

        found = True
        lines.append(f"\nDate: {date}")

        for entry in entries:
            lines.append(
                f"#{entry['id']} - "
                f"{entry['description']}: "
                f"{entry['calories']} kcal"
            )

    if not found:
        await update.message.reply_text(
            "No food entries found in the last 7 days."
        )
        return

    message = "\n".join(lines)

    if len(message) > 4000:
        message = message[:3950] + "\nHistory truncated."

    await update.message.reply_text(message)


# ----- change the calories on a saved food entry -----
async def edit_food(update, context):

    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: /editfood <food_id> <calories>"
        )
        return

    try:
        food_id = int(context.args[0])
        calories = int(context.args[1])

        if food_id <= 0 or calories <= 0:
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "Enter a valid ID and positive calories."
        )
        return

    updated = update_food_calories(
        user_id=update.effective_user.id,
        food_id=food_id,
        calories=calories,
    )

    if not updated:
        await update.message.reply_text(
            f"Food entry #{food_id} not found."
        )
        return

    await update.message.reply_text(
        f"Food #{food_id} updated to {calories} kcal."
    )


# ----- remove a saved food entry -----
async def delete_food_command(update, context):

    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /deletefood <food_id>"
        )
        return

    try:
        food_id = int(context.args[0])

        if food_id <= 0:
            raise ValueError

    except ValueError:
        await update.message.reply_text(
            "Enter a valid food ID."
        )
        return

    deleted = delete_food(
        user_id=update.effective_user.id,
        food_id=food_id,
    )

    if not deleted:
        await update.message.reply_text(
            f"Food entry #{food_id} not found."
        )
        return

    await update.message.reply_text(
        f"Food entry #{food_id} deleted."
    )



# ----- apply the selected standalone exercise action -----
async def handle_exercise_buttons(update, context):
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get("pending_exercise")
    state = context.user_data.get("exercise_state")

    if context.user_data.get("food_processing"):
        await query.message.reply_text(
            "Please wait for the current analysis."
        )
        return

    if not pending or state != "confirming":
        await query.message.reply_text(
            "No active exercise entry."
        )
        return

    action, entry_id = query.data.split(":", 1)

    if entry_id != pending["id"]:
        await query.message.reply_text(
            "This button belongs to an older exercise entry."
        )
        return

    if action == "exercise_confirm":
        result = pending["estimate"]
        now = datetime.now(TIMEZONE)

        try:
            exercise_id = save_exercise(
                user_id=query.from_user.id,
                date=pending.get("logging_date") or now.date().isoformat(),
                time=now.strftime("%H:%M:%S"),
                description=pending["description"],
                activity=result["activity"],
                duration_minutes=result["duration_minutes"],
                met=result["met"],
                additional_calories=result["additional_calories"],
                weight_kg=result.get(
                    "weight_kg", pending.get("weight_kg", STARTING_WEIGHT_KG)
                ),
            )
        except Exception:
            logger.exception("Exercise save failed")
            await query.message.reply_text(
                "Couldn't save exercise. Please try again."
            )
            return

        context.user_data.pop("pending_exercise", None)
        context.user_data.pop("exercise_state", None)

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "Exercise recorded successfully!\n\n"
            f"Entry ID: {exercise_id}\n"
            f"Activity: {result['activity']}\n"
            f"Additional calories: "
            f"{result['additional_calories']} kcal\n\n"
            f"Date: {pending.get('logging_date') or now.date().isoformat()}\n\n"
            "Exercise calories were allocated using your current bank and debt balances."
        )

    elif action == "exercise_edit":
        context.user_data["exercise_state"] = "editing"

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "Enter corrected additional calories, "
            "or describe what needs changing.\n\n"
            "Examples:\n"
            "350\n"
            "I actually ran for 40 minutes."
        )

    elif action == "exercise_cancel":
        context.user_data.pop("pending_exercise", None)
        context.user_data.pop("exercise_state", None)

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "Exercise entry cancelled."
        )


# ----- apply the selected workout confirmation action -----
async def handle_workout_buttons(update, context):
    query = update.callback_query
    await query.answer()
    pending = context.user_data.get("pending_workout")
    state = context.user_data.get("workout_state")
    if not pending:
        await query.message.reply_text("This workout session is no longer active.")
        return
    action, session_id = query.data.split(":", 1)
    if session_id != pending.get("button_id"):
        await query.message.reply_text("This button belongs to an older workout session.")
        return

    if action == "workout_cancel":
        context.user_data.pop("pending_workout", None)
        context.user_data.pop("workout_state", None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Workout session cancelled.")
        return
    if action == "workout_edit" and state == "confirming":
        context.user_data["workout_state"] = "editing"
        pending["button_id"] = secrets.token_hex(6)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Enter corrected calories (for example 180) or a corrected total duration (for example 50 min).")
        return
    if action != "workout_confirm" or state != "confirming":
        await query.message.reply_text("This workout action is no longer available.")
        return

    now = datetime.now(TIMEZONE)
    try:
        saved_id = save_workout_session(
            session_key=pending["id"],
            user_id=query.from_user.id,
            date=pending.get("logging_date") or now.date().isoformat(),
            time=now.strftime("%H:%M:%S"),
            duration_minutes=pending["duration_minutes"],
            duration_source=pending["duration_source"],
            weight_kg=pending["weight_kg"],
            met=pending["met"],
            additional_calories=pending["estimate"],
            exercises=pending["exercises"],
        )
    except Exception:
        logger.exception("Workout session save failed")
        await query.message.reply_text("Couldn't save the workout. Your session is still here; please try Confirm again.")
        return
    context.user_data.pop("pending_workout", None)
    context.user_data.pop("workout_state", None)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"Workout recorded successfully!\nSession ID: {saved_id}\n"
        f"Date: {pending.get('logging_date') or now.date().isoformat()}\n"
        f"Estimated additional calories: {pending['estimate']} kcal"
    )


# ----- initialize storage and register telegram handlers -----
def main():

    if not TOKEN:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN missing from .env"
        )

    init_db()

    # Rebuild live state and finalize any missed Singapore calendar days.
    refresh_all_goals(datetime.now(TIMEZONE).date())
    recalculate_all(datetime.now(TIMEZONE).date())

    app = ApplicationBuilder().token(TOKEN).post_init(
        schedule_midnight_finalization
    ).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("prev", prev_command))
    app.add_handler(CommandHandler("day", day_command))
    app.add_handler(CommandHandler("bank", bank_command))
    app.add_handler(CommandHandler("month", month_command))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("editfood", edit_food))
    app.add_handler(
        CommandHandler("deletefood", delete_food_command)
    )
    app.add_handler(CommandHandler("weight", weight_command))
    app.add_handler(CommandHandler("weighthistory", weight_history_command))
    app.add_handler(CommandHandler("goal", goal_command))

    # Confirmation buttons
    app.add_handler(
        CallbackQueryHandler(
            handle_clear_buttons,
            pattern=r"^clear:(history|everything|confirm|cancel):[0-9a-f]{16}$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_food_buttons,
            pattern=r"^food_(confirm|edit|cancel):[0-9a-f]+$",
        )
    )

    # Photo messages
    app.add_handler(
        MessageHandler(
            filters.PHOTO,
            handle_photo,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_exercise_buttons,
            pattern=r"^exercise_(confirm|edit|cancel):[0-9a-f]+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            handle_workout_buttons,
            pattern=r"^workout_(confirm|edit|cancel):[0-9a-f]+$",
        )
    )

    # Text messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    logger.info("AI Calorie Tracker is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
