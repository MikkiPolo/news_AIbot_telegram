import logging
import pandas as pd
import os
from telegram import Update, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
OWNER_ID = int(os.environ["OWNER_ID"])
LOG_FILE = os.environ.get("LOG_FILE", "news_logs.csv")

client = OpenAI(api_key=OPENAI_API_KEY)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

PROMPT_TEMPLATE = """
Ты — личный новостной ассистент, пишущий от лица гражданина России, реагируя на свежие заголовки.

Вот новость:

"{news}"

Сгенерируй полноценный пост по шаблону:

1. Сначала — краткая суть новости (1–2 предложения).
2. Затем — комментарий (2–3 предложения).
3. Раздели смысловые части эмодзи (см. ниже).
4. Обязательно используй стиль: Иронично и по-человечески, Прямолинейно, или Саркастично, в зависимости от содержания новости.
5. Не добавляй воду, канцелярит, клише. Пиши живо, от лица россиянина.

📌 Формат поста:

[Эмодзи из списка] [Краткая суть новости]

[Эмодзи из списка] Мой комментарий: [твой комментарий]

Выводи строго по формату. Не повторяй инструкций.
"""

COPYWRITING_PROMPT_TEMPLATE = """
Ты — копирайтер, создающий короткие, выразительные посты от лица мужчины 35 лет. Вот задача:

{instruction}

Формат:
**❗️[Заголовок]**

[Текст поста]
"""

# Лог в .csv
def log_to_csv(user_id: int, news: str, response: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {"timestamp": now, "user_id": user_id, "news": news, "response": response}
    try:
        df = pd.read_csv(LOG_FILE)
    except FileNotFoundError:
        df = pd.DataFrame(columns=["timestamp", "user_id", "news", "response"])
    df = pd.concat([df, pd.DataFrame([entry])], ignore_index=True)
    df.to_csv(LOG_FILE, index=False)

def extract_and_format(response: str) -> str:
    response = response.replace("Мой комментарий:\n", "Мой комментарий: ")
    lines = response.strip().splitlines()
    formatted = []
    headline_done = False

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if not headline_done and not line.startswith("**") and "Комментарий:" not in line:
            formatted.append(f"**❗️{line}**")
            headline_done = True
        else:
            formatted.append(line)

    return "\n\n".join(formatted)

async def generate_post(news: str, comment: str = None, style: str = None, is_topic: bool = False, is_copywriting: bool = False) -> str:
    if is_topic or is_copywriting:
        prompt = COPYWRITING_PROMPT_TEMPLATE.format(instruction=news)
    else:
        prompt = PROMPT_TEMPLATE.format(news=news)

    if style:
        prompt += f"\n\nСтиль комментария: {style}"

    if comment:
        prompt += (
            f"\n\nКомментарий редактора:\n{comment}\n\n"
            "Перепиши весь пост целиком с учётом замечаний редактора. "
            "Не добавляй исходный текст, не повторяй инструкции, просто выдай финальный пост."
        )

    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.7
        )
        raw = response.choices[0].message.content.strip()
        return raw if is_copywriting else extract_and_format(raw)
    except Exception as e:
        logging.exception("OpenAI API error")
        return f"Не удалось сгенерировать пост: {e}"

async def handle_style_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    style_map = {
        "style_strict": "строго",
        "style_ironic": "с иронией",
        "style_short": "кратко",
        "style_emotional": "эмоционально"
    }
    style = style_map.get(query.data)
    context.user_data['style'] = style

    news = context.user_data.get('news')
    comment = context.user_data.get('revision_comment', '')

    await query.message.reply_text("Дорабатываю новость...")
    result = await generate_post(news, comment=comment, style=style)
    context.user_data['post'] = result
    context.user_data['awaiting_revision'] = False

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
            InlineKeyboardButton("✏️ Доработать", callback_data="revise")
        ]
    ])

    await query.message.reply_text(result, parse_mode="Markdown", reply_markup=keyboard)

async def unified_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("Извините, доступ к боту разрешён только владельцу.")
        return

    # Сохраняем media
    if update.message.photo:
        context.user_data['media'] = update.message.photo[-1].file_id
        context.user_data['media_type'] = 'photo'
    elif update.message.video:
        context.user_data['media'] = update.message.video.file_id
        context.user_data['media_type'] = 'video'
    else:
        context.user_data['media'] = None
        context.user_data['media_type'] = None

    user_input = update.message.text or update.message.caption
    if not user_input or len(user_input.strip()) < 10:
        await update.message.reply_text("Пожалуйста, пришли осмысленную новость.")
        return

    await update.message.reply_text("Обрабатываю новость...")

    result = await generate_post(user_input)
    context.user_data['news'] = user_input
    context.user_data['post'] = result

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
            InlineKeyboardButton("✏️ Доработать", callback_data="revise")
        ]
    ])
    await update.message.reply_text(result, parse_mode="Markdown", reply_markup=keyboard)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "publish":
        post = context.user_data.get('post')
        news = context.user_data.get('news')
        media_id = context.user_data.get('media')
        media_type = context.user_data.get('media_type')

        try:
            if media_id:
                if media_type == 'photo':
                    sent = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=media_id, caption=post, parse_mode="Markdown")
                elif media_type == 'video':
                    sent = await context.bot.send_video(chat_id=CHANNEL_ID, video=media_id, caption=post, parse_mode="Markdown")
            else:
                sent = await context.bot.send_message(chat_id=CHANNEL_ID, text=post, parse_mode="Markdown")

            context.user_data['last_published_message_id'] = sent.message_id
            log_to_csv(user_id, news, post)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ Пост опубликован в канал.")
        except Exception as e:
            await query.message.reply_text(f"Ошибка при публикации: {e}")

    elif query.data == "revise":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📢 Серьёзно", callback_data="style_strict"),
                InlineKeyboardButton("😏 С иронией", callback_data="style_ironic")
            ],
            [
                InlineKeyboardButton("🧵 Кратко", callback_data="style_short"),
                InlineKeyboardButton("🗣 Эмоционально", callback_data="style_emotional")
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Как доработать пост? Выбери стиль:", reply_markup=keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Пришли мне новость, и я превращу её в пост с комментарием. Потом ты сможешь его одобрить или доработать.")

async def send_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("Извини, у тебя нет доступа к логам.")
        return

    try:
        with open(LOG_FILE, "rb") as file:
            await update.message.reply_document(document=InputFile(file), filename="news_logs.csv")
    except FileNotFoundError:
        await update.message.reply_text("Файл с логами пока не создан.")

async def undo_last_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("У тебя нет прав на удаление публикации.")
        return

    message_id = context.user_data.get('last_published_message_id')
    if message_id:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=message_id)
            await update.message.reply_text("🗑 Последняя публикация удалена из канала.")
        except Exception as e:
            await update.message.reply_text(f"Ошибка при удалении сообщения: {e}")
    else:
        await update.message.reply_text("Нет опубликованного сообщения для удаления.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("logs", send_logs))
    app.add_handler(CommandHandler("undo", undo_last_message))
    app.add_handler(CallbackQueryHandler(handle_style_selection, pattern="^style_"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler((filters.TEXT | filters.Caption()) & ~filters.COMMAND, unified_message_handler))

    logging.info("Бот запущен...")
    app.run_polling()
