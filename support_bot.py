import asyncio
import os
import asyncpg
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

load_dotenv()

SUPPORT_BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN")
SUPPORT_GROUP_ID  = int(os.getenv("SUPPORT_GROUP_ID", "0"))
DATABASE_URL      = os.getenv("DATABASE_URL")
WEBHOOK_HOST      = os.getenv("WEBHOOK_HOST", "")
SUPPORT_WEBHOOK_PATH = "/support_webhook"
SUPPORT_WEBHOOK_URL  = f"{WEBHOOK_HOST}{SUPPORT_WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

bot = Bot(token=SUPPORT_BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

pool: asyncpg.Pool | None = None

CATEGORIES = {
    "ads":     {"emoji": "💼", "label": "Реклама и сотрудничество"},
    "payment": {"emoji": "⭐", "label": "Вопрос по оплате"},
    "bot":     {"emoji": "🔮", "label": "Вопрос по работе бота"},
    "other":   {"emoji": "💬", "label": "Другое"},
}


# ── FSM ───────────────────────────────────────────────────────────────────────

class SupportFlow(StatesGroup):
    waiting_category = State()
    waiting_message  = State()


# ── БД ────────────────────────────────────────────────────────────────────────

async def init_support_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=5, ssl="require")
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                username    TEXT,
                first_name  TEXT,
                thread_id   INT NOT NULL,
                status      TEXT DEFAULT 'open',
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                closed_at   TIMESTAMP WITH TIME ZONE
            )
        """)
    print("Support DB готова")


async def get_active_ticket(user_id: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM support_tickets WHERE user_id = $1 AND status = 'open'",
            user_id
        )


async def create_ticket(user_id: int, username: str, first_name: str, thread_id: int, category: str):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO support_tickets (user_id, username, first_name, thread_id, status)
            VALUES ($1, $2, $3, $4, 'open')
        """, user_id, username, first_name, thread_id)


async def close_ticket_db(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE support_tickets
            SET status = 'closed', closed_at = NOW()
            WHERE user_id = $1 AND status = 'open'
        """, user_id)


async def get_ticket_by_thread(thread_id: int) -> asyncpg.Record | None:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM support_tickets WHERE thread_id = $1 AND status = 'open'",
            thread_id
        )


# ── Кнопки ────────────────────────────────────────────────────────────────────

def get_categories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💼 Реклама и сотрудничество", callback_data="cat_ads")],
        [InlineKeyboardButton(text="⭐ Вопрос по оплате",         callback_data="cat_payment")],
        [InlineKeyboardButton(text="🔮 Вопрос по работе бота",   callback_data="cat_bot")],
        [InlineKeyboardButton(text="💬 Другое",                   callback_data="cat_other")],
    ])


# ── Старт ─────────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(SupportFlow.waiting_category)
    await message.answer(
        "Добро пожаловать в поддержку RuneCast 🌌\n\n"
        "Выбери тему твоего обращения:",
        reply_markup=get_categories_keyboard()
    )


# ── Выбор категории ───────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("cat_"))
async def choose_category(callback: CallbackQuery, state: FSMContext):
    cat_key = callback.data.replace("cat_", "")
    if cat_key not in CATEGORIES:
        return
    cat = CATEGORIES[cat_key]
    await state.update_data(category=cat_key)
    await state.set_state(SupportFlow.waiting_message)
    await callback.message.edit_text(
        f"Выбрано: {cat['emoji']} {cat['label']}\n\n"
        f"Опиши свой вопрос — мы ответим как можно скорее."
    )
    try:
        await callback.answer()
    except Exception:
        pass


# ── Сообщение от пользователя ─────────────────────────────────────────────────

@dp.message(SupportFlow.waiting_message)
async def handle_user_message(message: Message, state: FSMContext):
    user_id    = message.from_user.id
    username   = message.from_user.username or "нет username"
    first_name = message.from_user.first_name or "Без имени"

    data    = await state.get_data()
    cat_key = data.get("category", "other")
    cat     = CATEGORIES[cat_key]

    ticket = await get_active_ticket(user_id)

    if not ticket:
        topic_name = f"{cat['emoji']} {first_name} · @{username} · {user_id}"
        topic      = await bot.create_forum_topic(chat_id=SUPPORT_GROUP_ID, name=topic_name)
        thread_id  = topic.message_thread_id
        await create_ticket(user_id, username, first_name, thread_id, cat_key)
        await bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text=(
                f"{cat['emoji']} <b>{cat['label']}</b>\n\n"
                f"👤 Имя: {first_name}\n"
                f"Username: @{username}\n"
                f"ID: <code>{user_id}</code>"
            ),
            parse_mode="HTML"
        )
    else:
        thread_id = ticket["thread_id"]

    await bot.forward_message(
        chat_id=SUPPORT_GROUP_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
        message_thread_id=thread_id
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Закрыть тикет", callback_data=f"close_{user_id}")
    ]])
    await bot.send_message(
        chat_id=SUPPORT_GROUP_ID,
        message_thread_id=thread_id,
        text="↑ сообщение от пользователя",
        reply_markup=keyboard
    )

    await message.answer("Сообщение получено ✓\nОтветим как можно скорее.")
    await state.clear()


# ── Повторное обращение ───────────────────────────────────────────────────────

@dp.message(F.chat.type == "private")
async def handle_repeat_message(message: Message, state: FSMContext):
    user_id = message.from_user.id
    ticket  = await get_active_ticket(user_id)

    if ticket:
        thread_id = ticket["thread_id"]
        await bot.forward_message(
            chat_id=SUPPORT_GROUP_ID,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
            message_thread_id=thread_id
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Закрыть тикет", callback_data=f"close_{user_id}")
        ]])
        await bot.send_message(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=thread_id,
            text="↑ дополнительное сообщение",
            reply_markup=keyboard
        )
        await message.answer("Сообщение добавлено к твоему обращению ✓")
    else:
        await state.set_state(SupportFlow.waiting_category)
        await message.answer(
            "Выбери тему твоего обращения:",
            reply_markup=get_categories_keyboard()
        )


# ── Ответ из группы → пользователю ───────────────────────────────────────────

@dp.message(F.chat.id == SUPPORT_GROUP_ID)
async def handle_group_message(message: Message):
    if not message.message_thread_id:
        return
    if message.from_user and message.from_user.is_bot:
        return
    ticket = await get_ticket_by_thread(message.message_thread_id)
    if not ticket:
        return
    await bot.send_message(
        chat_id=ticket["user_id"],
        text=f"💬 Ответ от поддержки:\n\n{message.text}"
    )


# ── Закрытие тикета ───────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("close_"))
async def close_ticket(callback: CallbackQuery):
    user_id = int(callback.data.replace("close_", ""))
    ticket  = await get_active_ticket(user_id)
    if ticket:
        await bot.close_forum_topic(
            chat_id=SUPPORT_GROUP_ID,
            message_thread_id=ticket["thread_id"]
        )
        await close_ticket_db(user_id)
    try:
        await callback.answer("Тикет закрыт ✅")
        await callback.message.edit_text("🔒 Тикет закрыт")
    except Exception:
        pass


# ── Webhook эндпоинт ──────────────────────────────────────────────────────────

async def handle_set_webhook(request: web.Request):
    token = request.headers.get("X-Secret", "")
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        return web.json_response({"ok": False}, status=403)
    await bot.delete_webhook(drop_pending_updates=True)
    result = await bot.set_webhook(SUPPORT_WEBHOOK_URL)
    return web.json_response({"ok": True, "webhook": SUPPORT_WEBHOOK_URL, "result": str(result)})


# ── Запуск ────────────────────────────────────────────────────────────────────
async def set_webhook_delayed():
    await asyncio.sleep(10)
    for attempt in range(5):
        try:
            await bot.set_webhook(SUPPORT_WEBHOOK_URL)
            print(f"Support webhook установлен: {SUPPORT_WEBHOOK_URL}")
            break
        except Exception as e:
            print(f"Webhook попытка {attempt + 1} не удалась: {e}")
            await asyncio.sleep(5)
    else:
        print("Support webhook не удалось установить после 5 попыток")
        
async def on_startup(app: web.Application):
    await init_support_db()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(set_webhook_delayed())
    print("Support bot запущен")


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()
    if pool:
        await pool.close()


def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/set_webhook", handle_set_webhook)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=SUPPORT_WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()