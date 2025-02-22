import asyncio
import logging
import time
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from sqlite3 import connect
from urllib.parse import urlparse
import aiohttp

# Конфигурация
API_TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN'
LZT_API_KEY = 'YOUR_LZT_API_KEY'
ALLOWED_USER_ID = 123456789  # Ваш Telegram ID

# Настройка базы данных
DB = connect('lzt_bot.db')
DB.execute('''CREATE TABLE IF NOT EXISTS items
             (user_id INT,
              item_id TEXT UNIQUE,
              interval INT,
              last_bump INT)''')

# Инициализация бота
logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Состояния
class Form(StatesGroup):
    set_interval = State()
    change_interval = State()
    delete_item = State()

# Клавиатуры
def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=" Добавить"), KeyboardButton(text=" Список")],
            [KeyboardButton(text="⚙ Интервал"), KeyboardButton(text="❌ Удалить")]
        ],
        resize_keyboard=True
    )

def cancel_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=" Назад")]],
        resize_keyboard=True
    )

def confirm_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Да"), KeyboardButton(text="❌ Нет")]
        ],
        resize_keyboard=True
    )

def items_keyboard(items, prefix):
    keyboard = []
    for item in items:
        keyboard.append([KeyboardButton(text=f"{prefix} {item[0]}")])
    keyboard.append([KeyboardButton(text=" Назад")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

# Проверка пользователя
async def check_user(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID

# Проверка возможности поднятия
async def can_bump(item_id: str):
    url = f"https://api.lzt.market/{item_id}/bump"
    headers = {"Authorization": f"Bearer {LZT_API_KEY}"}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers) as response:
                data = await response.json()
                if response.status == 200:
                    return True, "OK"
                return False, data.get('errors', ['Неизвестная ошибка'])[0]
        except Exception as e:
            return False, str(e)

# Автоподнятие
async def scheduler():
    while True:
        await asyncio.sleep(60)
        cursor = DB.execute("SELECT * FROM items")
        for row in cursor.fetchall():
            user_id, item_id, interval, last_bump = row
            if (time.time() - last_bump) >= interval * 3600:
                success, error = await can_bump(item_id)
                new_bump = int(time.time()) if success else last_bump
                DB.execute("UPDATE items SET last_bump = ? WHERE item_id = ?",
                          (new_bump, item_id))
                DB.commit()

# Обработчики
@dp.message(Command('start'))
@dp.message(F.text == " Назад")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if not await check_user(message.from_user.id):
        return
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.message(F.text == " Добавить")
async def cmd_add(message: types.Message):
    await message.answer("Отправьте ссылку на объявление:", reply_markup=cancel_keyboard())

@dp.message(F.text.startswith('https://lzt.market/'))
async def process_url(message: types.Message, state: FSMContext):
    try:
        path = urlparse(message.text).path.split('/')
        item_id = path[1]
        
        if not item_id.isdigit():
            raise ValueError

        cursor = DB.execute("SELECT 1 FROM items WHERE item_id = ?", (item_id,))
        if cursor.fetchone():
            await message.answer("⚠ Это объявление уже в списке!", reply_markup=main_menu())
            return

        success, error = await can_bump(item_id)
        if not success:
            await message.answer(f"❌ Невозможно добавить: {error}", reply_markup=main_menu())
            return

        await state.set_state(Form.set_interval)
        await state.update_data(item_id=item_id)
        await message.answer("✅ Аккаунт доступен! Введите интервал поднятия (часов, минимум 6):",
                           reply_markup=cancel_keyboard())
        
    except Exception as e:
        await message.answer("❌ Неверная ссылка!", reply_markup=main_menu())

@dp.message(Form.set_interval)
async def set_interval(message: types.Message, state: FSMContext):
    if message.text == " Назад":
        await state.clear()
        return await cmd_start(message, state)
    
    try:
        interval = max(6, int(message.text))
        data = await state.get_data()
        item_id = data['item_id']
        
        DB.execute("""INSERT OR REPLACE INTO items
                    (user_id, item_id, interval, last_bump)
                    VALUES (?, ?, ?, ?)""",
                (message.from_user.id, item_id, interval, int(time.time())))
        DB.commit()
        
        await message.answer(f"✅ Объявление {item_id} добавлено!", reply_markup=main_menu())
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Введите число!", reply_markup=cancel_keyboard())

@dp.message(F.text == " Список")
async def cmd_list(message: types.Message):
    if not await check_user(message.from_user.id):
        return
    
    cursor = DB.execute("SELECT item_id, interval, last_bump FROM items WHERE user_id = ?",
                       (message.from_user.id,))
    items = cursor.fetchall()
    
    if not items:
        return await message.answer(" Список объявлений пуст", reply_markup=main_menu())
    
    text = [" Ваши объявления:"]
    for item in items:
        last_bump = time.strftime('%d.%m.%Y %H:%M', time.localtime(item[2])) if item[2] > 0 else "никогда"
        text.append(f"▪ ID: {item[0]}\nИнтервал: {item[1]}ч\nПоследнее поднятие: {last_bump}")
    
    await message.answer("\n".join(text), reply_markup=main_menu())

@dp.message(F.text == "⚙ Интервал")
async def cmd_change_interval(message: types.Message, state: FSMContext):
    if not await check_user(message.from_user.id):
        return
    
    cursor = DB.execute("SELECT item_id, interval FROM items WHERE user_id = ?",
                      (message.from_user.id,))
    items = cursor.fetchall()
    
    if not items:
        return await message.answer("❌ Нет объявлений для изменения", reply_markup=main_menu())
    
    await message.answer("Выберите объявление:", reply_markup=items_keyboard(items, ""))
    await state.set_state(Form.change_interval)

@dp.message(Form.change_interval, F.text == " Назад")
async def cancel_change_interval(message: types.Message, state: FSMContext):
    await state.clear()
    await cmd_start(message, state)

@dp.message(Form.change_interval, F.text.startswith(""))
async def select_item(message: types.Message, state: FSMContext):
    try:
        item_id = message.text.split()[1]
        await state.update_data(item_id=item_id)
        await message.answer("Введите новый интервал:", reply_markup=cancel_keyboard())
        await state.set_state(Form.set_interval)
    except:
        await message.answer("❌ Ошибка выбора", reply_markup=main_menu())
        await state.clear()

@dp.message(F.text == "❌ Удалить")
async def cmd_delete(message: types.Message, state: FSMContext):
    if not await check_user(message.from_user.id):
        return
    
    cursor = DB.execute("SELECT item_id FROM items WHERE user_id = ?",
                      (message.from_user.id,))
    items = cursor.fetchall()
    
    if not items:
        return await message.answer("❌ Нет объявлений для удаления", reply_markup=main_menu())
    
    await message.answer("Выберите объявление:", reply_markup=items_keyboard(items, ""))
    await state.set_state(Form.delete_item)

@dp.message(Form.delete_item, F.text.startswith(""))
async def select_delete_item(message: types.Message, state: FSMContext):
    try:
        item_id = message.text.split()[1]
        await state.update_data(item_id=item_id)
        await message.answer(f"Удалить объявление {item_id}?", reply_markup=confirm_keyboard())
    except:
        await message.answer("❌ Ошибка выбора", reply_markup=main_menu())
        await state.clear()

@dp.message(Form.delete_item, F.text == "✅ Да")
async def confirm_delete(message: types.Message, state: FSMContext):
    data = await state.get_data()
    DB.execute("DELETE FROM items WHERE item_id = ? AND user_id = ?",
             (data['item_id'], message.from_user.id))
    DB.commit()
    await message.answer(f"✅ Объявление {data['item_id']} удалено!", reply_markup=main_menu())
    await state.clear()

@dp.message(Form.delete_item, F.text == "❌ Нет")
async def cancel_delete(message: types.Message, state: FSMContext):
    await state.clear()
    await cmd_start(message, state)

# Запуск
async def on_startup():
    asyncio.create_task(scheduler())

if __name__ == '__main__':
    dp.startup.register(on_startup)
    dp.run_polling(bot)
