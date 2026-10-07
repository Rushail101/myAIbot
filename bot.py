import os
import io
import asyncio
from datetime import datetime
import pytz
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from google import genai
from mem0 import MemoryClient
from fastapi import FastAPI
import uvicorn
from gtts import gTTS

# ----------------- CONFIGURATION -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6KjbkKCJpNWnfUutNkhJbQ3O1GPfNnYiwfPYCh2WjGPbw")
MEM0_API_KEY = os.getenv("MEM0_API_KEY", "m0-Q0av4q2w25kjwz6ktxLmer6QEV2n9WJ8r6vvt7YG")
PORT = int(os.getenv("PORT", 8080))
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")
# -------------------------------------------------

ai_client = genai.Client(api_key=GEMINI_API_KEY)
mem_client = MemoryClient(api_key=MEM0_API_KEY)

server = FastAPI()

@server.get("/")
def health_check():
    return {"status": "ok", "message": "Bot cashbook active"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! I'm your Personal Assistant & Real-Time Cashbook.\n\n"
        "📸 **Cashbook:** Send any payment screenshot (UPI, GPay, Paytm, invoice, bill) and I'll extract and log the amount, payee, and date.\n"
        "🎙️ **Voice/Video:** Send audio notes to log cash spent or ask questions.\n"
        "📊 **Commands:**\n"
        "/ledger - View recent logged transactions\n"
        "/tasks - View pending tasks and general context"
    )

async def view_ledger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    try:
        search_res = mem_client.search(query="payment transaction spent debit credit received ₹", filters={"user_id": user_id})
        records = search_res.get("results", []) if isinstance(search_res, dict) else search_res

        if not records:
            await update.message.reply_text("No transactions logged yet.")
            return

        formatted = "\n\n".join([f"• {m.get('memory', '')}" for m in records if isinstance(m, dict) and "memory" in m])
        await update.message.reply_text(f"📖 **Recent Transactions & Entries:**\n\n{formatted}")
    except Exception as e:
        await update.message.reply_text(f"Could not fetch ledger: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    tz = pytz.timezone("Asia/Kolkata")
    now_str = datetime.now(tz).strftime("%A, %d %B %Y, %I:%M %p IST")

    is_audio = bool(update.message.voice or update.message.video_note)
    is_photo = bool(update.message.photo)
    user_text = ""

    # 1. Handle Transaction Receipt Photo
    if is_photo:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        try:
            photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
            img_bytes = await photo_file.download_as_bytearray()
            caption = update.message.caption or ""

            prompt = (
                f"You are an accountant managing a personal cashbook. Current date: {now_str}.\n"
                f"Analyze this payment receipt/transaction screenshot carefully.\n"
                f"User note/caption: '{caption}'.\n\n"
                "Extract:\n"
                "1. Transaction Type (Debit/Expense OR Credit/Received)\n"
                "2. Amount (in INR ₹)\n"
                "3. Payee / Beneficiary / Vendor name\n"
                "4. Date/Time and Transaction ID / UTR (if visible)\n"
                "5. Purpose/Category (e.g., Food, Fuel, Operations, Equipment, Raw Materials)\n\n"
                "Output a neat transaction confirmation summary in 3-4 bullet points. "
                "End with an explicit 'LOGGED_ENTRY:' line formatted like: "
                "'LOGGED_ENTRY: [Date] Paid ₹Amount to Payee for Category (Txn: ID)'"
            )

            response = ai_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=[
                    genai.types.Part.from_bytes(data=bytes(img_bytes), mime_type="image/jpeg"),
                    prompt
                ]
            )
            bot_reply = response.text

            # Persist extracted payment directly into Mem0
            try:
                mem_client.add(
                    messages=[
                        {"role": "user", "content": f"[Transaction Screenshot uploaded at {now_str}]. Note: {caption}"},
                        {"role": "assistant", "content": bot_reply}
                    ],
                    user_id=user_id
                )
            except Exception as e:
                print(f"Mem0 photo save error: {e}")

            await update.message.reply_text(bot_reply)
            return

        except Exception as e:
            print(f"Photo processing error: {e}")
            await update.message.reply_text("Could not process transaction screenshot. Please try again.")
            return

    # 2. Handle Voice / Video
    elif is_audio:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE)
        try:
            file_obj = update.message.voice or update.message.video_note
            media_file = await context.bot.get_file(file_obj.file_id)
            audio_bytes = await media_file.download_as_bytearray()

            mime = "audio/ogg" if update.message.voice else "video/mp4"
            transcription = ai_client.models.generate_content(
                model='gemini-3.5-flash-lite',
                contents=[
                    genai.types.Part.from_bytes(data=bytes(audio_bytes), mime_type=mime),
                    "Transcribe this audio verbatim. Output only the plain transcription, nothing else."
                ]
            )
            user_text = transcription.text.strip()
        except Exception as e:
            print(f"Transcription error: {e}")
            await update.message.reply_text("Could not process audio. Please send text or try again.")
            return

    # 3. Handle Text
    elif update.message.text:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        user_text = update.message.text.strip()

    if not user_text:
        return

    # 4. Search context / previous records
    memory_context = ""
    try:
        search_res = mem_client.search(query=user_text, filters={"user_id": user_id})
        relevant_memories = search_res.get("results", []) if isinstance(search_res, dict) else search_res
        if relevant_memories:
            mem_entries = [m.get("memory", "") for m in relevant_memories if isinstance(m, dict) and "memory" in m]
            if mem_entries:
                memory_context = "\n- " + "\n- ".join(mem_entries)
    except Exception as e:
        print(f"Mem0 search error: {e}")

    system_instruction = (
        f"You are a personal assistant and live cashbook manager. Current time: {now_str}.\n"
        f"Context and past entries:\n{memory_context}\n\n"
        "If the user is asking about their spending, summarize total spent and breakdown based on context. "
        "If they are noting cash spent manually (e.g. 'Paid 500 for diesel'), confirm the debit clearly."
    )

    candidate_models = ['gemini-3.5-flash-lite', 'gemini-3.8-flash', 'gemini-3.1-pro']
    bot_reply = None

    for model_name in candidate_models:
        try:
            response = ai_client.models.generate_content(
                model=model_name,
                contents=user_text,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_instruction
                )
            )
            bot_reply = response.text
            break
        except Exception as err:
            print(f"Model error {model_name}: {err}")
            continue

    if not bot_reply:
        bot_reply = "Service is temporarily busy. Please try again shortly."

    # Save conversation
    try:
        mem_client.add(
            messages=[
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": bot_reply}
            ],
            user_id=user_id
        )
    except Exception as e:
        print(f"Mem0 add error: {e}")

    # Reply matching audio format if input was voice/video
    if is_audio:
        try:
            tts = gTTS(text=bot_reply, lang="en")
            voice_buffer = io.BytesIO()
            tts.write_to_fp(voice_buffer)
            voice_buffer.seek(0)
            await context.bot.send_voice(chat_id=update.effective_chat.id, voice=voice_buffer, caption=bot_reply)
        except Exception as e:
            await update.message.reply_text(bot_reply)
    else:
        await update.message.reply_text(bot_reply)

async def run_bot():
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("ledger", view_ledger))
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.VIDEO_NOTE | filters.PHOTO, handle_message))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)

@server.on_event("startup")
async def on_startup():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    uvicorn.run(server, host="0.0.0.0", port=PORT)
