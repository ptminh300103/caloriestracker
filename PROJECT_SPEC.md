Telegram AI Calorie Tracker — Project Specification
Purpose: Handoff document for a separate Codex/ChatGPT account.
Status: Local Python Telegram bot under active development. This document records agreed design decisions, reported working features, proposed architecture, and known limitations. Inspect the actual repository before assuming a feature is implemented.
1. Product objective
Build a personal Telegram bot to track food intake, exercise expenditure, occasional body-weight updates, daily calorie allowance, a short-lived exercise calorie bank, and the number of days on target each month. It should accept natural-language messages and food photos, estimate calories using AI where appropriate, request only useful clarifications, and let the user confirm or correct entries. Develop locally in VS Code first, then consider inexpensive/free cloud hosting.
2. Current technology and local setup
- Python 3.11 in a project-local .venv on macOS, running in VS Code.
- python-telegram-bot with ApplicationBuilder().token(...).build() and app.run_polling().
- OpenAI Python SDK, AsyncOpenAI, model gpt-4.1-mini in the shared examples; python-dotenv loads .env.
- SQLite database calorie_tracker.db next to database.py.
- Pillow is used to render the in-memory `/month` calendar image.
- Time zone Asia/Singapore for all daily boundaries.
- Expected files include main.py, ai_estimator.py, database.py, .env, .gitignore, and potentially intent_classifier.py and exercise_estimator.py. The latter two were proposed in Step 7; their actual implementation has not been confirmed.
- The prior bot is a separate Telegram bot; the new tracker needs its own Telegram token. The OpenAI API key can be reused, although billing/usage is shared.
- Keep .env, .venv/, __pycache__/, *.pyc, *.db, *.db-shm, and *.db-wal out of Git. A Telegram bot token was exposed in a previous terminal-log screenshot; ensure it has been revoked and replaced. Never print secrets or log Telegram API URLs containing the token. Suppress routine httpx INFO logs with logging.getLogger('httpx').setLevel(logging.WARNING).
3. User profile and calorie target
Initial inputs, for this personal project only:
Parameter	Value
Sex used for BMR equation	Male
Age at initial setup	23 years
Height	175.5 cm
Starting weight	82 kg
Baseline activity	Lightly active (multiplier 1.375)
Weight-loss goal	Estimated 400 kcal/day deficit


Mifflin–St Jeor BMR = 10*weight_kg + 6.25*height_cm - 5*age + 5 = 1,806.875 kcal/day.
TDEE = BMR × 1.375 = 2,484.45 kcal/day (approx.).
Starting food target = TDEE − 400 = 2,084.45, rounded half-up to 2,084 kcal/day. Daily targets use decimal ROUND_HALF_UP to the nearest whole kcal consistently.

These are estimates, not measured expenditure. Recalculate future targets after weight updates, preserving historical targets. Avoid counting exercise twice when using a lightly active TDEE multiplier. Any exercise-credit formula must use an estimate of additional expenditure beyond the baseline. If uncertainty is high, be conservative.
4. Unified calorie-bank and debt rules (active from each user's migration activation date)
For each confirmed exercise session with estimated additional expenditure E:
1. When no debt is outstanding, split E 50/50 between same-day food allowance and an individual positive bank credit.
2. Positive credits become available the following Singapore calendar day and expire at the end of the third following calendar day. Multiple credits have independent dates; spend earliest-expiring first.
3. When debt is outstanding, allocate 75% of E toward the oldest debt and 25% to same-day allowance. If that exercise clears all debt, split its remaining calories 50/50. Integer rounding must allocate every calorie exactly once.
4. Debt is created when confirmed intake exceeds base allowance, same-day exercise allocation, and eligible positive credits. Debt has no expiry; it is marked recovered only when fully repaid within seven calendar days of its source date.
5. Unused ordinary daily allowance does not roll over. Never spend a future or expired positive credit.
6. Recompute post-activation balances deterministically after food/exercise edits without duplicate ledger entries. Preserve finalized daily results before activation and preserve unexpired legacy positive credits during migration.
7. Monthly status distinguishes on-target, over-target, recovered-within-seven-days, incomplete, provisional, and future dates. A recovered day counts as successful for the monthly success rate.
8. Finalize days at Singapore midnight and catch up after downtime/restarts; calculations must be idempotent.
9. An optional one-time pre-activation debt import is owner-run from the local CLI (`python historical_debt_import.py preview <user_id>` followed by `apply <user_id> <batch_id>` only after explicit review). Preview is read-only, the confirmed import creates a verified backup and audit snapshots, and startup never runs it automatically. Source edits on imported dates must update the imported debt and replay post-activation allocations.
5. Food logging — implemented/reported working
Earlier steps were tested by the user:
- Natural-language food descriptions, AI calorie estimate and itemized breakdown.
- Clarification questions, with a reply classifier (clarification, correction, replacement) proposed and integrated in previous code. User reported repeated or incorrect clarification behavior; see known bugs.
- Telegram inline Confirm / Edit / Cancel buttons.
- Edit by typing a new total kcal number or describing a natural-language correction.
- Confirmed food logs persisted in SQLite; /day worked across restarts.
- /history (last seven days), /editfood <id> <calories>, /deletefood <id> were reported working.
Revised text + photo flow
A newer implementation was supplied (not all paths separately verified):
1. User sends a text description first.
2. Bot asks Add Photo / No Photo via inline buttons. Do not call OpenAI yet.
3. If Add Photo, wait for one Telegram photo, with a Skip Photo button. Otherwise proceed with text only.
4. AI estimates from text plus optional image; ask meaningful clarification only when needed.
5. Confirm / Edit / Cancel; save one final confirmed entry to SQLite.
6. Photos are held temporarily as bytes in the pending conversation; database currently stores food description, JSON item breakdown, calories, date/time and user ID, not the image.
7. Pending conversation state is in context.user_data and is lost on restart. The proposed version uses unique meal IDs in callback data to reject stale buttons.
Food clarification principles
- Ask a question when food identity or major calorie-relevant components are unknown and plausible alternatives materially change calories.
- Do not ask repeatedly for exact grams, typical portion weights, minor oil variations or ingredients that the user cannot reasonably know.
- A known standardized branded item/serving should generally be estimated without ingredient questions; if available, prefer verified published nutritional information over model memory. No live online nutrition lookup has been implemented yet. Do not claim values are verified unless a source was actually consulted.
- Clarification must preserve the entire original meal and add details; only an explicit correction/replacement should remove original components.
- A replacement (e.g. “never mind, rice instead”) starts a fresh meal and resets clarification count.
- Avoid repeated clarification loops; recognize “I don't know,” “just estimate,” and requests for online lookup. When appropriate, fall back to a reasonable, explicitly uncertain estimate, not an infinite loop.
- Allow items=[] while a clarification is requested; do not save empty-item, zero-calorie estimates as complete meals.
- AI should not invent major components merely to produce a number.
- Keep food description/meal state explicit; a future structured current_meal representation is preferred to relying on the last user message or a provisional estimate.
Photo-analysis design under consideration — deferred
User proposed independent text-only and photo-only calorie analyses followed by an internal AI reconciliation, with only the final estimate shown to Telegram. We discussed three strategies: text only, combined text+photo in one request, and separate text/photo estimates plus cross-check. No evidence yet proves cross-check improves calorie accuracy; extra cost is likely small for personal use but complexity and latency increase. Proposed future evaluation: compare approaches on 20–30 meals with known/reference calories, compute MAE and monitor tokens/latency. A conditional cross-check only for major uncertainty is another option. Do not implement the cross-check until explicitly requested. Current image analysis used detail='low' in supplied code; higher detail may help some visual recognition but does not reveal hidden oils/ingredients or exact weights.
6. Exercise tracking — redesign agreed; implementation pending
The first exercise approach used automatic AI classification of ordinary messages into food, exercise, or unclear, and a single-exercise MET estimator. It caused unnecessary duration questions for individual lifting sets. Replace the lifting flow with session-based tracking, while preserving food/photo behavior.
Automatic routing
- When there is no active conversation, use AI to classify a new text message as food, exercise, or unclear.
- When a food or workout session is active, state takes priority; do not reclassify clarification, editing, or follow-up responses as new entries.
- For clearly ambiguous mixed messages, ask a routing question rather than guessing.
- Keep simple yes/no/finish/cancel/duration responses in deterministic Python logic to avoid unnecessary API calls. Inline Telegram buttons are preferred for robust routing.
Strength-training session workflow — agreed
1. User sends an exercise, e.g. Bench press 50kg 5 reps x4.
2. AI extracts exercise name, load, sets, reps (and other available details). Do not require duration for every individual exercise.
3. Bot acknowledges exercise and asks “Anything else?” with Add more / Finish / Cancel buttons.
4. Additional messages add exercises or correct prior entries. Continue until user chooses Finish or says a recognized equivalent such as no, done, that's all.
5. Ask once at the end: total session duration including rest periods. Accept natural formats (e.g. 45 min, 1 hour 10 minutes). Offer Not sure.
6. If duration unknown, infer a conservative, low-confidence estimate using number of sets, plausible repetition tempo and rest, with transparent assumptions. Do not pretend this is measured duration.
7. Estimate one session-level additional calorie total from current body weight, total session duration and an appropriate session-level MET/intensity assumption. Do not sum gross estimates per lift or double-count rest.
8. Show workout summary, estimated additional calories, and Confirm / Edit / Cancel. On confirmation save a single workout session plus its constituent exercise records to SQLite.
9. Apply the active unified bank/debt allocation once per confirmed session.
Cardio
A complete cardio entry such as Ran 5 km in 30 minutes can be estimated directly (with confirmation), without forcing the multi-exercise gym workflow. Distance, duration, pace/speed, intensity, and body weight matter; use appropriate exercise-specific methods. Ask only for genuinely missing inputs. Avoid double-counting baseline activity.
Calorie formula and limitations
An initial approximate net exercise formula discussed was:

additional_kcal ≈ (MET - 1) * weight_kg * duration_hours

This subtracts resting expenditure but is not necessarily a perfect adjustment for the lightly active TDEE baseline. MET assumptions should reflect the whole session, including rest. Weight lifted/sets/reps alone do not uniquely determine calories. Store assumptions and make corrections possible. Suggested example intensities (2.5, 3.5, 5.0 MET) were illustrative, not validated classifications.
Exercise conversation states (proposed)
collecting_exercises, awaiting_more, awaiting_duration, estimating_session, confirming_session, editing_session.

Use a single temporary session object containing exercise list, total duration, source of duration (reported/inferred), intensity assumptions, estimated kcal and unique session ID. Confirmed records survive restart; in-progress state need not initially.
7. SQLite design and known existing API
Existing database.py was supplied with:
- init_db() creating food_logs table and (user_id, date) index.
- save_food(user_id, date, time, description, items, calories).
- get_food_by_date(user_id, date).
- get_daily_total(user_id, date).
- update_food_calories(user_id, food_id, calories).
- delete_food(user_id, food_id).
Food table: id, user_id, date, time, description, JSON items, integer calories. Query/update/delete by user_id to isolate users.

An exercise_logs table and save_exercise / get_exercise_by_date helpers were proposed for individual exercise records, but actual status must be checked. The session redesign should use a normalized workout_sessions table and workout_exercises child table, with migrations preserving existing data. Later add weight_logs, user profile/goal settings, calorie-bank deposits/withdrawals, and daily completion status. Use SQLite transactions and indexes as appropriate.

Do not assume update_food_calories updates per-item JSON: the previous version only changed the total. The bot's /day uses a 2,084 kcal default target when no effective-dated user target applies.
8. Bugs and unresolved technical debt
1. Food clarification loops: repeated questions for branded items, even after user says the details aren't known. Need termination/fallback and future nutrition lookup.
2. Lost original meal: Pasta → cream sauce, chicken, broccoli sometimes estimated chicken/broccoli but omitted pasta. Need structured cumulative meal state and validation of essential components.
3. Inconsistent food estimates: semantically equivalent pasta descriptions yielded 650 vs 900 kcal. Reference nutrition data, explicit portion assumptions and testing needed; prompt changes alone are insufficient.
4. Empty AI item list: clarification responses sometimes contain items=[], which originally caused ValueError('AI returned no food items'). Validation should allow this only while clarification is genuinely pending.
5. Exercise over-clarification: individual lifting descriptions (bench 50kg 5x4) triggered unnecessary duration questions; session-based design addresses this.
6. Image usefulness unproven: identical text and text+photo estimates do not prove photo helped. Benchmark before assuming improved accuracy.
7. Stale callbacks / retry: unique callback IDs are desirable; ensure a failed database save retains a usable retry path and doesn't double-save.
8. Concurrency: avoid overlapping AI requests and conflicting pending states. Ensure no double processing on rapid messages/button taps.
9. Persistence: confirmed entries in SQLite persist; pending conversations are in memory. On restart, no pending session recovery yet.
10. No online nutrition lookup; do not imply GPT-4.1 mini automatically browses websites.
11. No real midnight finalization or bank yet; hard-coded /day target must not be presented as including exercise.
9. Implementation roadmap (updated)
Already reported working (verify in code): Telegram polling, /start, text food estimation, Confirm/Edit/Cancel, food SQLite persistence, /day, /history, /editfood, /deletefood. A newer photo-choice workflow and initial automatic exercise classifier were supplied; their full regression status is not confirmed.

Next agreed work:
1. Review actual repository and preserve working food/photo/database functionality.
2. Replace the per-exercise strength tracker with session collection → finish → ask duration once → calculate → confirm → save.
3. Add robust deterministic yes/no/finish/cancel parsing, inline buttons, and duration parser; avoid API calls for these.
4. Test cardio separately and ensure automatic food/exercise routing respects active session state.
5. Implement weight updates and future-only target recalculation.
6. Use the unified calorie bank after each user's activation date: when no debt is outstanding, split exercise calories 50/50 between same-day allowance and positive bank credit. Credits become available the following Singapore calendar day, expire after three following calendar days, and are spent earliest-expiring first. If a debt is outstanding, apply 75% of exercise calories toward the oldest debt and 25% to the day's allowance; when that exercise clears all debt, split its remainder 50/50. Debts do not expire. Fully repaid debts within seven calendar days mark their original day recovered. Preserve finalized legacy daily results before activation.
7. Integrate positive credits and debt into `/day`, `/bank`, `/month`, and monthly status counts. `/month` uses finalized/recalculated daily results; recovered days are reported separately and count as successful days. If historical debt is imported, retain the original legacy result as an audit snapshot and show the imported/recovered status in reports without overwriting it.
8. Add midnight Singapore-time finalization, catch-up/idempotency and incomplete-day handling.
9. Revisit food clarification reliability, published nutrition lookup, and optional photo cross-check benchmark later.
10. Hybrid per-user food memory is implemented with deterministic exact matches, bounded similar examples, confirmed-only evidence, and edit/delete reconciliation.
11. After local testing, consider cloud deployment. Local polling requires an awake Mac. Cloud alternatives discussed: an always-running free-tier VM or a webhook-based on-demand service plus scheduled daily task and persistent external storage. Free-tier terms may change.
10. Development instructions for Codex
- First inspect all project files and report what is actually implemented. Do not overwrite working code from an older chat snippet.
- Do not make code changes until explicitly instructed. Propose a small, testable plan first.
- Prefer incremental, isolated modules and migrations over large unreviewed rewrites.
- Preserve Telegram food text/photo choice, clarification, confirmation, saved food, and history commands.
- Separate AI interpretation from deterministic Python state management, arithmetic and database operations.
- Ask only high-value clarifications. Avoid repeated or forced questions and fabricated precision.
- Use deterministic Python handling for buttons, yes/no/finish/cancel and duration parsing; use AI for free-form descriptions and meaningful corrections.
- Add unit tests for session transitions, duration parsing, bank expiry/withdrawal, idempotent daily processing, and regressions in food workflows.
- Avoid logging API keys, photo data or sensitive food/weight details. Use .env and .gitignore.
- Keep token usage reasonable; do not optimize cost at the expense of correctness.
- Explain any migration or breaking behavior before applying it.
11. Suggested initial prompt for Codex
Read PROJECT_SPEC.md and inspect the actual Python project files. Summarize implemented features, discrepancies with this spec, and known bugs. Do not edit anything yet. Propose the smallest safe implementation plan for session-based strength training: automatic exercise routing, multiple exercise entries, deterministic finish/yes/no controls, asking total duration once with a conservative fallback, session-level calorie calculation, Confirm/Edit/Cancel, and SQLite persistence. Preserve existing food-photo and database features. Wait for my approval before changing code.

This is a handoff summary of decisions and reported progress, not a guarantee that the current repository matches every supplied code example. The actual codebase is the source of truth for implementation status.
