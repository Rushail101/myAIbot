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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MEM0_API_KEY = os.getenv("MEM0_API_KEY")
PORT = int(os.getenv("PORT", 8080))

# Optional: Set your numeric Telegram ID in Render to restrict access to yourself
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")  # e.g., "123456789"
# -------------------------------------------------

ai_client = genai.Client(api_key=GEMINI_API_KEY)
mem_client = MemoryClient(api_key=MEM0_API_KEY)

server = FastAPI()

@server.get("/")
def health_check():
    return {"status": "ok", "message": "Bot is live"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! I'm your persistent personal AI assistant.\n\n"
        "• Send text -> I reply in text.\n"
        "• Send voice note or video note -> I reply in voice.\n"
        "• All tasks and context are stored across chats.\n\n"
        "Commands:\n"
        "/tasks - View pending memories and tasks\n"
        "/forget - Clear saved memories"
    )

async def view_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    try:
        search_res = mem_client.get_all(filters={"user_id": user_id})
        memories = search_res.get("results", []) if isinstance(search_res, dict) else search_res
        
        if not memories:
            await update.message.reply_text("No stored tasks or memories found.")
            return

        formatted = "\n".join([f"• {m.get('memory', '')}" for m in memories if isinstance(m, dict) and "memory" in m])
        await update.message.reply_text(f"Here is what I have remembered:\n\n{formatted}")
    except Exception as e:
        await update.message.reply_text(f"Could not retrieve tasks: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if ALLOWED_USER_ID and user_id != ALLOWED_USER_ID:
        return

    is_audio_input = bool(update.message.voice or update.message.video_note)
    user_text = ""

    # 1. Handle Voice & Video Notes
    if is_audio_input:
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

    elif update.message.text:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        user_text = update.message.text.strip()

    if not user_text:
        return

    # 2. Retrieve persistent memories
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

    # 3. Time awareness
    tz = pytz.timezone("Asia/Kolkata")
    now_str = datetime.now(tz).strftime("%A, %d %B %Y, %I:%M %p IST")

    system_instruction = (
        "You are an executive personal AI assistant on Telegram.\n"
        f"Current time: {now_str}\n"
        f"Relevant remembered context about the user:\n{memory_context}\n\n"
        "Provide direct, concise, and helpful responses without robotic preamble."
    )

    # 4. Generate response with fallback
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
            print(f"Model {model_name} error: {err}")
            continue

    if not bot_reply:
        bot_reply = "I'm having trouble connecting to the language model right now. Please try again shortly."

    # 5. Persist interaction
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

    # 6. Mirror input medium in reply
    if is_audio_input:
        try:
            tts = gTTS(text=bot_reply, lang="en")
            voice_buffer = io.BytesIO()
            tts.write_to_fp(voice_buffer)
            voice_buffer.seek(0)

            await context.bot.send_voice(
                chat_id=update.effective_chat.id,
                voice=voice_buffer,
                caption=bot_reply
            )
        except Exception as e:
            print(f"TTS error: {e}")
            await update.message.reply_text(bot_reply)
    else:
        await update.message.reply_text(bot_reply)

async def run_bot():
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("tasks", view_tasks))
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.VIDEO_NOTE, handle_message))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)

@server.on_event("startup")
async def on_startup():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    uvicorn.run(server, host="0.0.0.0", port=PORT)
