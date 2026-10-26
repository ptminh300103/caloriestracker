"""Monthly progress calendar built from finalized calorie-bank results."""

import calendar
import re
from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

import database


TIMEZONE = ZoneInfo("Asia/Singapore")
MONDAY_FIRST = calendar.Calendar(firstweekday=calendar.MONDAY)

COLORS = {
    "on_target": (193, 232, 202),
    "over_target": (250, 197, 197),
    "recovered_within_7_days": (177, 222, 211),
    "incomplete": (224, 228, 233),
    "provisional": (255, 229, 157),
    "future": (246, 247, 249),
}
INK = (38, 48, 61)
MUTED = (103, 114, 128)
WHITE = (255, 255, 255)


# ----- validate and resolve a requested calendar month -----
def parse_month(value=None, today=None):
    """Return (year, month), strictly validating an optional YYYY-MM value."""
    today = today or datetime.now(TIMEZONE).date()
    if value is None or value == "":
        return today.year, today.month
    if not re.fullmatch(r"\d{4}-\d{2}", str(value)):
        raise ValueError("Use /month or /month YYYY-MM (for example, /month 2026-03).")
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError as exc:
        raise ValueError("That month is invalid. Enter a month from 01 to 12 as YYYY-MM.") from exc
    return parsed.year, parsed.month


# ----- assign a progress status to one calendar day -----
def classify_day(day, result, today):
    """Map one calendar date to finalized status or provisional/future state."""
    if day > today:
        return "future"
    if day == today:
        return "provisional"
    if not result or not result.get("finalized") or result.get("status") == "incomplete":
        return "incomplete"
    if result.get("status") == "on_target":
        return "on_target"
    if result.get("status") == "recovered_within_7_days":
        return "recovered_within_7_days"
    if result.get("status") == "over_target":
        return "over_target"
    return "incomplete"


# ----- assemble daily statuses and statistics for a month -----
def monthly_data(user_id, year, month, as_of=None):
    """Get calendar statuses and finalized-only statistics for one user/month."""
    today = as_of or datetime.now(TIMEZONE).date()
    if isinstance(today, str):
        today = date.fromisoformat(today)
    elif isinstance(today, datetime):
        today = today.astimezone(TIMEZONE).date() if today.tzinfo else today.date()
    activation = database.get_calorie_account_activation(user_id)
    if activation and today >= date.fromisoformat(activation):
        from calorie_bank import recalculate_user

        recalculate_user(user_id, today)
    result_rows = database.get_daily_results_for_month(
        user_id, f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
    )
    results = {date.fromisoformat(row["date"]): row for row in result_rows}
    if activation:
        from calorie_bank import calculate_daily_summary

        for day_number in range(1, calendar.monthrange(year, month)[1] + 1):
            day = date(year, month, day_number)
            if day < date.fromisoformat(activation) and day < today and day not in results:
                summary = calculate_daily_summary(user_id, day)
                frozen = database.save_legacy_daily_result_if_missing(
                    user_id,
                    summary,
                    datetime.now(TIMEZONE).isoformat(timespec="seconds"),
                )
                results[day] = frozen or summary

    days = {}
    on_target = over_target = recovered = incomplete = 0
    for day_number in range(1, calendar.monthrange(year, month)[1] + 1):
        day = date(year, month, day_number)
        status = classify_day(day, results.get(day), today)
        days[day] = status
        if day >= today:
            continue
        if status == "on_target":
            on_target += 1
        elif status == "over_target":
            over_target += 1
        elif status == "recovered_within_7_days":
            recovered += 1
        else:
            incomplete += 1
    completed = on_target + recovered + over_target
    success_rate = ((on_target + recovered) / completed * 100) if completed else None
    return {
        "year": year,
        "month": month,
        "days": days,
        "on_target": on_target,
        "over_target": over_target,
        "recovered": recovered,
        "incomplete": incomplete,
        "success_rate": success_rate,
    }


# ----- load a readable font with a system fallback -----
def _font(size, bold=False):
    candidates = (
        ("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf"),
        ("/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


# ----- draw the monthly progress calendar as an image -----
def render_calendar(report):
    """Render a phone-friendly PNG and return it as an in-memory BytesIO."""
    year, month = report["year"], report["month"]
    weeks = MONDAY_FIRST.monthdatescalendar(year, month)
    width, margin, gap = 1080, 56, 12
    cell_width = (width - 2 * margin - 6 * gap) // 7
    cell_height = 132
    grid_top = 365
    grid_bottom = grid_top + len(weeks) * (cell_height + gap)
    height = grid_bottom + 190

    image = Image.new("RGB", (width, height), (250, 251, 253))
    draw = ImageDraw.Draw(image)
    title_font, stat_font = _font(52, True), _font(27, True)
    day_font, weekday_font = _font(22, True), _font(30, True)
    legend_font, small_font = _font(22), _font(20)

    month_title = f"{calendar.month_name[month]} {year}"
    draw.text((margin, 45), month_title, font=title_font, fill=INK)
    draw.text((margin, 125), "CALORIE PROGRESS", font=_font(20, True), fill=MUTED)

    success = "—" if report["success_rate"] is None else f"{report['success_rate']:.1f}%"
    stats = (
        (f"{report['on_target']}", "On target", COLORS["on_target"]),
        (f"{report['over_target']}", "Over target", COLORS["over_target"]),
        (f"{report.get('recovered', 0)}", "Recovered ≤7d", COLORS["recovered_within_7_days"]),
        (f"{report['incomplete']}", "Incomplete", COLORS["incomplete"]),
        (success, "Success rate", COLORS["provisional"]),
    )
    stat_gap = 14
    stat_width = (width - 2 * margin - (len(stats) - 1) * stat_gap) // len(stats)
    for index, (value, label, color) in enumerate(stats):
        x = margin + index * (stat_width + stat_gap)
        draw.rounded_rectangle((x, 190, x + stat_width, 315), radius=20, fill=WHITE)
        draw.rounded_rectangle((x, 190, x + 9, 315), radius=5, fill=color)
        draw.text((x + 24, 207), value, font=stat_font, fill=INK)
        draw.text((x + 24, 258), label, font=small_font, fill=MUTED)

    weekday_names = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
    for column, name in enumerate(weekday_names):
        x = margin + column * (cell_width + gap)
        draw.text((x + 12, 330), name, font=weekday_font, fill=MUTED)

    for row_index, week in enumerate(weeks):
        y = grid_top + row_index * (cell_height + gap)
        for column, day in enumerate(week):
            x = margin + column * (cell_width + gap)
            in_month = day.month == month
            status = report["days"].get(day, "future") if in_month else "future"
            fill = COLORS[status] if in_month else (242, 244, 247)
            text_color = INK if in_month else (177, 183, 191)
            draw.rounded_rectangle((x, y, x + cell_width, y + cell_height), radius=17, fill=fill)
            draw.text((x + 16, y + 14), str(day.day), font=day_font, fill=text_color)
            if in_month:
                label = {
                    "on_target": "ON TARGET",
                    "over_target": "OVER TARGET",
                    "recovered_within_7_days": "RECOVERED",
                    "incomplete": "INCOMPLETE",
                    "provisional": "TODAY",
                    "future": "",
                }[status]
                if label:
                    draw.text((x + 14, y + 76), label, font=_font(16, True), fill=text_color)

    legend = (
        ("on_target", "On target", margin, grid_bottom + 28),
        ("over_target", "Over target", margin + 350, grid_bottom + 28),
        ("recovered_within_7_days", "Recovered within 7 days", margin + 700, grid_bottom + 28),
        ("incomplete", "Incomplete", margin, grid_bottom + 76),
        ("provisional", "Today (provisional)", margin + 350, grid_bottom + 76),
        ("future", "Future", margin + 700, grid_bottom + 76),
    )
    for status, label, x, y in legend:
        draw.rounded_rectangle((x, y + 3, x + 24, y + 27), radius=6, fill=COLORS[status])
        draw.text((x + 34, y), label, font=legend_font, fill=INK)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = "monthly-calorie-progress.png"
    return output


# ----- summarize monthly progress for the telegram caption -----
def format_caption(report):
    success = "No finalized days yet" if report["success_rate"] is None else f"Success rate: {report['success_rate']:.1f}%"
    return (
        f"{calendar.month_name[report['month']]} {report['year']} calorie progress\n"
        f"On target: {report['on_target']} · Recovered within 7 days: {report.get('recovered', 0)} · "
        f"Over target: {report['over_target']} · "
        f"Incomplete: {report['incomplete']}\n{success}"
    )
