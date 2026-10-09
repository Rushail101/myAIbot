import os
import io
import csv
import json
import asyncio
from datetime import datetime, time
from collections import defaultdict
from typing import Literal, Optional
from pydantic import BaseModel, Field

import pytz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)
from google import genai
from google.genai import types
from mem0 import MemoryClient
from supabase import create_client, Client
from fastapi import FastAPI
import uvicorn
from gtts import gTTS

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MEM0_API_KEY = os.getenv("MEM0_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
PORT = int(os.getenv("PORT", 8080))
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")
IST = pytz.timezone("Asia/Kolkata")
# -------------------------------------------------

ai_client = genai.Client(api_key=GEMINI_API_KEY)
mem_client = MemoryClient(api_key=MEM0_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

CHAT_HISTORY = defaultdict(list)
MAX_HISTORY_TURNS = 10

server = FastAPI()

@server.get("/")
def health_check():
    return {"status": "ok", "message": "Phase 2 Assistant Active"}

def get_ist_now():
    return datetime.now(IST)

def get_ist_now_str():
    return get_ist_now().strftime("%A, %d %B %Y, %I:%M %p IST")

# --- Structured Pydantic Schemas ---
class ParsedTransaction(BaseModel):
    is_transaction: bool = Field(description="True if message/image is an expense, payment, or money receipt")
    txn_type: Literal["DEBIT", "CREDIT"] = Field(default="DEBIT")
    amount: float = Field(default=0.0)
    payee: Optional[str] = Field(default="Unspecified")
    category: Optional[str] = Field(default="General")
    payment_mode: Literal["CASH", "UPI", "BANK", "OTHER"] = Field(default="CASH")
    reference_id: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)

class ParsedReminder(BaseModel):
    is_reminder: bool = Field(description="True if user is asking to be reminded about a task at a specific time or day")
    task: str = Field(description="The core task or event to remember")
    iso_datetime: Optional[str] = Field(description="Target time in ISO-8601 string (e.g. 2026-10-09T16:00:00+05:30)")

# --- Core Accounting Helpers ---
def fetch_daily_stats(user_id: str, date_obj=None):
    if not date_obj:
        date_obj = get_ist_now().date()
    start_of_day = datetime.combine(date_obj, time.min, tzinfo=IST).isoformat()
    end_of_day = datetime.combine(date_obj, time.max, tzinfo=IST).isoformat()

    res = supabase.table("cashbook").select("*") \
        .eq("user_id", user_id) \
        .gte("created_at", start_of_day) \
        .lte("created_at", end_of_day) \
        .execute()
    rows = res.data or []

    total_debit = sum(float(r["amount"]) for r in rows if r["txn_type"] == "DEBIT")
    total_credit = sum(float(r["amount"]) for r in rows if r["txn_type"] == "CREDIT")

    categories = defaultdict(float)
    for r in rows:
        if r["txn_type"] == "DEBIT":
            categories[r.get("category") or "General"] += float(r["amount"])

    return total_debit, total_credit, len(rows), dict(categories)

# --- Telegram Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 **Phase 2 Executive Assistant & Cashbook**\n\n"
        "• **Snap Receipts:** Send screenshots to parse & verify with interactive confirm buttons.\n"
        "• **Reminders:** Say *'Remind me tomorrow at 4 PM to inspect plant foundations'* for automatic push alerts.\n"
        "• **Auditing & Stats:** Run `/eod` for your end-of-day cash reconciliation.\n\n"
        "**Available Commands:**\n"
        "/eod - Today's complete financial summary\n"
        "/ledger - View recent 10 transactions\n"
        "/export - Download full ledger as `.csv`\n"
        "/undo - Delete latest transaction\n"
        "/tasks - View stored tasks\n"
        "/clear - Reset immediate chat buffer"
    )

async def eod_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    debit, credit, count, cat_breakdown = fetch_daily_stats(user_id)
    if count == 0:
        await update.message.reply_text("📋 **End-of-Day Summary:** No transactions recorded today.")
        return

    breakdown_lines = [f"  • {cat}: ₹{amt:,.2f}" for cat, amt in cat_breakdown.items()]
    breakdown_text = "\n".join(breakdown_lines) if breakdown_lines else "  • None"

    summary_text = (
        f"📊 **Daily Reconciliation ({get_ist_now().strftime('%d %B %Y')}):**\n\n"
        f"• **Total Paid (Outflow):** ₹{debit:,.2f}\n"
        f"• **Total Received (Inflow):** ₹{credit:,.2f}\n"
        f"• **Net Cash Movement:** ₹{(credit - debit):,.2f}\n"
        f"• **Transactions Logged:** {count}\n\n"
        f"**Category Breakdown:**\n{breakdown_text}"
    )
    await update.message.reply_text(summary_text)

async def auto_daily_eod_job(context: ContextTypes.DEFAULT_TYPE):
    """Automated 9:00 PM IST daily push summary."""
    if not ALLOWED_USER_ID:
        return
    debit, credit, count, cat_breakdown = fetch_daily_stats(ALLOWED_USER_ID)
    if count == 0:
        return

    breakdown_lines = [f"  • {cat}: ₹{amt:,.2f}" for cat, amt in cat_breakdown.items()]
    breakdown_text = "\n".join(breakdown_lines)

    msg = (
        f"🌙 **Automated 9:00 PM Daily Cashbook Summary**\n\n"
        f"• **Total Spent Today:** ₹{debit:,.2f}\n"
        f"• **Total Inflow:** ₹{credit:,.2f}\n"
        f"• **Net Outflow:** ₹{(debit - credit):,.2f}\n\n"
        f"**Categories:**\n{breakdown_text}\n\n"
        "Check `/ledger` or download `/export` anytime."
    )
    await context.bot.send_message(chat_id=int(ALLOWED_USER_ID), text=msg)

async def execute_reminder_push(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    text = job_data["text"]
    reminder_id = job_data["id"]

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 **REMINDER ALERT:**\n\n{text}"
    )
    # Mark done in Supabase
    try:
        supabase.table("reminders").update({"is_completed": True}).eq("id", reminder_id).execute()
    except Exception as e:
        print(f"Failed to update reminder state: {e}")

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    CHAT_HISTORY[user_id] = []
    await update.message.reply_text("Session history reset.")

async def undo_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    try:
        res = supabase.table("cashbook").select("*").eq("user_id", user_id).order("id", desc=True).limit(1).execute()
        if not res.data:
            await update.message.reply_text("No transactions found to undo.")
            return

        last_txn = res.data[0]
        supabase.table("cashbook").delete().eq("id", last_txn["id"]).execute()
        await update.message.reply_text(
            f"🗑️ **Reverted Transaction:** ₹{last_txn['amount']:,.2f} to {last_txn['payee']}"
        )
    except Exception as e:
        await update.message.reply_text(f"Could not delete: {e}")

async def export_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    try:
        res = supabase.table("cashbook").select("*").eq("user_id", user_id).order("created_at", desc=False).execute()
        rows = res.data or []
        if not rows:
            await update.message.reply_text("No transactions to export.")
            return

        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer)
        writer.writerow(["ID", "Timestamp (UTC)", "Type", "Amount (INR)", "Payee", "Category", "Mode", "Reference ID", "Notes"])
        for r in rows:
            writer.writerow([r.get("id"), r.get("created_at"), r.get("txn_type"), r.get("amount"), r.get("payee"), r.get("category"), r.get("payment_mode"), r.get("reference_id"), r.get("notes")])

        csv_bytes = io.BytesIO(csv_buffer.getvalue().encode("utf-8"))
        csv_bytes.seek(0)
        await context.bot.send_document(
            chat_id=update.effective_chat.id,
            document=csv_bytes,
            filename=f"cashbook_{get_ist_now().strftime('%Y%m%d_%H%M%S')}.csv",
            caption="📊 Complete transaction export."
        )
    except Exception as e:
        await update.message.reply_text(f"Export failed: {e}")

async def view_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    try:
        res = supabase.table("cashbook").select("*").eq("user_id", user_id).order("created_at", desc=True).limit(10).execute()
        rows = res.data or []
        if not rows:
            await update.message.reply_text("Ledger is empty.")
            return

        lines = ["📖 **Recent 10 Transactions:**"]
        for r in rows:
            s = "-" if r["txn_type"] == "DEBIT" else "+"
            lines.append(f"• `{s}₹{r['amount']:,.2f}` | {r['payee']} ({r['category']}) [{r['payment_mode']}]")
        await update.message.reply_text("\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"Error fetching ledger: {e}")

async def view_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    try:
        memories = mem_client.get_all(filters={"user_id": user_id})
        items = memories.get("results", []) if isinstance(memories, dict) else memories
        if not items:
            await update.message.reply_text("No persistent memories stored.")
            return
        formatted = "\n".join([f"• {m.get('memory', '')}" for m in items[:15] if isinstance(m, dict) and "memory" in m])
        await update.message.reply_text(f"🧠 **Stored Context & Tasks:**\n\n{formatted}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# --- Interactive Inline Button Callback ---
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, txn_id = query.data.split(":")
    if action == "del":
        try:
            supabase.table("cashbook").delete().eq("id", txn_id).execute()
            await query.edit_message_text("❌ **Transaction removed from Cashbook.**")
        except Exception as e:
            await query.edit_message_text(f"Error removing transaction: {e}")
    elif action == "confirm":
        await query.edit_message_text(query.message.text + "\n\n*(Status: Verified & Stored)*")

# --- Intent Handlers ---
async def process_reminder_check(user_text: str, user_id: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Uses Gemini to detect if user requested an alarm/reminder."""
    if not any(k in user_text.lower() for k in ["remind", "alarm", "schedule", "alert me"]):
        return False

    prompt = (
        f"Current time: {get_ist_now_str()}.\n"
        f"Determine if the user wants an automated reminder at a future time. "
        f"Input: '{user_text}'. If so, return target ISO-8601 timestamp with +05:30 offset."
    )

    try:
        response = ai_client.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ParsedReminder
            )
        )
        data = json.loads(response.text)
        if data.get("is_reminder") and data.get("iso_datetime"):
            target_dt = datetime.fromisoformat(data["iso_datetime"])
            now_dt = get_ist_now()

            delay_secs = (target_dt - now_dt).total_seconds()
            if delay_secs <= 0:
                return False

            # Persist reminder to Supabase
            ins = supabase.table("reminders").insert({
                "user_id": user_id,
                "chat_id": chat_id,
                "reminder_text": data["task"],
                "due_at": target_dt.isoformat()
            }).execute()

            r_id = ins.data[0]["id"] if ins.data else 0

            # Schedule JobQueue event
            context.job_queue.run_once(
                execute_reminder_push,
                when=delay_secs,
                data={"chat_id": chat_id, "text": data["task"], "id": r_id},
                name=f"rem_{r_id}"
            )

            readable = target_dt.strftime("%d %b %Y at %I:%M %p IST")
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⏰ **Reminder Set!**\nI'll push an alert for: *\"{data['task']}\"*\non {readable}."
            )
            return True
    except Exception as e:
        print(f"Reminder scheduling error: {e}")
    return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    now_str = get_ist_now_str()
    is_audio = bool(update.message.voice or update.message.video_note)
    is_photo = bool(update.message.photo)
    user_text = ""

    # 1. Photo Receipt Logging with Inline Confirmation Buttons
    if is_photo:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
            img_bytes = await photo_file.download_as_bytearray()
            caption = update.message.caption or ""

            prompt = (
                f"You are an expense tracker. Current time: {now_str}.\n"
                f"User note: '{caption}'. Extract the transaction details exactly."
            )

            resp = ai_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=[
                    types.Part.from_bytes(data=bytes(img_bytes), mime_type="image/jpeg"),
                    prompt
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ParsedTransaction
                )
            )
            data = json.loads(resp.text)
            if data.get("is_transaction") and float(data.get("amount", 0)) > 0:
                ins = supabase.table("cashbook").insert({
                    "user_id": user_id,
                    "txn_type": data["txn_type"],
                    "amount": data["amount"],
                    "payee": data.get("payee") or "Vendor",
                    "category": data.get("category") or "General",
                    "payment_mode": data.get("payment_mode") or "UPI",
                    "reference_id": data.get("reference_id"),
                    "notes": data.get("notes") or caption
                }).execute()

                txn_id = ins.data[0]["id"]
                sign = "Paid" if data["txn_type"] == "DEBIT" else "Received"

                card = (
                    f"🧾 **Logged to Cashbook**\n"
                    f"• {sign}: ₹{data['amount']:,.2f}\n"
                    f"• Payee/Purpose: {data.get('payee')}\n"
                    f"• Category: {data.get('category')} [{data.get('payment_mode')}]\n"
                )

                keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Verified", callback_data=f"confirm:{txn_id}"),
                        InlineKeyboardButton("❌ Remove", callback_data=f"del:{txn_id}")
                    ]
                ])
                await update.message.reply_text(card, reply_markup=keyboard)
                return
            else:
                await update.message.reply_text("Could not recognize a clear financial transaction in this photo.")
                return
        except Exception as e:
            print(f"Receipt OCR failed: {e}")
            await update.message.reply_text("Failed to process transaction receipt.")
            return

    # 2. Audio Transcription
    elif is_audio:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VOICE)
        try:
            file_obj = update.message.voice or update.message.video_note
            media_file = await context.bot.get_file(file_obj.file_id)
            audio_bytes = await media_file.download_as_bytearray()
            mime = "audio/ogg" if update.message.voice else "video/mp4"

            trans = ai_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=[
                    types.Part.from_bytes(data=bytes(audio_bytes), mime_type=mime),
                    "Transcribe this audio verbatim in original language/English. Return only plain text."
                ]
            )
            user_text = trans.text.strip()
        except Exception as e:
            print(f"Transcription error: {e}")
            await update.message.reply_text("Could not transcribe voice note.")
            return

    elif update.message.text:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        user_text = update.message.text.strip()

    if not user_text:
        return

    # 3. Check for Proactive Reminder Intent
    scheduled = await process_reminder_check(user_text, user_id, chat_id, context)
    if scheduled:
        return

    # 4. Check for Text/Audio Financial Expense Intent
    if any(k in user_text.lower() for k in ["paid", "spent", "received", "credited", "debited", "bought", "rs", "inr", "₹"]):
        try:
            resp = ai_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=f"Extract transaction from: '{user_text}'. Current date: {now_str}",
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ParsedTransaction
                )
            )
            data = json.loads(resp.text)
            if data.get("is_transaction") and float(data.get("amount", 0)) > 0:
                ins = supabase.table("cashbook").insert({
                    "user_id": user_id,
                    "txn_type": data["txn_type"],
                    "amount": data["amount"],
                    "payee": data.get("payee") or "Unspecified",
                    "category": data.get("category") or "General",
                    "payment_mode": data.get("payment_mode") or "CASH",
                    "notes": data.get("notes") or user_text
                }).execute()

                txn_id = ins.data[0]["id"]
                sign = "Paid" if data["txn_type"] == "DEBIT" else "Received"
                bot_reply = (
                    f"✅ **Recorded to Cashbook**\n"
                    f"• {sign}: ₹{data['amount']:,.2f}\n"
                    f"• Payee/Purpose: {data.get('payee')}\n"
                    f"• Category: {data.get('category')} [{data.get('payment_mode')}]"
                )

                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("❌ Remove", callback_data=f"del:{txn_id}")]
                ])
                await update.message.reply_text(bot_reply, reply_markup=keyboard)
                return
        except Exception as e:
            print(f"Text expense logging error: {e}")

    # 5. General Conversational / Context Query
    memory_context = ""
    try:
        search_res = mem_client.search(query=user_text, filters={"user_id": user_id})
        memories = search_res.get("results", []) if isinstance(search_res, dict) else search_res
        if memories:
            extracted = [m.get("memory", "") for m in memories if isinstance(m, dict) and "memory" in m]
            if extracted:
                memory_context = "\n- " + "\n- ".join(extracted[:8])
    except Exception as e:
        print(f"Mem0 search error: {e}")

    history_lines = [f"{('User' if t['role']=='user' else 'Assistant')}: {t['text']}" for t in CHAT_HISTORY[user_id][-MAX_HISTORY_TURNS:]]
    history_context = "\n".join(history_lines) if history_lines else "None"

    system_instruction = (
        f"You are a personal executive AI assistant. Current IST time: {now_str}.\n"
        f"Remembered facts & tasks:\n{memory_context}\n\n"
        f"Sliding conversation window:\n{history_context}\n\n"
        "Guidelines:\n"
        "- Respond directly without repetitive conversational preamble.\n"
        "- For financial balance totals, refer the user to /eod or /ledger."
    )

    candidate_models = ['gemini-3.5-flash-lite', 'gemini-3.8-flash', 'gemini-3.1-pro']
    bot_reply = None

    for model_name in candidate_models:
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=user_text,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction
                )
            )
            bot_reply = response.text
            break
        except Exception:
            continue

    if not bot_reply:
        bot_reply = "I'm having temporary connection delays. Please try again shortly."

    CHAT_HISTORY[user_id].append({"role": "user", "text": user_text})
    CHAT_HISTORY[user_id].append({"role": "model", "text": bot_reply})

    # Save preferences/tasks to Mem0
    if any(w in user_text.lower() for w in ["note", "prefer", "task", "remember", "meeting", "contact"]) or len(user_text.split()) > 8:
        try:
            mem_client.add(
                messages=[{"role": "user", "content": user_text}, {"role": "assistant", "content": bot_reply}],
                user_id=user_id
            )
        except Exception as e:
            print(f"Mem0 save error: {e}")

    if is_audio:
        try:
            tts = gTTS(text=bot_reply, lang="en")
            voice_buffer = io.BytesIO()
            tts.write_to_fp(voice_buffer)
            voice_buffer.seek(0)
            await context.bot.send_voice(chat_id=chat_id, voice=voice_buffer, caption=bot_reply)
        except Exception:
            await update.message.reply_text(bot_reply)
    else:
        await update.message.reply_text(bot_reply)

# --- Application Startup & Scheduler ---
async def restore_pending_reminders(app):
    """Restores uncompleted alarms from Supabase on bot restart."""
    try:
        now_iso = get_ist_now().isoformat()
        res = supabase.table("reminders").select("*").eq("is_completed", False).gt("due_at", now_iso).execute()
        for r in (res.data or []):
            target_dt = datetime.fromisoformat(r["due_at"])
            delay = (target_dt - get_ist_now()).total_seconds()
            if delay > 0:
                app.job_queue.run_once(
                    execute_reminder_push,
                    when=delay,
                    data={"chat_id": r["chat_id"], "text": r["reminder_text"], "id": r["id"]},
                    name=f"rem_{r['id']}"
                )
    except Exception as e:
        print(f"Error restoring pending reminders: {e}")

async def run_bot():
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("eod", eod_command))
    bot_app.add_handler(CommandHandler("ledger", view_ledger))
    bot_app.add_handler(CommandHandler("export", export_ledger))
    bot_app.add_handler(CommandHandler("undo", undo_transaction))
    bot_app.add_handler(CommandHandler("tasks", view_tasks))
    bot_app.add_handler(CommandHandler("clear", clear_history))
    bot_app.add_handler(CallbackQueryHandler(button_callback))
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.VIDEO_NOTE | filters.PHOTO, handle_message))

    # Schedule automated 9:00 PM IST daily push summary
    eod_time = time(hour=21, minute=0, tzinfo=IST)
    bot_app.job_queue.run_daily(auto_daily_eod_job, time=eod_time)

    await bot_app.initialize()
    await bot_app.start()
    await restore_pending_reminders(bot_app)
    await bot_app.updater.start_polling(drop_pending_updates=True)

@server.on_event("startup")
async def on_startup():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    uvicorn.run(server, host="0.0.0.0", port=PORT)