# SavedBot (Telegram Bot + Mini App)

Production-ready MVP stack:
- Telegram bot: `aiogram + Telethon`
- Mini App / API: `FastAPI`
- Storage: `SQLite (WAL + migrations in app init)`
- Payments: Telegram Stars (`XTR`)
- Referrals, trial, subscriptions, alerts, daily analytics

## 1) Local Run

### Requirements
- Python 3.11+ (recommended 3.12)
- Telegram bot token and Telegram API credentials (`TG_API_ID`, `TG_API_HASH`)

### Setup
1. Copy env template:
```bash
cp .env.example .env
```
2. Fill required values in `.env`:
- `TG_API_ID`
- `TG_API_HASH`
- `SOO_BOT_TOKEN`
- `ADMIN_IDS`
- `AI_WEBAPP_URL` (for production; locally can stay default)

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Start bot (from parent directory of `savedbot`):
```bash
python -m savedbot.handlers
```

FastAPI Mini App will be served by the same process on `AI_APP_PORT` (default `8000`).

Health check:
```bash
curl http://127.0.0.1:8000/health
```

## 2) Docker (Local / VPS)

```bash
docker compose up -d --build
```

Logs:
```bash
docker compose logs -f savedbot
```

Health:
```bash
docker inspect --format='{{json .State.Health}}' savedbot
```

## 3) GitHub + Production HTTPS (без ngrok)

### Push to GitHub
```bash
git init
git add .
git commit -m "savedbot production setup"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### Deploy option (recommended): Render (Docker)
1. Create new **Web Service** from your GitHub repo.
2. Environment: `Docker`.
3. Add env variables from `.env.example` (at least required ones).
4. Deploy.
5. Get final HTTPS URL, for example:
`https://savedbot-prod.onrender.com`

Then set:
```env
AI_WEBAPP_URL=https://savedbot-prod.onrender.com/miniapp
```
and redeploy.

## 4) Telegram / BotFather Setup

1. Ensure bot has username (mandatory for referral links).
2. In BotFather set Mini App URL to production HTTPS:
`https://<your-domain>/miniapp`
3. If username cannot be fetched at runtime, set fallback:
```env
BOT_USERNAME=your_bot_username_without_at
```

## 5) Ops Notes

- Health endpoint: `GET /health` (`/ai/health` alias kept).
- Restart policy: Docker `restart: unless-stopped`.
- Critical alert routing: set `ALERT_CHAT_ID`.
- Runtime stores data under `BASE_DIR` (`/data` in docker-compose):
  - `/data/sessions`
  - `/data/media`
  - `/data/logs`
