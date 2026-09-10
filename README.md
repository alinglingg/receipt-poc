# Receipt POC

Telegram receipt-processing proof of concept for the Full Stack Automation and AI Developer assessment.

## What it does

1. Receives Telegram images through a verified webhook.
2. Compresses and normalizes images before Vision processing.
3. Extracts strict receipt JSON using OpenAI Vision.
4. Detects duplicates with a database-enforced exact rule.
5. Remembers vendor categories and pauses for unknown vendors.
6. Stores receipt images in a private Supabase Storage bucket.
7. Sends a Markdown confirmation with a short-lived signed link.

## Required environment variables

Copy `.env.example` to `.env` and configure:

```env
DATABASE_URL=postgresql+psycopg://...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_WEBHOOK_SECRET=...
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...
SUPABASE_RECEIPTS_BUCKET=receipts
SIGNED_URL_TTL_SECONDS=900
```

Use the Supabase Session Pooler URI if your network does not support IPv6.

## Local checks

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m work.check_supabase
.\.venv\Scripts\python.exe -m work.check_openai
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Deploying

The included Dockerfile runs the web service on `0.0.0.0:$PORT`. Configure the same environment variables on the host. Do not upload or commit `.env`.

After deployment, configure Telegram with:

```text
https://YOUR_PUBLIC_HOST/webhooks/telegram
```

using the exact value of `TELEGRAM_WEBHOOK_SECRET` as Telegram's webhook secret token.
