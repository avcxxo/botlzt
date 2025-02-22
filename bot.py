import requests
import re
import time
import pytz
import logging
from datetime import datetime, timedelta
from threading import Thread, Lock
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)

# Настройка логгера
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
API_KEY = "YOUR_TELEGRAM_BOT_API_KEY"
FORUM_API_KEY = "YOUR_FORUM_API_KEY"
ALLOWED_USER_IDS = [YOUR_ALLOWED_USER_ID]
TIMEZONE = pytz.timezone('Europe/Moscow')
INTERVAL_OPTIONS = [36, 18, 12, 6]  # Доступные интервалы в часах

# Глобальные переменные
topics = {}
topics_lock = Lock()
current_interval = 6  # Текущий интервал по умолчанию
operation_paused = False  # Флаг приостановки операций

def check_access(user_id):
    return user_id in ALLOWED_USER_IDS

async def send_admin_alert(context: ContextTypes.DEFAULT_TYPE, message: str):
    for admin_id in ALLOWED_USER_IDS:
        await context.bot.send_message(
            chat_id=admin_id,
            text=f" Уведомление: {message}"
        )

def bump_topic(topic_id):
    try:
        url = f"https://api.zelenka.guru/threads/{topic_id}/bump"
        headers = {
            "accept": "application/json",
            "Authorization": f"Bearer {FORUM_API_KEY}"
        }
        response = requests.post(url, headers=headers, timeout=10)
        return response.json()
    except Exception as e:
        logger.error(f"Сетевая ошибка: {str(e)}")
        return {"errors": [f"Сетевая ошибка: {str(e)}"]}

async def show_main_menu(update: Update, message: str = "Выберите действие:"):
    keyboard = [
        [InlineKeyboardButton("➕ Добавить тему", callback_data='add_topic')],
        [InlineKeyboardButton(" Удалить тему", callback_data='remove_topic')],
        [InlineKeyboardButton(" Список тем", callback_data='list_topics')],
        [InlineKeyboardButton("⏱ Изменить интервал", callback_data='change_interval')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.message:
        await update.message.reply_text(message, reply_markup=reply_markup)
    else:
        await update.callback_query.message.reply_text(message, reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.message.from_user.id):
        await update.message.reply_text(" Доступ запрещен!")
        return
    await show_main_menu(update)

async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'add_topic':
        await query.message.reply_text("Отправьте ссылку на тему или её ID:")
        context.user_data['action'] = 'add_topic'

    elif data == 'remove_topic':
        with topics_lock:
            if not topics:
                await query.message.reply_text("Список тем пуст!")
                await show_main_menu(update)
                return
          
            keyboard = [
                [InlineKeyboardButton(f"ID: {tid}", callback_data=f'del_{tid}')]
                for tid in topics
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.message.reply_text("Выберите тему для удаления:", reply_markup=reply_markup)

    elif data.startswith('del_'):
        topic_id = data.split('_')[1]
        with topics_lock:
            if topic_id in topics:
                del topics[topic_id]
                await query.message.reply_text(f"✅ Тема {topic_id} удалена!")
            else:
                await query.message.reply_text("❌ Тема не найдена!")
        await show_main_menu(update)

    elif data == 'list_topics':
        with topics_lock:
            if not topics:
                await query.message.reply_text(" Список тем пуст!")
                await show_main_menu(update)
                return
          
            message = " Активные темы:\n\n"
            for tid, data in topics.items():
                next_time = data['next_bump_time'].astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")
                message += f" ID: {tid}\n⏰ Следующее поднятие: {next_time}\n\n"
          
            await query.message.reply_text(message)
            await show_main_menu(update)

    elif data == 'change_interval':
        keyboard = [
            [InlineKeyboardButton(f"{h} часов", callback_data=f'interval_{h}')]
            for h in INTERVAL_OPTIONS
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            "Выберите новый интервал для всех тем:",
            reply_markup=reply_markup
        )

    elif data.startswith('interval_'):
        global current_interval, operation_paused
        new_interval = int(data.split('_')[1])
        operation_paused = True
      
        with topics_lock:
            current_interval = new_interval
            now = datetime.now(TIMEZONE)
            for topic_id in topics:
                topics[topic_id]['next_bump_time'] = now + timedelta(hours=new_interval)
                topics[topic_id]['interval_hours'] = new_interval
          
            operation_paused = False
            await query.message.reply_text(
                f"✅ Интервал для всех тем изменен на {new_interval} часов!\n"
                "Следующее поднятие через указанный интервал."
            )
        await show_main_menu(update)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.message.from_user.id):
        return

    text = update.message.text.strip()
    user_data = context.user_data.get('action')

    if user_data == 'add_topic':
        try:
            original_input = text
            match = re.search(r'\d+', text)
            if not match:
                raise ValueError("Некорректный формат")
            topic_id = match.group(0)
          
            with topics_lock:
                if topic_id in topics:
                    next_time = topics[topic_id]['next_bump_time'].astimezone(TIMEZONE).strftime("%d.%m.%Y %H:%M")
                    await update.message.reply_text(
                        f"⚠ Тема {topic_id} уже есть в списке!\n"
                        f"Следующее поднятие: {next_time}"
                    )
                    context.user_data.pop('action')
                    await show_main_menu(update)
                    return

                response = bump_topic(topic_id)
                now = datetime.now(TIMEZONE)

                topics[topic_id] = {
                    'next_bump_time': now + timedelta(hours=current_interval),
                    'interval_hours': current_interval,
                    'original_input': original_input
                }
              
                next_time = topics[topic_id]['next_bump_time'].strftime("%d.%m.%Y %H:%M")
                await update.message.reply_text(
                    f"✅ Тема {topic_id} успешно добавлена!\n"
                    f"Следующее поднятие: {next_time}"
                )
              
                context.user_data.pop('action')
                await show_main_menu(update)

        except Exception as e:
            await update.message.reply_text("❌ Ошибка! Убедитесь что отправили правильную ссылку или ID темы")
            context.user_data.pop('action', None)
            await show_main_menu(update)

def start_bumping(application):
    while True:
        if not operation_paused:
            now = datetime.now(TIMEZONE)
            with topics_lock:
                for topic_id, data in list(topics.items()):
                    if now >= data['next_bump_time'] and not operation_paused:
                        response = bump_topic(topic_id)
                        new_time = now + timedelta(hours=data['interval_hours'])
                      
                        if "errors" in response:
                            error = "\n".join(response["errors"])
                            application.bot.send_message(
                                chat_id=ALLOWED_USER_IDS[0],
                                text=f"❌ Ошибка в теме {topic_id}:\n{error}"
                            )
                        else:
                            topics[topic_id]['next_bump_time'] = new_time
                            application.bot.send_message(
                                chat_id=ALLOWED_USER_IDS[0],
                                text=f"✅ Тема {topic_id} поднята!\nСледующее: {new_time.strftime('%d.%m.%Y %H:%M')}"
                            )
        time.sleep(60)

if __name__ == "__main__":
    application = Application.builder().token(API_KEY).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT, handle_message))

    bump_thread = Thread(target=start_bumping, args=(application,))
    bump_thread.daemon = True
    bump_thread.start()

    application.run_polling()
