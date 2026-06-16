# SavedBot

Telegram bot for saving deleted and edited Telegram messages.

Stack:
- Telegram bot: `aiogram + Telethon`
- Storage: `SQLite` with WAL and app-side migrations
- Runtime data: sessions, media and logs under `BASE_DIR`

## Local Run

Requirements:
- Python 3.11+ (Python 3.12 recommended)
- Telegram bot token
- Telegram API credentials: `TG_API_ID`, `TG_API_HASH`

Setup:

```bash
cp .env.example .env
```

Fill required values in `.env`:

```env
TG_API_ID=123456
TG_API_HASH=your_telegram_api_hash
SOO_BOT_TOKEN=your_bot_token
ADMIN_IDS=123456789
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Start the bot from the parent directory of `savedbot`:

```bash
python -m savedbot.handlers
```

## Docker

```bash
docker compose up -d --build
```

Logs:

```bash
docker compose logs -f savedbot
```

## Runtime Data

When `BASE_DIR=/data`, the bot stores:

- `/data/sessions`
- `/data/media`
- `/data/logs`

The Docker Compose setup mounts `./data` to `/data`.

## Bot Commands

- `/start` - main menu and connection flow
- `/set` - bot settings
- `/profile` - profile
- `/stats` - archive statistics
- `/logout` - close session

## Deployment Notes

For long polling, run the container or process as a background worker. The bot does not require an HTTP port.
