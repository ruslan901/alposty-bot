import asyncio
import logging
import time
from datetime import datetime, timedelta
import os
import aiohttp
import json
from dotenv import load_dotenv
import aiosqlite
import uuid
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, LabeledPrice, PreCheckoutQuery, \
    SuccessfulPayment
from aiogram.filters import CommandStart, Command
import uvicorn
from fastapi import FastAPI

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ТВОЙ ID - БЕЗ ЛИМИТОВ ДЛЯ ТЕСТИРОВАНИЯ
MY_TEST_USER_ID = int(os.getenv('MY_TEST_USER_ID', '0'))  # 0 = выкл

BOT_TOKEN = os.getenv('BOT_TOKEN')
SBER_AUTH_KEY = os.getenv('SBER_AUTH_KEY')
SBER_SCOPE = "GIGACHAT_API_PERS"
DB_PATH = 'users.db'

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "GigaChat Bot @my_alpost_bot Live 24/7!"}

@app.get("/health")
async def health():
    return {"status": "healthy", "bot": "@my_alpost_bot"}



class UserState(StatesGroup):
    waiting_input = State()


class GigaChatAuth:
    def __init__(self):
        self.access_token = None
        self.expires_at = 0

    async def get_token(self) -> str:
        if self.access_token and time.time() < self.expires_at:
            return self.access_token

        rq_uid = str(uuid.uuid4())
        url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'RqUID': rq_uid,
            'Authorization': f'Basic {SBER_AUTH_KEY}'
        }
        data = {'scope': SBER_SCOPE, 'grant_type': 'client_credentials'}

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, data=data) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        self.access_token = result['access_token']
                        self.expires_at = time.time() + 1700
                        print(f"✅ GigaChat токен обновлен")
                        return self.access_token
        except Exception as e:
            print(f"❌ GigaChat auth error: {e}")
        return ""


giga_auth = GigaChatAuth()


# 🔥 GIGA CHAT
async def giga_chat_request(prompt: str, service_type: str = "content") -> str:
    token = await giga_auth.get_token()
    if not token:
        return "Ошибка авторизации GigaChat. Проверьте SBER_AUTH_KEY"

    url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    if service_type == "posts":
        system_prompt = """Ты профессиональный контент-маркетолог. Пиши продающие посты для соцсетей:
- Эмоциональный язык + эмодзи
- 200-300 слов максимум
- Призыв к действию в конце
- Живой текст"""
    elif service_type == "law":
        system_prompt = """Ты профессиональный юрист РФ. Отвечай:
- По законам РФ с номерами статей
- Практические советы
- Четко структурировано"""
    else:
        system_prompt = "Ты полезный ассистент. Отвечай четко."

    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'RqUID': str(uuid.uuid4())
    }

    payload = {
        "model": "GigaChat-Pro",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Создай: {prompt}"}
        ],
        "temperature": 0.7,
        "max_tokens": 1500
    }

    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    response_text = result['choices'][0]['message']['content']
                    return response_text[:3950] if len(response_text) > 3950 else response_text
                else:
                    error_text = await resp.text()
                    print(f"❌ GigaChat error {resp.status}: {error_text}")
                    return f"Ошибка GigaChat: {resp.status}"
    except Exception as e:
        print(f"❌ GigaChat request error: {e}")
        return "Ошибка сети. Попробуйте позже."


# ✅ ИСПРАВЛЕННАЯ БАЗА ДАННЫХ - БЕЗ PRAGMA
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Создаем таблицу если нет
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                posts_free INTEGER DEFAULT 3,
                law_free INTEGER DEFAULT 3,
                last_reset TEXT,
                stars_purchased INTEGER DEFAULT 0,
                stars_end_date TEXT DEFAULT NULL
            )
        ''')
        await db.commit()

        # Добавляем недостающие колонки через ALTER (безопасно)
        try:
            await db.execute('ALTER TABLE users ADD COLUMN posts_free INTEGER DEFAULT 3')
            await db.commit()
        except aiosqlite.OperationalError:
            pass  # Колонка уже есть

        try:
            await db.execute('ALTER TABLE users ADD COLUMN law_free INTEGER DEFAULT 3')
            await db.commit()
        except aiosqlite.OperationalError:
            pass

        try:
            await db.execute('ALTER TABLE users ADD COLUMN stars_end_date TEXT DEFAULT NULL')
            await db.commit()
        except aiosqlite.OperationalError:
            pass

        try:
            await db.execute('ALTER TABLE users ADD COLUMN last_reset TEXT')
            await db.commit()
        except aiosqlite.OperationalError:
            pass

    print("✅ База данных готова")


async def check_limit(user_id: int, service: str) -> tuple[bool, str]:
    if user_id == MY_TEST_USER_ID:
        return True, "🔥 ТЕСТЕР: БЕЗЛИМИТ"

    today = datetime.now().strftime('%Y-%m-%d')
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT posts_free, law_free, last_reset, stars_end_date FROM users WHERE user_id=?',
            (user_id,))
        row = await cursor.fetchone()

        if not row:
            await db.execute(
                'INSERT INTO users (user_id, posts_free, law_free, last_reset) VALUES (?, 3, 3, ?)',
                (user_id, today))
            await db.commit()
            return True, f"✅ {service}: 3/3 бесплатно"

        posts_free, law_free, last_reset, stars_end_date = row if row[0] is not None else (3, 3, today, None)

        if stars_end_date and stars_end_date > today:
            remaining_days = (datetime.strptime(stars_end_date, '%Y-%m-%d') - datetime.now()).days + 1
            return True, f"⭐ Stars Безлимит: {remaining_days} дней"

        if last_reset != today:
            await db.execute('UPDATE users SET posts_free=3, law_free=3, last_reset=? WHERE user_id=?',
                             (today, user_id))
            await db.commit()
            return True, f"✅ {service}: 3/3 бесплатно"

        posts_free = posts_free or 0
        law_free = law_free or 0

        if service == "posts" and posts_free > 0:
            return True, f"✅ Посты: {posts_free - 1}/3 бесплатно"
        if service == "law" and law_free > 0:
            return True, f"✅ Юрист: {law_free - 1}/3 бесплатно"

        return False, f"❌ {service}: лимит исчерпан"


async def use_limit(user_id: int, service: str):
    if user_id == MY_TEST_USER_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        if service == "posts":
            await db.execute('UPDATE users SET posts_free = MAX(0, COALESCE(posts_free, 3) - 1) WHERE user_id = ?',
                             (user_id,))
        else:
            await db.execute('UPDATE users SET law_free = GREATEST(0, COALESCE(law_free, 3) - 1) WHERE user_id = ?',
                             (user_id,))
        await db.commit()


# ✅ ОБРАБОТЧИК ТЕКСТА
@dp.message(UserState.waiting_input)
async def process_user_input(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    service_type = data.get('service_type', 'posts')

    can_use, status = await check_limit(user_id, service_type)
    if not can_use:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💎 Купить Stars", callback_data="tariffs")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        await message.answer(f"❌ Лимит исчерпан!\n\n{status}", reply_markup=kb, parse_mode="Markdown")
        await state.clear()
        return

    await message.answer("🤖 GigaChat думает... ⏳")
    await use_limit(user_id, service_type)

    response = await giga_chat_request(message.text, service_type)

    kb_back = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить Stars", callback_data="tariffs")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

    await message.answer(f"✅ ВОТ ВАШ РЕЗУЛЬТАТ:\n\n{response}\n\n⚠️ Это не юридическая консультация",
                         reply_markup=kb_back)
    await state.clear()


# 🔥 ГЛАВНОЕ МЕНЮ
@dp.message(CommandStart())
@dp.message(Command("start"))
async def welcome_full_screen(message: types.Message):
    print(f"🎉 ПРИВЕТСТВИЕ ДЛЯ ID: {message.from_user.id}")

    kb_main = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Создать пост", callback_data="service_posts")],
        [InlineKeyboardButton(text="⚖️ Юридическая помощь", callback_data="service_law")],
        [InlineKeyboardButton(text="💎 Тарифы Stars", callback_data="tariffs")],
        [InlineKeyboardButton(text="📊 Мой баланс", callback_data="balance")]
    ])

    await message.answer("""
🎉 GIGA CHAT РАБОТАЕТ! 🎉

✍️ КОНТЕНТ - продающие посты
⚖️ ЮРИСТ - законы РФ + шаблоны

🎁 3 БЕСПЛАТНЫХ ЗАПРОСА/ДЕНЬ

💎 Stars: 1д=150⭐ 7д=250⭐ 30д=500⭐
    """, reply_markup=kb_main, parse_mode="Markdown")

    if message.from_user.id == MY_TEST_USER_ID:
        await asyncio.sleep(0.5)
        await message.answer("🔥 ТЕСТЕР ✓ БЕЗЛИМИТ GigaChat РАБОТАЕТ!", parse_mode="Markdown")


# 📝 МЕНЮ ПОСТОВ
@dp.callback_query(F.data == "service_posts")
async def content_menu(callback: CallbackQuery):
    can_use, status = await check_limit(callback.from_user.id, "posts")

    kb_content = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Продвижение услуг", callback_data="post_promo")],
        [InlineKeyboardButton(text="🛒 Продажа товаров", callback_data="post_sales")],
        [InlineKeyboardButton(text="📚 Курсы", callback_data="post_edu")],
        [InlineKeyboardButton(text="✨ Любой пост", callback_data="post_free")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    ])

    await callback.message.edit_text(
        f"✍️ ГЕНЕРАТОР ПОСТОВ\n\n📊 {status}\n\n"
        f"ПРИМЕР: iPhone 15 Барнаул 80к\n\n"
        f"🚀 Выберите тип поста 👇",
        reply_markup=kb_content, parse_mode="Markdown"
    )
    await callback.answer()


# ⚖️ МЕНЮ ЮРИСТА
@dp.callback_query(F.data == "service_law")
async def lawyer_menu(callback: CallbackQuery):
    can_use, status = await check_limit(callback.from_user.id, "law")

    kb_lawyer = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="law_question")],
        [InlineKeyboardButton(text="📄 Шаблон", callback_data="law_template")],
        [InlineKeyboardButton(text="🏛️ Иск в суд", callback_data="law_court")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    ])

    await callback.message.edit_text(
        f"⚖️ ЮРИСТ РФ\n\n📊 {status}\n\n"
        f"ПРИМЕР: Как уволить по ТК РФ\n\n"
        f"❓ Выберите услугу 👇",
        reply_markup=kb_lawyer, parse_mode="Markdown"
    )
    await callback.answer()


# ✅ ОБРАБОТЧИКИ С КНОПКАМИ НАЗАД
@dp.callback_query(F.data == "post_promo")
async def post_promo_handler(callback: CallbackQuery, state: FSMContext):
    kb_wait = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "🚀 ПРОДВИЖЕНИЕ УСЛУГ\n\n"
        "ПРИМЕР: Ремонт iPhone Барнаул от 1000₽\n\n"
        "📝 Напишите описание 👇",
        reply_markup=kb_wait,
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_input)
    await state.update_data(service_type="posts")
    await callback.answer()


@dp.callback_query(F.data == "post_sales")
async def post_sales_handler(callback: CallbackQuery, state: FSMContext):
    kb_wait = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "🛒 ПРОДАЖА ТОВАРОВ\n\n"
        "ПРИМЕР: iPhone 15 новый 80к Барнаул\n\n"
        "📝 Напишите про товар 👇",
        reply_markup=kb_wait,
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_input)
    await state.update_data(service_type="posts")
    await callback.answer()


@dp.callback_query(F.data == "post_edu")
async def post_edu_handler(callback: CallbackQuery, state: FSMContext):
    kb_wait = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "📚 КУРСЫ\n\n"
        "ПРИМЕР: Курс Python Барнаул 15к\n\n"
        "📝 Опишите курс 👇",
        reply_markup=kb_wait,
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_input)
    await state.update_data(service_type="posts")
    await callback.answer()


@dp.callback_query(F.data == "post_free")
async def post_free_handler(callback: CallbackQuery, state: FSMContext):
    kb_wait = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "✨ ЛЮБОЙ ПОСТ\n\n"
        "ПРИМЕР: Пост про автосервис скидки\n\n"
        "📝 Напишите ТЗ 👇",
        reply_markup=kb_wait,
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_input)
    await state.update_data(service_type="posts")
    await callback.answer()


@dp.callback_query(F.data == "law_question")
async def law_question_handler(callback: CallbackQuery, state: FSMContext):
    kb_wait = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "❓ ВОПРОС ЮРИСТУ\n\n"
        "ПРИМЕР: Уволить за опоздание?\n\n"
        "💬 Задайте вопрос 👇",
        reply_markup=kb_wait,
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_input)
    await state.update_data(service_type="law")
    await callback.answer()


@dp.callback_query(F.data == "law_template")
async def law_template_handler(callback: CallbackQuery, state: FSMContext):
    kb_wait = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "📄 ШАБЛОН\n\n"
        "ПРИМЕР: Жалоба в труд инспекцию\n\n"
        "📋 Какой документ? 👇",
        reply_markup=kb_wait,
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_input)
    await state.update_data(service_type="law")
    await callback.answer()


@dp.callback_query(F.data == "law_court")
async def law_court_handler(callback: CallbackQuery, state: FSMContext):
    kb_wait = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(
        "🏛️ ИСК В СУД\n\n"
        "ПРИМЕР: Иск за зарплату\n\n"
        "⚖️ Опишите ситуацию 👇",
        reply_markup=kb_wait,
        parse_mode="Markdown"
    )
    await state.set_state(UserState.waiting_input)
    await state.update_data(service_type="law")
    await callback.answer()


# ✅ ИСПРАВЛЕННЫЙ БАЛАНС
@dp.callback_query(F.data == "balance")
async def balance_menu(callback: CallbackQuery):
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute('''
                SELECT COALESCE(posts_free, 3), COALESCE(law_free, 3), stars_end_date 
                FROM users WHERE user_id=?
            ''', (callback.from_user.id,))
            row = await cursor.fetchone()

            if row:
                posts, law, stars_date = row
                stars = "0 дней"
                if stars_date and stars_date > today:
                    stars = "БЕЗЛИМИТ"
                balance_text = f"Посты: {posts}/3\nЮрист: {law}/3\nStars: {stars}"
            else:
                balance_text = "Посты: 3/3\nЮрист: 3/3\nStars: 0 дней"
    except:
        balance_text = "Посты: 3/3\nЮрист: 3/3\nStars: 0 дней"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить Stars", callback_data="tariffs")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text(f"📊 БАЛАНС\n\n{balance_text}", reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# Остальные обработчики
@dp.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery):
    kb_main = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Создать пост", callback_data="service_posts")],
        [InlineKeyboardButton(text="⚖️ Юридическая помощь", callback_data="service_law")],
        [InlineKeyboardButton(text="💎 Тарифы Stars", callback_data="tariffs")],
        [InlineKeyboardButton(text="📊 Мой баланс", callback_data="balance")]
    ])
    await callback.message.edit_text("🏠 ГЛАВНОЕ МЕНЮ", reply_markup=kb_main, parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "tariffs")
async def tariffs_menu(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔸 1 день — 150 ⭐", callback_data="buy_1day")],
        [InlineKeyboardButton(text="🔸 7 дней — 250 ⭐", callback_data="buy_7day")],
        [InlineKeyboardButton(text="🔸 30 дней — 500 ⭐", callback_data="buy_30day")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text("💎 ТАРИФЫ Stars\n\n🔸 1д=150⭐\n🔸 7д=250⭐\n🔸 30д=500⭐",
                                     reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# Stars оплата
@dp.callback_query(F.data.startswith("buy_"))
async def buy_stars(callback: CallbackQuery):
    days_map = {"buy_1day": 1, "buy_7day": 7, "buy_30day": 30}
    amount_map = {"buy_1day": 150, "buy_7day": 250, "buy_30day": 500}

    for key in days_map:
        if key in callback.data:
            prices = [LabeledPrice(label=f"⭐ {days_map[key]} дней", amount=amount_map[key])]
            await callback.message.answer_invoice(
                title=f"🔥 БЕЗЛИМИТ {days_map[key]} ДНЕЙ",
                description="Посты + Юрист",
                payload=f"stars_{days_map[key]}days_{callback.from_user.id}",
                provider_token="", currency="XTR", prices=prices
            )
            break
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout_query(pre_checkout_q: PreCheckoutQuery):
    await pre_checkout_q.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payload = message.successful_payment.payload
    user_id = message.from_user.id
    days = 1 if "1days" in payload else 7 if "7days" in payload else 30
    end_date = (datetime.now() + timedelta(days=days)).strftime('%Y-%m-%d')

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('INSERT OR IGNORE INTO users (user_id) VALUES (?)', (user_id,))
        await db.execute('UPDATE users SET posts_free=999, law_free=999, stars_end_date=? WHERE user_id=?',
                         (end_date, user_id))
        await db.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Новый запрос", callback_data="main_menu")],
        [InlineKeyboardButton(text="📊 Баланс", callback_data="balance")]
    ])
    await message.answer(f"🎉 БЕЗЛИМИТ {days} ДНЕЙ!\n📅 До: {end_date}", reply_markup=kb, parse_mode="Markdown")


@dp.callback_query()
async def unknown_callback(callback: CallbackQuery):
    await callback.answer("❌ Выберите кнопку выше 👆", show_alert=True)


async def main():
    await init_db()
    print("🚀 GigaChat Бот запущен!")

    # Render Free: FastAPI + Aiogram Webhook
    port = int(os.getenv("PORT", 10000))
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == '__main__':
    asyncio.run(main())





















