import logging
import pandas as pd
import textwrap
from telegram import Update, InputFile, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)
from telegram.ext import filters
from datetime import datetime
from openai import OpenAI
from config import TELEGRAM_TOKEN, OPENAI_API_KEY, CHANNEL_ID, OWNER_ID, LOG_FILE

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
async def generate_post(news: str, comment: str = None, style: str = None) -> str:
    prompt = PROMPT_TEMPLATE.format(news=news)

    if style:
        prompt += f"\n\nСтиль комментария: {style}"

    if comment:
        prompt += (
            f"\n\nКомментарий редактора:\n{comment}\n\n"
            "Перепиши весь пост целиком с учётом комментария редактора. "
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
        return extract_and_format(raw)
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
    user_input = update.message.text or update.message.caption
    user_id = update.effective_user.id

    if context.user_data.get('awaiting_revision_text'):
        context.user_data['revision_comment'] = user_input
        context.user_data['awaiting_revision_text'] = False

        news = context.user_data.get('news')
        comment = context.user_data.get('revision_comment')

        await update.message.reply_text("Дорабатываю новость...")
        result = await generate_post(news, comment=comment)
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

# Обработка кнопок
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "publish":
        post = context.user_data.get('post')
        news = context.user_data.get('news')
        if post:
            await context.bot.send_message(chat_id=CHANNEL_ID, text=post, parse_mode="Markdown")
            log_to_csv(user_id, news, post)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ Пост опубликован в канал.")

            # Статистика публикаций
            try:
                df = pd.read_csv(LOG_FILE)
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                today = datetime.now().date()
                published_today = df[df['timestamp'].dt.date == today].shape[0]
                total_published = df.shape[0]
                await query.message.reply_text(
                    f"📊 Статистика:\nСегодня опубликовано: {published_today}\nВсего опубликовано: {total_published}"
                )
            except Exception as e:
                logging.warning(f"Не удалось получить статистику: {e}")

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
    await update.message.reply_text("Привет! Пришли мне новость, и я превращу её в пост с комментарием. Потом ты сможешь его одобрить или доработать.")

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

# Запуск
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("logs", send_logs))
    app.add_handler(CallbackQueryHandler(handle_style_selection, pattern="^style_"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler((filters.TEXT | filters.Caption()) & ~filters.COMMAND, unified_message_handler))

    logging.info("Бот запущен...")
    app.run_polling()