import os
import logging
import io
import uuid
import time
from pathlib import Path
from collections import defaultdict

# Библиотеки для API
import google.generativeai as genai
from dotenv import load_dotenv

# Библиотеки для Telegram
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

# Библиотека для обработки изображений
from PIL import Image

# Импортируем наш модуль для работы с базой данных
import database as db

# ------------------- НАСТРОЙКА И ИНИЦИАЛИЗАЦИЯ -------------------

load_dotenv()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

db.init_db()
logger.info("База данных SQLite инициализирована.")

MAX_FILE_SIZE = 20 * 1024 * 1024

# --- Система защиты от флуда (троттлинг) ---
# Словарь для хранения времени последнего запроса от каждого пользователя.
# Используем defaultdict(float), чтобы по умолчанию время было 0.0
user_last_request = defaultdict(float)

# Задержки в секундах для разных типов команд.
# Это позволяет установить более строгие ограничения на ресурсоемкие операции.
COOLDOWNS = {
    "text": 3,
    "fact": 5,
    "photo": 10,
    "voice": 15,
    "video": 20,
    "settings": 2
}


def is_user_on_cooldown(user_id: int, command_type: str) -> bool:
    """Проверяет, находится ли пользователь на кулдауне для данного типа команды."""
    cooldown_period = COOLDOWNS.get(command_type, 2)  # По умолчанию 2 сек.
    time_since_last_request = time.time() - user_last_request[user_id]

    if time_since_last_request < cooldown_period:
        logger.warning(
            f"Пользователь {user_id} отправил запрос типа '{command_type}' слишком часто. "
            f"Осталось ждать: {cooldown_period - time_since_last_request:.1f} сек."
        )
        return True  # Да, пользователь на кулдауне
    return False  # Нет, можно выполнять


def update_user_timestamp(user_id: int):
    """Обновляет время последнего запроса для пользователя."""
    user_last_request[user_id] = time.time()


# --- Конец системы защиты от флуда ---


try:
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        raise ValueError("GEMINI_API_KEY не найден в .env файле")
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel('gemini-1.5-flash-latest')
    logger.info("Модель Gemini ('gemini-1.5-flash-latest') успешно загружена.")
except Exception as e:
    logger.error(f"Критическая ошибка при конфигурации Gemini: {e}")
    model = None

# --- ГЛАВНАЯ КЛАВИАТУРА (REPLY KEYBOARD) ---
main_keyboard = [
    [KeyboardButton("❓ Задать вопрос"), KeyboardButton("💡 Случайный факт")],
    [KeyboardButton("⚙️ Настройки")]
]
main_reply_markup = ReplyKeyboardMarkup(main_keyboard, resize_keyboard=True)


async def send_long_message(update: Update, text: str, parse_mode: str = None):
    MAX_LENGTH = 4096
    if len(text) <= MAX_LENGTH:
        await update.message.reply_text(text, parse_mode=parse_mode)
        return
    parts = []
    while len(text) > 0:
        if len(text) <= MAX_LENGTH: parts.append(text); break
        cut_off = text.rfind('\n', 0, MAX_LENGTH)
        if cut_off == -1: cut_off = text.rfind(' ', 0, MAX_LENGTH)
        if cut_off == -1: cut_off = MAX_LENGTH
        parts.append(text[:cut_off])
        text = text[cut_off:].lstrip()
    for part in parts:
        await update.message.reply_text(part, parse_mode=part)
        time.sleep(0.5)


# --- Клавиатуры и меню настроек ---
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    # Проверяем кулдаун, только если это не нажатие на inline-кнопку
    if not update.callback_query and is_user_on_cooldown(user_id, "settings"):
        return

    update_user_timestamp(user_id)  # Обновляем время, чтобы предотвратить спам командой /settings

    current_limit = db.get_history_limit(user_id)
    text = (f"⚙️ *Настройки бота*\n\nТекущий лимит истории: *{current_limit} сообщений*.")
    keyboard = [[InlineKeyboardButton("📝 Изменить лимит истории", callback_data='settings_limit_menu')],
                [InlineKeyboardButton("🗑️ Очистить историю", callback_data='settings_clear')],
                [InlineKeyboardButton("❌ Закрыть", callback_data='settings_close')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')


async def settings_limit_menu(query: Update.callback_query) -> None:
    text = "Выберите, сколько последних сообщений бот должен помнить. (Максимум 20)"
    keyboard = [
        [InlineKeyboardButton("4", callback_data='set_limit_4'), InlineKeyboardButton("8", callback_data='set_limit_8'),
         InlineKeyboardButton("12", callback_data='set_limit_12')],
        [InlineKeyboardButton("16", callback_data='set_limit_16'),
         InlineKeyboardButton("20", callback_data='set_limit_20')],
        [InlineKeyboardButton("⬅️ Назад", callback_data='settings_main_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup)


async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    callback_data = query.data
    if callback_data == 'settings_main_menu':
        await settings_menu(update, context)
    elif callback_data == 'settings_limit_menu':
        await settings_limit_menu(query)
    elif callback_data.startswith('set_limit_'):
        limit = int(callback_data.split('_')[-1])
        db.set_history_limit(user_id, limit)
        await query.answer(f"✅ Лимит истории установлен: {limit}", show_alert=True)
        await settings_menu(update, context)
    elif callback_data == 'settings_clear':
        db.clear_history(user_id)
        await query.answer("✅ Ваша история диалога была полностью очищена!", show_alert=True)
        await query.edit_message_text("История очищена. Это меню можно закрыть.")
    elif callback_data == 'settings_close':
        await query.message.delete()


# ------------------- ОБРАБОТЧИКИ КОМАНД И ОСНОВНЫХ КНОПОК -------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db.clear_history(user.id)
    logger.info(f"История для пользователя {user.id} в базе данных очищена.")
    await update.message.reply_html(
        f"Привет, {user.mention_html()}! 👋\n\nЯ — твой персональный ассистент на базе Google Gemini.\n\nИспользуй кнопки ниже, чтобы задать вопрос или узнать что-нибудь новое. Ты также можешь просто отправить мне текст, фото, видео или голосовое сообщение.",
        reply_markup=main_reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "💡 <b>Краткая справка по моим возможностям:</b>\n\n"
        "• <b>Кнопки внизу</b> — основной способ взаимодействия.\n"
        "• <b>Текст, фото, голос, видео</b> — я пойму любое ваше сообщение.\n"
        "• <code>/settings</code> — открывает меню для настройки и очистки истории.\n"
        "• <code>/start</code> — полный сброс диалога и возврат в главное меню.",
        reply_markup=main_reply_markup
    )


async def random_fact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_user_on_cooldown(user_id, "fact"):
        return

    try:
        update_user_timestamp(user_id)
        if not model: return
        logger.info(f"Пользователь {user_id} запросил случайный факт.")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

        prompt = "Расскажи один очень интересный, удивительный и малоизвестный научный или исторический факт..."

        # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
        # Старый код, который упирался в лимит:
        # response = model.generate_content(prompt)

        # Новый код: используем временную сессию чата, чтобы обойти лимит
        # Мы не передаем историю, поэтому это чистый запрос, не влияющий на контекст пользователя
        chat = model.start_chat(history=[])
        response = await chat.send_message_async(prompt)
        # --- КОНЕЦ ИЗМЕНЕНИЯ ---

        await send_long_message(update, f"💡 <b>Вот интересный факт:</b>\n\n{response.text}", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Ошибка при генерации случайного факта: {e}")
        # Теперь можно добавить более конкретное сообщение об ошибке
        if "quota" in str(e).lower():
            await update.message.reply_text(
                "😔 К сожалению, на сегодня лимит интересных фактов исчерпан. Попробуйте завтра!")
        else:
            await update.message.reply_text("😔 К сожалению, не удалось придумать факт.")


# ------------------- ОБРАБОТЧИКИ КОНТЕНТА -------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message_text = update.message.text

    # --- Проверяем кнопки и вызываем соответствующие функции ---
    # Эти операции быстрые и не требуют кулдауна здесь,
    # так как вызываемые функции имеют свою собственную проверку.
    if message_text == "❓ Задать вопрос":
        await update.message.reply_text(
            "Слушаю вас! Задавайте свой вопрос текстом, голосом или отправляйте фото/видео с подписью.")
        return
    if message_text == "💡 Случайный факт":
        await random_fact(update, context)
        return
    if message_text == "⚙️ Настройки":
        await settings_menu(update, context)
        return

    # --- Если это обычный текст, проверяем кулдаун и обрабатываем ---
    if is_user_on_cooldown(user_id, "text"):
        return

    try:
        update_user_timestamp(user_id)
        if not model: return

        logger.info(f"Получен текст от {user_id}: {message_text}")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

        user_history = db.get_history(user_id)
        chat = model.start_chat(history=user_history)
        response = await chat.send_message_async(message_text)
        full_response_text = response.text
        db.add_message_to_history(user_id, "user", [{"text": message_text}])
        db.add_message_to_history(user_id, "model", [{"text": full_response_text}])
        await send_long_message(update, full_response_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Ошибка при обработке текста с БД: {e}")
        await update.message.reply_text("😔 Произошла ошибка при обработке вашего запроса.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_user_on_cooldown(user_id, "photo"):
        return

    try:
        if update.message.photo[-1].file_size > MAX_FILE_SIZE:
            await update.message.reply_text("😔 Ой, это фото слишком большое. Лимит — 20 МБ.")
            return

        update_user_timestamp(user_id)
        if not model: return

        # --- НАЧАЛО ИЗМЕНЕНИЙ ---

        user_caption = update.message.caption

        # Создаем более надежный и понятный для модели промпт
        if user_caption:
            # Если пользователь оставил подпись, мы вставляем ее в наш "шаблон"
            prompt = (
                f"Проанализируй это изображение и ответь на следующий вопрос или выполни просьбу: '{user_caption}'. "
                "Сначала кратко опиши, что ты видишь на фото, а затем дай развернутый ответ."
            )
            log_prompt = user_caption  # В лог записываем оригинальный промпт
        else:
            # Если подписи нет, используем промпт по умолчанию
            prompt = "Опиши подробно, что изображено на этой картинке?"
            log_prompt = prompt

        # --- КОНЕЦ ИЗМЕНЕНИЙ ---

        logger.info(f"Получено фото от {user_id} с запросом: '{log_prompt}'")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        img = Image.open(io.BytesIO(photo_bytes))

        user_history = db.get_history(user_id)
        chat = model.start_chat(history=user_history)

        # Используем наш новый, улучшенный промпт
        response = await chat.send_message_async([prompt, img])
        full_response_text = response.text

        # В историю сохраняем оригинальный промпт пользователя для чистоты контекста
        db.add_message_to_history(user_id, "user", [{"text": log_prompt}])
        db.add_message_to_history(user_id, "model", [{"text": full_response_text}])
        await send_long_message(update, full_response_text, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Ошибка при обработке фото с БД: {e}")
        await update.message.reply_text("😔 Не удалось обработать изображение.")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_user_on_cooldown(user_id, "voice"):
        return

    file_path, gemini_file = None, None
    try:
        if update.message.voice.file_size > MAX_FILE_SIZE:
            await update.message.reply_text("😔 Ой, это голосовое сообщение слишком большое. Лимит — 20 МБ.")
            return

        update_user_timestamp(user_id)
        if not model: return

        logger.info(f"Получено голосовое сообщение от {user_id}")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

        voice_file = await update.message.voice.get_file()
        temp_dir = Path("temp_audio");
        temp_dir.mkdir(exist_ok=True)
        file_path = temp_dir / f"{uuid.uuid4()}.ogg"
        await voice_file.download_to_drive(file_path)

        gemini_file = genai.upload_file(path=file_path)
        while gemini_file.state.name == "PROCESSING":
            time.sleep(2);
            gemini_file = genai.get_file(gemini_file.name)
        if gemini_file.state.name == "FAILED": raise ValueError("Ошибка обработки файла Gemini.")

        prompt = (
            "Выступи в роли ассистента, который анализирует аудио. Твоя задача — точно расшифровать речь и дать содержательный ответ по сути сказанного. "
            "Игнорируй любые технические артефакты: фоновый шум, щелчки, помехи. "
            "Твой ответ должен начинаться сразу, без вступлений. "
            "Структура ответа:\n"
            "*🗣️ Расшифровка:* [здесь дословный текст из аудио]\n"
            "*🤖 Ответ:* [здесь твой комментарий по сути расшифровки]"
        )
        user_history = db.get_history(user_id)
        chat = model.start_chat(history=user_history)
        response = await chat.send_message_async([prompt, gemini_file])
        full_response_text = response.text

        db.add_message_to_history(user_id, "user", [{"text": "(Голосовое сообщение)"}])
        db.add_message_to_history(user_id, "model", [{"text": full_response_text}])
        await send_long_message(update, full_response_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Ошибка при обработке голоса с БД: {e}")
        await update.message.reply_text("😔 Не удалось обработать голосовое сообщение.")
    finally:
        if file_path and file_path.exists(): file_path.unlink()
        if gemini_file: genai.delete_file(gemini_file.name)


async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_user_on_cooldown(user_id, "video"):
        return

    file_path, gemini_file = None, None
    try:
        if update.message.video_note.file_size > MAX_FILE_SIZE:
            await update.message.reply_text("😔 Ой, это видео слишком большое. Лимит — 20 МБ.")
            return

        update_user_timestamp(user_id)
        if not model: return

        logger.info(f"Получено видеосообщение от {user_id}")
        await update.message.reply_text("Видео получено, начинаю обработку... Это может занять некоторое время.")
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='upload_video')

        video_note_file = await update.message.video_note.get_file()
        temp_dir = Path("temp_video");
        temp_dir.mkdir(exist_ok=True)
        file_path = temp_dir / f"{uuid.uuid4()}.mp4"
        await video_note_file.download_to_drive(file_path)

        gemini_file = genai.upload_file(path=file_path)
        while gemini_file.state.name == "PROCESSING":
            time.sleep(5);
            gemini_file = genai.get_file(gemini_file.name)
        if gemini_file.state.name == "FAILED": raise ValueError("Ошибка обработки видео Gemini.")

        prompt = (
            "Выступи в роли ассистента, который анализирует видео. Твоя задача — предоставить концентрированный отчет о его содержании. "
            "Сосредоточься на основном объекте, его действиях, речи и эмоциях. Игнорируй элементы интерфейса (например, логотип Telegram) и технические артефакты записи (шум, низкое качество видео/аудио). "
            "Твой ответ должен начинаться сразу, без вступлений. "
            "Структура ответа:\n"
            "*🎬 На видео:* [здесь краткое, но точное описание действий и речи]\n"
            "*💡 Комментарий:* [здесь твой развернутый анализ или ответ по сути увиденного]"
        )
        user_history = db.get_history(user_id)
        chat = model.start_chat(history=user_history)
        response = await chat.send_message_async([prompt, gemini_file])
        full_response_text = response.text

        db.add_message_to_history(user_id, "user", [{"text": "(Видеосообщение)"}])
        db.add_message_to_history(user_id, "model", [{"text": full_response_text}])
        await send_long_message(update, full_response_text, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Ошибка при обработке видеосообщения: {e}")
        await update.message.reply_text("😔 К сожалению, не удалось обработать это видеосообщение.")
    finally:
        if file_path and file_path.exists(): file_path.unlink()
        if gemini_file: genai.delete_file(gemini_file.name)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_name = update.effective_user.first_name
    logger.info(f"Пользователь {user_name} ({update.effective_user.id}) отправил обычное видео.")
    await update.message.reply_html(
        f"👋 Привет, {user_name}!\n\n"
        "Спасибо за видео! 📹 На данный момент я умею анализировать только короткие видеосообщения (\"кружочки\").\n\n"
        "Поддержка обычных видеофайлов уже в разработке и появится в будущих обновлениях. Следите за новостями! 😉"
    )


# ------------------- ОСНОВНАЯ ФУНКЦИЯ ЗАПУСКА -------------------
def main() -> None:
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token or not model:
        logger.critical("Не найден токен Telegram или не загружена модель Gemini! Бот не может быть запущен.")
        return

    application = Application.builder().token(telegram_token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settings", settings_menu))
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))

    logger.info("Бот запускается...")
    application.run_polling()


if __name__ == '__main__':
    main()