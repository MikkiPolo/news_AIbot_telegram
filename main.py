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

def log_to_csv(user_id: int, news: str, response: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = {"timestamp": now, "user_id": user_id, "news": news, "response": response}
    try:
        df = pd.read_csv(LOG_FILE)
    except FileNotFoundError:
        df = pd.DataFrame(columns=["timestamp", "user_id", "news", "response"])
    df = pd.concat([df, pd.DataFrame([entry])], ignore_index=True)
    df.to_csv(LOG_FILE, index=False)

async def generate_post(news: str, comment: str = None, style: str = None, is_topic: bool = False, is_copywriting: bool = False) -> str:
    prompt = COPYWRITING_PROMPT_TEMPLATE.format(instruction=news) if is_topic or is_copywriting else PROMPT_TEMPLATE.format(news=news)

    if style:
        prompt += f"\n\nСтиль комментария: {style}"

    if comment:
        prompt += (
            f"\n\nКомментарий редактора:\n{comment}\n\n"
            "Перепиши весь пост целиком с учётом замечаний редактора."
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
        return "⚠️ Не удалось сгенерировать пост. Попробуй позже."

async def unified_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("Извините, доступ к боту разрешён только владельцу.")
        return

    # ✅ Если включён режим копирайтера — генерируем по шаблону COPYWRITING
    if context.user_data.get("copywriting_mode"):
        context.user_data["copywriting_mode"] = False
        instruction = update.message.text or update.message.caption
        post = await generate_post(instruction, is_copywriting=True)
        context.user_data["news"] = instruction
        context.user_data["post"] = post

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
                InlineKeyboardButton("✏️ Доработать", callback_data="revise")
            ]
        ])
        await update.message.reply_text(post, parse_mode="Markdown", reply_markup=keyboard)
        return

    # ✅ Здесь вставляем перезапись media
    if update.message.photo:
        context.user_data['media'] = update.message.photo[-1].file_id
        context.user_data['media_type'] = 'photo'
    elif update.message.video:
        context.user_data['media'] = update.message.video.file_id
        context.user_data['media_type'] = 'video'

    user_input = update.message.text or update.message.caption
    if context.user_data.get("revision_mode"):
        context.user_data["revision_mode"] = False
        original_post = context.user_data.get("news")
        comment = user_input

        is_copywriting = context.user_data.get("copywriting_mode_active", False)
        revised = await generate_post(original_post, comment=comment, is_copywriting=is_copywriting)

        context.user_data["post"] = revised

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
                InlineKeyboardButton("✏️ Доработать", callback_data="revise")
            ]
        ])
        await update.message.reply_text(revised, parse_mode="Markdown", reply_markup=keyboard)
        return
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

    if query.data == "publish":
        post = context.user_data.get('post')
        news = context.user_data.get('news')
        media_id = context.user_data.get('media')
        media_type = context.user_data.get('media_type')

        try:
            if media_id and media_type == 'photo':
                sent = await context.bot.send_photo(chat_id=CHANNEL_ID, photo=media_id, caption=post,
                                                    parse_mode="Markdown")
            elif media_id and media_type == 'video':
                sent = await context.bot.send_video(chat_id=CHANNEL_ID, video=media_id, caption=post,
                                                    parse_mode="Markdown")
            else:
                sent = await context.bot.send_message(chat_id=CHANNEL_ID, text=post, parse_mode="Markdown")

            context.user_data['last_published_message_id'] = sent.message_id
            context.user_data['media'] = None
            context.user_data['media_type'] = None
            context.user_data['copywriting_mode_active'] = False
            log_to_csv(query.from_user.id, news, post)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ Пост опубликован в канал.")
        except Exception as e:
            await query.message.reply_text(f"Ошибка при публикации: {e}")

    elif query.data == "revise":
        context.user_data["revision_mode"] = True
        await query.message.reply_text("✏️ Напиши, что изменить или уточни пожелания к посту.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Привет! Пришли мне новость, и я превращу её в пост с комментарием. Потом ты сможешь его одобрить или доработать.")

async def start_copywriting_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data['copywriting_mode'] = True
    context.user_data['copywriting_mode_active'] = True
    await update.message.reply_text("📝 Режим копирайтера включён. Пришли описание поста.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(CommandHandler("post", start_copywriting_mode))
    app.add_handler(MessageHandler((filters.TEXT | filters.Caption()) & ~filters.COMMAND, unified_message_handler))
    logging.info("Бот запущен...")
    app.run_polling()
