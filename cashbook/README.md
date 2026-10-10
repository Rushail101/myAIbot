# Nova Geo Cashbook

Telegram bot + web dashboard for logging UPI and cash transactions. Single user, no AI API keys (OCR is Tesseract).

**How you use it**
- Send a UPI screenshot → bot reads amount / payee / date / UPI ref → tap a category or reply with text.
- Many screenshots in a row is fine: each gets its own message. Reply to a specific one to label it, or send text and it labels the latest pending one. `/pending` re-sends everything still unlabelled.
- `/cash 500 diesel` (paid) · `/cashin 2000 partner top-up` (received)
- `/today` · `/summary` (month) · `/undo` (delete last entry)
- Buttons under each screenshot: ✏️ fix amount, 🔄 flip in/out, 🗑 discard.
- Duplicate screenshots are caught by UPI reference.
- Dashboard: filter, search, edit, add, delete, export CSV.

## Setup (≈15 min)

1. **Supabase** – new project → SQL Editor → paste `schema.sql` → Run. Then Project Settings → API: copy the *Project URL* and the **service_role** key (keep it secret; only the server uses it).
2. **Telegram** – talk to `@BotFather` → `/newbot` → copy the token. Start the bot and send any message; if `TELEGRAM_OWNER_ID` isn't set it replies with your numeric id. Put that in the env var (the bot ignores everyone else).
3. **Env vars** – copy `.env.example`, fill in. `DASHBOARD_KEY` = any long random string.
4. **Run**
   - Local: `sudo apt install tesseract-ocr && pip install -r requirements.txt && set -a && . ./.env && set +a && uvicorn app.main:app --port 8000`
   - Server: the `Dockerfile` installs Tesseract and starts everything. Works on Render (Docker web service), Railway, Fly, or any VPS.
5. Open the dashboard at `https://YOUR-HOST/?key=YOUR_DASHBOARD_KEY` once per device (the key is remembered, then removed from the URL).

## Notes
- The bot uses long polling, so the process must stay awake. Render's free tier sleeps after idle — use a paid instance, a VPS, or ping `/health` every few minutes.
- OCR is best-effort. The amount is always shown back to you; fix with ✏️ if it's wrong.
- Reading is tuned for GPay, PhonePe and Paytm screenshots. If one app misreads often, send me a sample (with names hidden) and I'll tune the patterns.
- Change the quick-tap categories with the `CATEGORIES` env var.
