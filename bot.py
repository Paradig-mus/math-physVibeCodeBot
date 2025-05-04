import os
import logging
import tempfile
import requests
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    ContextTypes, filters
)
import psycopg2
from psycopg2.extras import RealDictCursor
from PyPDF2 import PdfReader

# === Configuration ===
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PG_DSN = os.getenv("PG_DSN", "postgresql://user:pass@localhost:5432/dbname")
GEMINI_URL = "https://gemini.googleapis.com/v1/chat"
GEMINI_MODEL = "models/gemini-2.5-flash-preview-04-17"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

logging.basicConfig(level=logging.INFO)

# === Database Helpers ===
def get_db_conn():
    return psycopg2.connect(PG_DSN, cursor_factory=RealDictCursor)

def save_chat_memory(chat_id, role, content):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_memory (chat_id, role, content) VALUES (%s, %s, %s)",
                (str(chat_id), role, content)
            )
            conn.commit()

def load_chat_memory(chat_id, limit=20):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role, content FROM chat_memory WHERE chat_id = %s ORDER BY created_at DESC LIMIT %s",
                (str(chat_id), limit)
            )
            rows = cur.fetchall()
    return rows[::-1]

def query_tasks(file_id, text):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM gd24_tasks WHERE file_id = %s AND question ILIKE %s LIMIT 5",
                (file_id, f"%{text}%")
            )
            return cur.fetchall()

# === AI Agent ===
def call_gemini(chat_id, user_text, system_message=False):
    memory = load_chat_memory(chat_id)
    messages = []
    sys = ("You are a “Smart Math & Physics Solver” intelligent assistant with ability to accumulate and apply scientific knowledge from admin-provided PDFs."
           if not system_message else user_text)
    messages.append({"role": "system", "content": sys})
    for m in memory:
        messages.append({"role": m['role'], "content": m['content']})
    if not system_message:
        messages.append({"role": "user", "content": user_text})

    resp = requests.post(
        GEMINI_URL,
        params={"key": GEMINI_API_KEY},
        json={"model": GEMINI_MODEL, "messages": messages, "temperature": 0.2}
    )
    resp.raise_for_status()
    data = resp.json()
    content = data['choices'][0]['message']['content']
    save_chat_memory(chat_id, "user", user_text)
    save_chat_memory(chat_id, "assistant", content)
    return content

def render_latex(latex_expr):
    url = "https://latex.codecogs.com/png.latex"
    r = requests.get(url, params={"latex": latex_expr}, headers={"Accept-Encoding": "identity"})
    r.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".png")
    with os.fdopen(fd, 'wb') as f:
        f.write(r.content)
    return path

# === Handlers ===
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    file = update.message.document
    if user_id != ADMIN_ID:
        await update.message.reply_text("Извините, только админ может загружать PDF для обучения.")
        return
    file_id = file.file_id
    file_path = os.path.join(DOWNLOAD_DIR, f"{file_id}.pdf")
    file_obj = await file.get_file()
    await file_obj.download_to_drive(file_path)

    reader = PdfReader(file_path)
    text = "\n".join(p.extract_text() or "" for p in reader.pages)

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pdf_knowledge (file_id, content) VALUES (%s, %s)",
                (file_id, text)
            )
            conn.commit()

    context.user_data['last_pdf_id'] = file_id
    context.user_data['systemMessage'] = True
    await update.message.reply_text("PDF успешно сохранён в базе знаний.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text or ''
    if any(w in text.lower() for w in ['hi', 'hello', 'привет', 'здравствуй']):
        await update.message.reply_text("Привет! Чем могу помочь?")
        return

    if context.user_data.get('systemMessage'):
        context.user_data['systemMessage'] = False
        return

    last_pdf = context.user_data.get('last_pdf_id')
    if last_pdf:
        tasks = query_tasks(last_pdf, text)
        # Можно вставить задачи в prompt

    answer = call_gemini(chat_id, text)
    if '$' in answer:
        expr = "A = A_0 \\left(\\frac{1}{2}\\right)^{t/T_{1/2}}"
        img_path = render_latex(expr)
        html = answer.replace('**', '<b>').replace('$', '')
        await update.message.reply_html(html)
        await update.message.reply_photo(photo=open(img_path, 'rb'))
    else:
        await update.message.reply_text(answer)




async def main():
    from telegram.ext import Application
    from telegram.ext import Defaults
    from telegram import Bot

    # Получаем переменные из среды
    port = int(os.getenv("PORT", 8080))
    webhook_path = "/webhook"
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME")
    if not hostname:
        raise RuntimeError("RENDER_EXTERNAL_HOSTNAME не установлена")

    # Создаем приложение
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Регистрируем handlers
    app.add_handler(MessageHandler(filters.Document.MimeType("application/pdf"), handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Удаляем старые webhook-и
    await app.bot.delete_webhook()

    # Запускаем Webhook
    await app.run_webhook(
        listen="0.0.0.0",
        port=port,
        webhook_path=webhook_path,
        webhook_url=f"https://{hostname}{webhook_path}",
        drop_pending_updates=True,
    )

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
