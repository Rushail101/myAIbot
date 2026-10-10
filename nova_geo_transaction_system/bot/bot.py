"""Private Telegram intake bot. OCR and parsing run without an AI API."""
import os, re, json, tempfile
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from supabase import create_client
from ocr import parse_payment_screenshot

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED = {x.strip() for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip()}
db = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
DRAFTS = {}

def authorised(update):
    return bool(update.effective_user and str(update.effective_user.id) in ALLOWED)
async def deny(update):
    if update.effective_message: await update.effective_message.reply_text("Private bot: this Telegram account is not authorised.")
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update): return await deny(update)
    await update.message.reply_text("Nova Geo Transaction Assistant (no AI API)\n\nSend a payment screenshot or text like: Paid ₹500 cash for diesel\n\nI will show a preview before saving. Use /cancel to discard.")
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update): return await deny(update)
    DRAFTS.pop(str(update.effective_user.id), None)
    await update.message.reply_text("Draft discarded. Nothing was saved.")
def parse_cash(text):
    m = re.search(r"(?:₹|INR\s*)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", text, re.I) or re.search(r"\b(?:paid|spent|expense)\s+([0-9][0-9,]*(?:\.[0-9]{1,2})?)\b", text, re.I)
    if not m: return None
    amount = round(float(m.group(1).replace(",", "")), 2)
    purpose = re.sub(r"^(?:i\s+)?(?:paid|spent|expense(?:\s+of)?|bought)\s*", "", text, flags=re.I)
    purpose = re.sub(r"(?:₹|INR\s*)\s*[0-9][0-9,]*(?:\.[0-9]{1,2})?", "", purpose, flags=re.I)
    purpose = re.sub(r"\b(?:cash|in cash|using cash|for)\b", "", purpose, flags=re.I)
    purpose = re.sub(r"\s+", " ", purpose).strip(" -,. ") or "Purpose not specified"
    return {"amount": amount, "payee": None, "purpose": purpose, "payment_method": "Cash", "payment_status": "user_reported", "upi_reference": None, "transaction_date": datetime.now().date().isoformat(), "source": "telegram_text", "raw_text": text}
def preview(d):
    return (f"Check before saving:\n\nAmount: ₹{d.get('amount') if d.get('amount') is not None else 'NOT FOUND'}\nVendor: {d.get('payee') or 'NOT FOUND'}\nPurpose: {d.get('purpose') or 'Not specified'}\nMethod: {d.get('payment_method','UPI')}\nStatus: {d.get('payment_status','unknown')}\nUPI reference: {d.get('upi_reference') or 'NOT FOUND'}\nDate: {d.get('transaction_date') or 'NOT FOUND'}\n\nConfirm only if correct. If any field is wrong, discard and resend with corrections.")
def buttons():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm & save", callback_data="confirm"), InlineKeyboardButton("🗑 Discard", callback_data="discard")]])
async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update): return await deny(update)
    path = None
    try:
        tgfile = await context.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f: path = f.name
        await tgfile.download_to_drive(path)
        d = parse_payment_screenshot(path)
        d["purpose"] = update.message.caption.strip() if update.message.caption else None
        DRAFTS[str(update.effective_user.id)] = d
        await update.message.reply_text(preview(d), reply_markup=buttons())
    except Exception as e:
        print("OCR failure:", repr(e))
        await update.message.reply_text("I couldn't safely read this image. Nothing was saved. Try a clearer screenshot or send the details as text.")
    finally:
        if path and os.path.exists(path): os.unlink(path)
async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorised(update): return await deny(update)
    d = parse_cash(update.message.text.strip())
    if not d:
        await update.message.reply_text("I couldn't find an amount. Try: Paid ₹500 cash for diesel")
        return
    DRAFTS[str(update.effective_user.id)] = d
    await update.message.reply_text(preview(d), reply_markup=buttons())
async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not authorised(update): await q.answer("Not authorised", show_alert=True); return
    await q.answer()
    key = str(update.effective_user.id); d = DRAFTS.get(key)
    if q.data == "discard":
        DRAFTS.pop(key, None); await q.edit_message_text("Discarded. Nothing was saved."); return
    if not d: await q.edit_message_text("Draft expired. Please send the transaction again."); return
    if not d.get("amount") or d["amount"] <= 0:
        await q.edit_message_text("Cannot save: amount was not read. Send the details as text."); return
    ref = d.get("upi_reference")
    try:
        if ref:
            existing = db.table("transactions").select("id").eq("upi_reference", ref).limit(1).execute()
            if existing.data:
                DRAFTS.pop(key, None); await q.edit_message_text("Possible duplicate: this UPI reference already exists. Nothing new was saved."); return
        row = {"amount": d["amount"], "payee": d.get("payee"), "purpose": d.get("purpose"), "payment_method": d.get("payment_method", "UPI"), "payment_status": d.get("payment_status", "unknown"), "upi_reference": ref, "transaction_date": d.get("transaction_date"), "source": d.get("source", "telegram"), "raw_text": d.get("raw_text"), "created_by_telegram_id": int(update.effective_user.id), "review_status": "confirmed"}
        saved = db.table("transactions").insert(row).execute()
        tid = saved.data[0]["id"]
        db.table("transaction_audit").insert({"transaction_id": tid, "action": "confirmed_from_telegram", "actor_telegram_id": int(update.effective_user.id), "details": {"amount": d["amount"], "reference": ref}}).execute()
        DRAFTS.pop(key, None)
        await q.edit_message_text(f"Saved to Nova Geo cashbook.\nAmount: ₹{d['amount']:.2f}\nVendor: {d.get('payee') or 'Not specified'}\nPurpose: {d.get('purpose') or 'Not specified'}\nMethod: {d.get('payment_method','UPI')}\nRecord ID: {tid}")
    except Exception as e:
        print("Database error:", repr(e))
        await q.edit_message_text("Database save failed. This was not confirmed as saved; please retry or contact the administrator.")
def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start)); app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.PHOTO, photo)); app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message)); app.add_handler(CallbackQueryHandler(callback))
    print("Nova Geo bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)
if __name__ == "__main__": main()
