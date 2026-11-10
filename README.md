# Personal Finance Tracker (HUMO + Gemini + Telegram)

One FastAPI service that runs, in a single process:

- **Telethon** on your personal account: listens to the HUMO Card bot, recovers missed messages, and refreshes card balances.
- **Telegram bot** (aiogram): menus, debts, voice assistant, and notifications. Owner-only, with an access code.
- **Background pipeline**: parses notifications, classifies them through merchant memory or Gemini, then saves them.
- **Web dashboard**: analytics and transaction editing, served by the same app.
- **One PostgreSQL database**.

## Setup (Docker)

`.env` holds only the secrets:

```env
TELEGRAM_BOT_TOKEN=     # your bot, from @BotFather
TELETHON_API_ID=        # from my.telegram.org
TELETHON_API_HASH=
GEMINI_API_KEY=
BOT_ACCESS_CODE=        # code for the bot AND the dashboard
```

Everything else is set in code (`app/config.py`): timezone `Asia/Tashkent`, currency `UZS`, owner id,
cards, HUMO bot username, database (the compose `db` service).

```bash
docker compose up -d --build
docker compose run --rm app python -m app.telegram.login   # one time: log in your Telegram account
docker compose restart app
docker compose logs -f app
```

- Bot: send `/start` and enter the access code. Anyone who knows the code gets access. The owner is
  1220127328, and every unlocked user gets transaction notifications.
- Dashboard: http://localhost:8000. Sign in with the same access code.
- Never run more than one app instance. The Telegram session must not be shared.

## Deploy on Railway

The project is one service (the app) plus a Railway PostgreSQL service. `railway.json` builds the Dockerfile
and runs a single replica with a `/healthz` health check.

1. **Export the Telegram login** on the machine where you are already logged in. No connection is made:
   ```bash
   docker compose exec app python -m app.telegram.export_session /data/telethon_session.txt
   docker compose exec app cat /data/telethon_session.txt   # copy it, then:
   docker compose exec app rm /data/telethon_session.txt
   ```
   The string gives **full access to your Telegram account**. Never commit it or share it.
2. **In Railway:** New Project → Deploy from GitHub repo. Then add a database (+ New → Database → PostgreSQL).
3. **Copy your existing data (optional, recommended).** Do this before the app's first successful start:
   `scripts/copy_db_to_railway.sh '<Postgres DATABASE_PUBLIC_URL>'`
4. **App service → Variables:**
   ```
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   TELEGRAM_BOT_TOKEN=...
   TELETHON_API_ID=...
   TELETHON_API_HASH=...
   TELETHON_SESSION=...      # the string from step 1
   GEMINI_API_KEY=...
   BOT_ACCESS_CODE=...
   ```
5. **App service → Settings → Networking → Generate Domain.** The bot's Menu button then opens
   `https://<domain>/app` automatically (from `RAILWAY_PUBLIC_DOMAIN`). You don't need a tunnel.
6. **Stop the local copy:** `docker compose down`. The same Telegram session and bot token must never run
   in two places at once.

On redeploys, the new container waits on a PostgreSQL lock until the old one stops. Only then does it
connect to Telegram, so two Telegram clients never run at the same time.

## Financial integrity

- Amounts, card, time and reported balance always come from the rule-based parser. Gemini only supplies the type, category and description. If Gemini's amount, card or direction disagrees with the parser, the transaction is marked for review.
- Money is stored as `Decimal` / `NUMERIC(18,2)` and sent to the dashboard as strings.
- **Idempotency:**
  - one original message per `(chat_id, message_id)`
  - one transaction per message
  - one snapshot per card and message
  - idempotency keys on bot-created records
  - leases on message processing
  - `FOR UPDATE` locks on debt repayments
- The 💰 balance in a notification is stored as a balance snapshot. A card's balance only moves forward in time, so messages processed out of order can't overwrite a newer balance.
- If Gemini fails, the transaction is still saved as unclassified. It is retried with backoff and ends as "needs review".
- Owner corrections are audited and never overwritten by AI. They can also be saved as merchant rules.
- The app never sends payments or transfers. The only thing it ever sends to the HUMO bot is a press of the existing balance button.

## Tests

```bash
createdb expense_test   # or set TEST_DATABASE_URL
.venv/bin/pytest
```

Tests use a real PostgreSQL database and a mocked Gemini, so they use no API quota.
