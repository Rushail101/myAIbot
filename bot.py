import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import os
from tempfile import NamedTemporaryFile
from typing import List, Optional

from fastapi import FastAPI
from google import genai
from google.genai import types
from gtts import gTTS
from mem0 import MemoryClient
from pydantic import BaseModel, Field
from supabase import Client, create_client
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import uvicorn

# ==============================================================================
# 1. ENVIRONMENT & CLIENT CONFIGURATION
# ==============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
MEM0_API_KEY = os.environ.get("MEM0_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_SERVICE_KEY", os.environ.get("SUPABASE_KEY", "")
)

# Optional Whitelist (comma-separated Telegram user IDs if restricted)
ALLOWED_USERS_RAW = os.environ.get("ALLOWED_TELEGRAM_USERS", "")
ALLOWED_USERS = (
    [int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()]
    if ALLOWED_USERS_RAW
    else []
)

# External Service Clients
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
memory = MemoryClient(api_key=MEM0_API_KEY) if MEM0_API_KEY else None
supabase: Client = (
    create_client(SUPABASE_URL, SUPABASE_KEY)
    if SUPABASE_URL and SUPABASE_KEY
    else None
)


# ==============================================================================
# 2. SCHEMAS & UTILITIES
# ==============================================================================
class CashbookEntry(BaseModel):
  amount: float = Field(description="Transaction amount in INR")
  type: str = Field(
      description="'debit' (expense/outflow) or 'credit' (received/inflow)"
  )
  category: str = Field(
      description=(
          "Operational category: Fuel/Diesel, Labor/Wages, Raw Materials,"
          " Cylinders, Crane Operations, Ration, Travel/Petty Cash, or Other"
      )
  )
  description: str = Field(
      description="Specific details, vendor name, or purpose of transaction"
  )
  date: str = Field(
      default_factory=lambda: datetime.now().strftime("%Y-%m-%d"),
      description="Date in YYYY-MM-DD format",
  )


class LedgerExtraction(BaseModel):
  transactions: List[CashbookEntry]
  financial_notes: Optional[str] = Field(
      None, description="Any balance adjustments, reset instructions, or notes"
  )


def is_authorized(user_id: int) -> bool:
  if not ALLOWED_USERS:
    return True
  return user_id in ALLOWED_USERS


def get_current_financial_summary() -> str:
  """Calculates the balance directly from the Supabase transactions ledger."""
  if not supabase:
    return "Database client not initialized."
  try:
    res = (
        supabase.table("nova_geo_cashbook")
        .select("*")
        .order("created_at", desc=False)
        .execute()
    )
    records = res.data or []

    total_credit = sum(
        r["amount"] for r in records if r.get("type", "").lower() == "credit"
    )
    total_debit = sum(
        r["amount"] for r in records if r.get("type", "").lower() == "debit"
    )
    balance = total_credit - total_debit

    # Expense categorization breakdown
    category_totals = {}
    for r in records:
      if r.get("type", "").lower() == "debit":
        cat = r.get("category", "Miscellaneous")
        category_totals[cat] = category_totals.get(cat, 0.0) + float(
            r.get("amount", 0)
        )

    cat_breakdown = "\n".join(
        [f"- **{cat}**: ₹{amt:,.2f}" for cat, amt in category_totals.items()]
    )

    summary = (
        f"### **Current Financial Status**\n"
        f"* **Total Inflow (Credits):** ₹{total_credit:,.2f}\n"
        f"* **Total Outflow (Debits):** ₹{total_debit:,.2f}\n"
        f"* **Calculated Ledger Balance:** ₹{balance:,.2f}\n\n"
        f"### **Major Outflow Categories**\n{cat_breakdown or 'No recorded debits.'}"
    )
    return summary
  except Exception as e:
    return f"Error retrieving financial records: {str(e)}"


# ==============================================================================
# 3. AI EXTRACTION & PROCESSING ENGINE
# ==============================================================================
SYSTEM_PROMPT = """You are the Nova Geo Resources LLP Financial Operations & Cashbook Assistant.
Your core role is to manage operational expenses, diesel logs, labor wages, contractor advances, and petty cash.
Always record debits (outflows) and credits (inflows) accurately.
If the user specifies reset instructions, past dates, or ledger corrections, respect them strictly.
When formatting replies:
- Be direct and professional.
- Format INR currency with commas (e.g. ₹55,914).
- Keep numerical statements concise and clear.
"""


async def process_user_input(
    user_id: str,
    text: Optional[str] = None,
    image_bytes: Optional[bytes] = None,
) -> str:
  # Retrieve persistent conversation context from Mem0
  user_memories = ""
  if memory:
    try:
      mem_results = memory.search(
          query=text or "Nova Geo transactions", user_id=user_id, limit=4
      )
      if mem_results and "results" in mem_results:
        user_memories = "\n".join(
            [f"- {m.get('memory', '')}" for m in mem_results["results"]]
        )
    except Exception:
      pass

  # Construct prompt context
  current_time = datetime.now().strftime("%A, %d %B %Y %I:%M %p")
  dynamic_context = (
      f"Current Timestamp: {current_time}\n"
      f"Context/Memories:\n{user_memories}\n"
      f"User Query:\n{text or 'Process receipt/slip from image'}"
  )

  # Model call with fallback support
  response_text = ""
  for model_name in ["gemini-2.5-flash", "gemini-1.5-flash"]:
    try:
      contents = [dynamic_context]
      if image_bytes:
        contents.append(
            types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        )

      response = ai_client.models.generate_content(
          model=model_name,
          contents=contents,
          config=types.GenerateContentConfig(
              system_instruction=SYSTEM_PROMPT,
              temperature=0.2,
          ),
      )
      response_text = response.text
      break
    except Exception:
      continue

  if not response_text:
    return "Error: Unable to process request through language model pipeline."

  # Save new context back to Mem0
  if memory and text:
    try:
      memory.add(
          f"User: {text}\nAssistant: {response_text}",
          user_id=user_id,
          metadata={"category": "cashbook"},
      )
    except Exception:
      pass

  # Attempt structured database insertion if transaction keywords detected
  keywords = [
      "paid",
      "spent",
      "received",
      "credit",
      "debit",
      "diesel",
      "labor",
      "wages",
      "advance",
      "cashbook",
      "₹",
      "rs",
  ]
  if text and any(kw in text.lower() for kw in keywords) and supabase:
    try:
      extraction = ai_client.models.generate_content(
          model="gemini-2.5-flash",
          contents=[
              f"Extract any explicit financial transaction lines from this interaction:\nText: {text}\nAssistant: {response_text}"
          ],
          config=types.GenerateContentConfig(
              response_mime_type="application/json",
              response_schema=LedgerExtraction,
          ),
      )
      extracted_data = json.loads(extraction.text)
      for entry in extracted_data.get("transactions", []):
        supabase.table("nova_geo_cashbook").insert({
            "telegram_user_id": user_id,
            "amount": entry["amount"],
            "type": entry["type"].lower(),
            "category": entry["category"],
            "description": entry["description"],
            "transaction_date": entry["date"],
        }).execute()
    except Exception:
      pass

  return response_text

# ==============================================================================
# 4. TELEGRAM EVENT HANDLERS
# ==============================================================================
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
  if not is_authorized(update.effective_user.id):
    await update.message.reply_text("Unauthorized access.")
    return
  await update.message.reply_text(
      "👋 **Nova Geo Cashbook Assistant Active**\n\n"
      "Send entries via text, voice notes, or photos of slips/receipts. "
      "Use /summary to inspect the live ledger calculations.",
      parse_mode="Markdown",
  )


async def summary_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
  if not is_authorized(update.effective_user.id):
    return
  summary = get_current_financial_summary()
  await update.message.reply_text(summary, parse_mode="Markdown")


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
  user_id = str(update.effective_user.id)
  if not is_authorized(update.effective_user.id):
    return

  await update.message.chat.send_action("typing")
  reply = await process_user_input(user_id=user_id, text=update.message.text)
  await update.message.reply_text(reply, parse_mode="Markdown")


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
  user_id = str(update.effective_user.id)
  if not is_authorized(update.effective_user.id):
    return

  await update.message.chat.send_action("record_voice")

  # Download voice audio
  voice_file = await update.message.voice.get_file()
  with NamedTemporaryFile(suffix=".ogg", delete=False) as temp_audio:
    await voice_file.download_to_drive(temp_audio.name)
    audio_path = temp_audio.name

  try:
    # Process audio via Gemini multimodal API
    with open(audio_path, "rb") as f:
      audio_data = f.read()

    transcription = ai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            "Transcribe this voice memo accurately. Only output the transcription text:",
            types.Part.from_bytes(data=audio_data, mime_type="audio/ogg"),
        ],
    )
    user_prompt = transcription.text.strip()
    reply = await process_user_input(user_id=user_id, text=user_prompt)

    # Respond in voice format (mirroring input)
    tts = gTTS(text=reply.replace("*", "").replace("#", ""), lang="en")
    with NamedTemporaryFile(suffix=".mp3", delete=False) as temp_out:
      tts.save(temp_out.name)
      await update.message.reply_voice(voice=open(temp_out.name, "rb"))
      os.remove(temp_out.name)
  finally:
    if os.path.exists(audio_path):
      os.remove(audio_path)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
  user_id = str(update.effective_user.id)
  if not is_authorized(update.effective_user.id):
    return

  await update.message.chat.send_action("typing")
  photo_file = await update.message.photo[-1].get_file()

  with NamedTemporaryFile(suffix=".jpg", delete=False) as temp_img:
    await photo_file.download_to_drive(temp_img.name)
    img_path = temp_img.name

  try:
    with open(img_path, "rb") as f:
      img_bytes = f.read()

    caption = update.message.caption or "Extract ledger details from receipt"
    reply = await process_user_input(
        user_id=user_id, text=caption, image_bytes=img_bytes
    )
    await update.message.reply_text(reply, parse_mode="Markdown")
  finally:
    if os.path.exists(img_path):
      os.remove(img_path)

# ==============================================================================
# 5. ASYNC LIFESPAN & APPLICATION RUNNER
# ==============================================================================
# Initialize Telegram Bot Application
tg_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
tg_app.add_handler(CommandHandler("start", start_handler))
tg_app.add_handler(CommandHandler("summary", summary_handler))
tg_app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler)
)
tg_app.add_handler(MessageHandler(filters.VOICE, voice_handler))
tg_app.add_handler(MessageHandler(filters.PHOTO, photo_handler))


@asynccontextmanager
async def lifespan(app: FastAPI):
  """FastAPI lifespan hook that starts Telegram polling alongside the web server."""
  print("==> Starting Telegram Bot polling loop...")
  await tg_app.initialize()
  await tg_app.start()
  # drop_pending_updates prevents processing stale messages accumulated during redeploys
  await tg_app.updater.start_polling(drop_pending_updates=True)
  print("==> Polling loop active.")

  yield

  print("==> Shutting down Telegram Bot...")
  await tg_app.updater.stop()
  await tg_app.stop()
  await tg_app.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def health_check():
  return {"status": "ok", "message": "Phase 2 Assistant Active"}


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 8080))
  uvicorn.run(app, host="0.0.0.0", port=port)