# AI Telegram Calorie Tracker

An AI-powered Telegram bot for tracking food intake, exercise, and body weight, featuring personalized calorie estimation, adaptive daily targets, and a dynamic calorie banking system.

## Features

- **AI Food Tracking:** Estimate calories from text descriptions and food photos, with intelligent clarification and user confirmation.
- **Personalized Food Memory:** Reuse previously logged meals and adapt estimates based on eating habits.
- **Exercise Tracking:** Log strength training and cardio through natural language, with session-based calorie calculations.
- **Weight & Goal Tracking:** Automatically adjust daily calorie targets using a 7-day rolling weight average and personal weight-loss goals.
- **Calorie Banking:** Allocate exercise calories between daily allowance, expiring credits, and outstanding calorie debt.
- **Progress Monitoring:** View daily summaries and color-coded monthly progress calendars.
- **Historical Logging:** Record missed meals and workouts for previous days.

## Tech Stack

- **Language:** Python
- **AI:** OpenAI API
- **Bot Interface:** python-telegram-bot
- **Database:** SQLite
- **Visualization:** Python-generated calendar images

## Getting Started

**1. Clone the repository**

```bash
git clone https://github.com/ptminh300103/caloriestracker.git
cd caloriestracker
```

**2. Create and activate a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Configure environment variables**

Create a `.env` file:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
OPENAI_API_KEY=your_openai_api_key
```

**5. Run the bot**

```bash
python main.py
```

## Telegram Commands

| Command | Description |
|---|---|
| `/start` | Start the bot |
| `/help` | View usage instructions |
| `/day N` | View daily summary (N days ago; defaults to today) |
| `/month` | View monthly progress calendar |
| `/bank` | View calorie credits and outstanding debt |
| `/weight` | View or update body weight |
| `/weighthistory` | View weight history |
| `/goal` | View or set a weight-loss goal |
| `/prev N` | Log the next entry N days in the past |
| `/history` | View recent food logs |
| `/editfood` | Edit saved food calories |
| `/deletefood` | Delete a food entry |
| `/clear` | Reset tracking history or personal data |
| `/cancel` | Cancel an active entry |

Food and exercise can be logged directly through natural-language messages without commands.

## How Calorie Banking Works

- **Positive credits:** Unused banked exercise calories expire after three eligible days.
- **Calorie debt:** Excess intake not covered by available allowance becomes outstanding debt.
- **Debt repayment:** While debt exists, 75% of eligible exercise calories go toward repayment and 25% increase the current day's allowance.
- **Recovery window:** An over-target day can be marked as recovered if its debt is fully repaid within seven days.

## Notes

- Calorie estimates are approximate and should not be treated as precise nutritional measurements.
- User data is stored locally in SQLite.
- API credentials and local databases are excluded from version control using `.gitignore`.
- The bot currently runs locally and requires the Python process to remain active.

## License

No license has been specified.
