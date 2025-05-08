import logging
import pandas as pd
import os
import textwrap
from telegram import Update, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)
from telegram.ext import filters
from datetime import datetime
from openai import OpenAI
from dotenv import load_dotenv
load_dotenv()


TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
OWNER_ID = int(os.environ["OWNER_ID"])
LOG_FILE = os.environ.get("LOG_FILE", "news_logs.csv")

# Инициализация клиента OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)

PROMPT_TEMPLATE = """
Ты — моя текстовая версия. Я — мужчина 35-ти лет, который размышляет вслух о политике и новостях.

Вот новость:

"{news}"

Сделай следующее:
1. В первой строке **очень кратко и ёмко передай суть новости**, выдели её жирным (`**`) и добавь эмодзи (например: ❗️, 🔥, 💬 и т.п.).
2. Ниже напиши блок **\"Мой комментарий:\"** — от первого лица, без сложных слов и официоза.
3. Комментарий должен быть простым, прямым, уверенным. Ясно обозначь позицию: кто виноват, кто лукавит, что важно.
4. Пиши честно, как будто я комментирую это друзьям. Не бойся критики, но и не перегибай. Можно чуть иронии.
5. Не используй слова вроде «международное сообщество», «весьма вероятно», «напоминает шахматную партию» и т.п.
6. Ни при каких условиях не выдумывай новость, не додумывай детали, не подменяй формулировки. Комментируй только тот текст, который дан между кавычками.
7. Если после новости дан комментарий редактора — обязательно учти его. Перепиши пост с учётом замечаний. Не игнорируй редактора. Ты не должен показывать старый текст, только выдай новый финальный пост.
8. Если указан стиль комментария, соблюдай его строго: например, "строго", "с иронией", "эмоционально" или "кратко".

Формат:
**❗️[Суть одной строкой]**

Мой комментарий:
[Простой, честный комментарий от мужского лица, 2-3 предложения]
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


# Форматирование

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


# Генерация поста
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
        return raw  # не применяем extract_and_format к копирайтингу
    except Exception as e:
        logging.exception("OpenAI API error")
        return f"Не удалось сгенерировать пост: {e}"


# Обработка стиля
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


# Основной обработчик
async def unified_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("Извините, доступ к боту разрешён только владельцу.")
        return
    if context.user_data.get('copywriting_mode'):
        context.user_data['copywriting_mode'] = False
        instruction = update.message.text.strip()
        await update.message.reply_text("✍️ Пишу пост по заданию...")
        try:
            prompt = COPYWRITING_PROMPT_TEMPLATE.format(instruction=instruction)
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500,
                temperature=0.7
            )
            post = extract_and_format(response.choices[0].message.content.strip())
            context.user_data['news'] = instruction
            context.user_data['post'] = post

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
                    InlineKeyboardButton("✏️ Доработать", callback_data="revise")
                ]
            ])
            await update.message.reply_text(post, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            await update.message.reply_text(f"Произошла ошибка при генерации поста: {e}")
        return
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("Извините, доступ к боту разрешён только владельцу.")
        return
    user_input = update.message.text or update.message.caption
    user_id = update.effective_user.id

    if context.user_data.get('awaiting_revision_text'):
        context.user_data['revision_comment'] = user_input
        context.user_data['awaiting_revision_text'] = False

        news = context.user_data.get('news')
        comment = context.user_data.get('revision_comment')

        await update.message.reply_text("Дорабатываю новость...")
        result = await generate_post(news, comment=comment, is_copywriting=True)
        context.user_data['post'] = result

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
                InlineKeyboardButton("✏️ Доработать", callback_data="revise")
            ]
        ])
        await update.message.reply_text(result, parse_mode="Markdown", reply_markup=keyboard)
        return

    if not user_input or len(user_input.strip()) < 10:
        await update.message.reply_text("Пожалуйста, пришли осмысленную новость.")
        return

    if len(user_input) > 1000:
        await update.message.reply_text("Слишком длинная новость, сократи, пожалуйста.")
        return

    await update.message.reply_text("Обрабатываю новость...")

    is_topic = user_input.lower().startswith("пост:") or user_input.lower().startswith("сделай пост")
    cleaned = user_input.split(":", 1)[1].strip() if ":" in user_input else user_input
    result = await generate_post(cleaned, is_topic=is_topic, is_copywriting=context.user_data.get('copywriting_mode', False))
    context.user_data['news'] = user_input
    context.user_data['post'] = result

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Опубликовать", callback_data="publish"),
            InlineKeyboardButton("✏️ Доработать", callback_data="revise")
        ]
    ])

    await update.message.reply_text(result, parse_mode="Markdown", reply_markup=keyboard)


# Обработка кнопок
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "publish":
        post = context.user_data.get('post')
        news = context.user_data.get('news')
        if post:
            sent = await context.bot.send_message(chat_id=CHANNEL_ID, text=post, parse_mode="Markdown")
            context.user_data['last_published_message_id'] = sent.message_id
            log_to_csv(user_id, news, post)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ Пост опубликован в канал.")



    elif query.data == "revise":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📢 Серьёзно", callback_data="style_strict"),
                InlineKeyboardButton("😏 С иронией", callback_data="style_ironic")
            ],
            [
                InlineKeyboardButton("🧵 Кратко", callback_data="style_short"),
                InlineKeyboardButton("🗣 Эмоционально", callback_data="style_emotional")
            ],
            [
                InlineKeyboardButton("🛠 Другое", callback_data="revise_custom")
            ]
        ])
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Как доработать пост? Выбери стиль или укажи вручную:", reply_markup=keyboard)

    elif query.data == "revise_custom":
        context.user_data['awaiting_revision_text'] = True
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("Что нужно изменить или дополнить? Напиши комментарий.")


# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Привет! Пришли мне новость, и я превращу её в пост с комментарием. Потом ты сможешь его одобрить или доработать.")


# Команда /дай_логи
async def send_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("Извини, у тебя нет доступа к логам.")
        return

    try:
        with open(LOG_FILE, "rb") as file:
            await update.message.reply_document(document=InputFile(file), filename="news_logs.csv")
    except FileNotFoundError:
        await update.message.reply_text("Файл с логами пока не создан.")


async def undo_last_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
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


COPYWRITING_PROMPT_TEMPLATE = """
Ты — копирайтер, создающий короткие, выразительные посты от лица мужчины 35 лет. Вот задача:

{instruction}

Твоя задача:
1. Сформулируй короткий пост на основе описания.
2. В начале — броский заголовок с эмодзи (❗️, 🔥, 📢 и т.д.), используй эмодзи только для заголовка
3. Не используй заумных выражений или воды. Пиши так, как будто говоришь подписчику от первого лица.
4. Учитывай ограничения по длине или стилю, если указаны.
5. Выделяй абзацы и разделяй между собой пустой строкой. Каждый новый абзац начинается с "красной строки"
6. Если после новости дан комментарий редактора — обязательно учти его. Перепиши пост с учётом замечаний. Не игнорируй редактора. Ты не должен показывать старый текст, только выдай новый финальный пост.

Формат:
**❗️[Заголовок]**

[Текст поста]
"""


async def start_copywriting_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data['copywriting_mode'] = True
    await update.message.reply_text("📝 Включен режим копирайтера. Опиши, какой пост тебе нужен: тему, объём, стиль.")


TOPIC_PROMPT_TEMPLATE = """
Ты — копирайтер, создающий короткие, выразительные посты от лица мужчины 35 лет. Вот задача:

{topic}

ТТвоя задача:
1. Сформулируй короткий пост на основе описания.
2. В начале — броский заголовок с эмодзи (❗️, 🔥, 📢 и т.д.), используй эмодзи только для заголовка
3. Не используй заумных выражений или воды. Пиши так, как будто говоришь подписчику от первого лица.
4. Учитывай ограничения по длине или стилю, если указаны.
5. Выделяй абзацы и разделяй между собой пустой строкой. Каждый новый абзац начинается с "красной строки"
6. Если после новости дан комментарий редактора — обязательно учти его. Перепиши пост с учётом замечаний. Не игнорируй редактора. Ты не должен показывать старый текст, только выдай новый финальный пост.

Формат:
**❗️[Заголовок]**

[Текст поста]
"""


async def undo_last_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message_id = context.user_data.get('last_published_message_id')
    if message_id:
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=message_id)
            await update.message.reply_text("Последнее сообщение в канале удалено.")
        except Exception as e:
            await update.message.reply_text(f"Ошибка при удалении сообщения: {e}")
    else:
        await update.message.reply_text("Нет сообщения для удаления.")

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("logs", send_logs))
    app.add_handler(CommandHandler("post", start_copywriting_mode))
    app.add_handler(CommandHandler("undo", undo_last_message))
    app.add_handler(CallbackQueryHandler(handle_style_selection, pattern="^style_"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler((filters.TEXT | filters.Caption()) & ~filters.COMMAND, unified_message_handler))

    logging.info("Бот запущен...")
    app.run_polling()