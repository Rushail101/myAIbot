# Nova Geo Transaction Assistant — AI-free starter

This is a starter scaffold for a private Telegram transaction bot + cashbook dashboard without Gemini or any paid AI API.

## Files
- `bot/bot.py` — Telegram bot for screenshot intake and cash expense text.
- `bot/ocr.py` — local Tesseract OCR and conservative text extraction.
- `bot/requirements.txt` — Python dependencies.
- `supabase/schema.sql` — transaction, vendor-rule and audit tables.
- `dashboard/index.html` — dark dashboard UI shell.
- `.env.example` — environment configuration template.

## Setup
1. Create a dedicated Supabase project for Nova Geo and run `supabase/schema.sql` in SQL Editor.
2. Create a Telegram bot using Telegram's BotFather.
3. Install Python 3.11+, Tesseract OCR, and packages from `bot/requirements.txt` on a machine/VPS.
4. Copy `.env.example` to `.env`, fill in credentials, and set your own Telegram numeric user ID in `TELEGRAM_ALLOWED_USER_IDS`.
5. Run `python bot.py` from the `bot` folder.
6. Host the dashboard only after adding Supabase Auth and appropriate Row Level Security policies. Never put the Supabase service-role key in browser code.

## Safety notes
- Bot requires a confirmation tap before saving.
- UPI references are unique in the database to prevent duplicate records.
- OCR can misread digits; always inspect the confirmation card.
- A screenshot is not independent proof of bank settlement. Reconcile against bank statements.
- The dashboard is a UI shell, not yet connected to Supabase.
