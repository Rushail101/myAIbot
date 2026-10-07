import os
import io
import asyncio
from telegram import Update
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
# -------------------------------------------------

ai_client = genai.Client(api_key=GEMINI_API_KEY)
mem_client = MemoryClient(api_key=MEM0_API_KEY)

server = FastAPI()

@server.get("/")
def health_check():
    return {"status": "ok", "message": "Bot is running"}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hey! Send me a text message and I'll reply in text. Send me a voice note and I'll reply in voice."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = ""
    is_voice_input = bool(update.message.voice)

    # 1. Handle Voice Note input
    if is_voice_input:
        try:
            voice_file = await context.bot.get_file(update.message.voice.file_id)
            audio_bytes = await voice_file.download_as_bytearray()
            
            transcription = ai_client.models.generate_content(
                model='gemini-2.0-flash',
                contents=[
                    genai.types.Part.from_bytes(data=bytes(audio_bytes), mime_type="audio/ogg"),
                    "Transcribe this voice note verbatim. Output only the plain transcription, nothing else."
                ]
            )
            user_text = transcription.text.strip()
        except Exception as e:
            print(f"Transcription error: {e}")
            await update.message.reply_text("Could not process your voice note. Please try again or send text.")
            return

    # 2. Handle Text input
    elif update.message.text:
        user_text = update.message.text.strip()

    if not user_text:
        return

    # 3. Retrieve relevant persistent memories from Mem0
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

    # 4. Generate response with fallback
    system_instruction = (
        "You are a helpful, direct personal AI assistant on Telegram.\n"
        f"Relevant remembered context about the user:\n{memory_context}\n\n"
        "Use this context naturally to answer if relevant."
    )

    candidate_models = ['gemini-2.0-flash', 'gemini-1.5-flash']
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
        bot_reply = "I'm experiencing temporary connection issues. Please try again shortly."

    # 5. Persist interaction to Mem0
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

    # 6. Mirror input mode in reply
    if is_voice_input:
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
            print(f"TTS conversion failed: {e}")
            await update.message.reply_text(bot_reply)
    else:
        await update.message.reply_text(bot_reply)

async def run_bot():
    bot_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))

    await bot_app.initialize()
    await bot_app.start()
    await bot_app.updater.start_polling(drop_pending_updates=True)

@server.on_event("startup")
async def on_startup():
    asyncio.create_task(run_bot())

if __name__ == "__main__":
    uvicorn.run(server, host="0.0.0.0", port=PORT)
