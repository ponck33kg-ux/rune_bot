import aiohttp_cors
import asyncio
import os
import yaml
import hmac
import hashlib
import json
import random
from urllib.parse import parse_qsl
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton,
    PreCheckoutQuery, LabeledPrice, MenuButtonWebApp
)
from aiogram.filters import CommandStart, Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from analytics import log_casting
from analytics_db import init_castings_table
from database import (
    init_db as init_users_db, close_db,
    get_user_balance, get_or_create_user,
    check_and_spend_message, add_messages, give_channel_bonus
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_HOST   = os.getenv("WEBHOOK_HOST", "https://your-app.up.railway.app")
WEBHOOK_PATH   = "/webhook"
WEBHOOK_URL    = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT           = int(os.getenv("PORT", 8080))

MINIAPP_URL      = "https://ponck33kg-ux.github.io/rune_mini_app"
CHANNEL_USERNAME = "@your_rune_channel"

# ── Гадания ───────────────────────────────────────────────────────────────────

SPREADS = {
    "single": {
        "name":  "⚡ Искра Футарка",
        "desc":  "1 руна — голос судьбы",
        "stars": 2,
        "count": 1,
    },
    "triple": {
        "name":  "🌿 Взор Норн",
        "desc":  "3 руны — прошлое, настоящее, будущее",
        "stars": 6,
        "count": 3,
    },
    "five": {
        "name":  "🌌 Посох Одина",
        "desc":  "5 рун — глубокий расклад",
        "stars": 10,
        "count": 5,
    },
}

client = OpenAI(api_key=OPENAI_API_KEY)

# ── Загрузка данных ───────────────────────────────────────────────────────────

def load_data():
    with open("casting/runes.yaml", encoding="utf-8") as f:
        runes_data = yaml.safe_load(f)
    with open("casting/prompts.yaml", encoding="utf-8") as f:
        prompts_data = yaml.safe_load(f)
    return runes_data["runes"], prompts_data["prompts"]

RUNES, PROMPTS = load_data()

bot = Bot(token=TELEGRAM_TOKEN)
dp  = Dispatcher()

# ── In-memory состояние пользователей ─────────────────────────────────────────
user_states: dict[int, dict] = {}


# ── Логика рун ────────────────────────────────────────────────────────────────

def cast_runes(count: int) -> list[dict]:
    keys   = random.sample(list(RUNES.keys()), count)
    result = []
    for key in keys:
        rune        = RUNES[key]
        is_reversed = random.choice([True, False])
        result.append({
            "key":         key,
            "name":        rune["name"],
            "symbol":      rune["symbol"],
            "image":       rune["image"],
            "tags":        rune["tags_reversed"] if is_reversed else rune["tags"],
            "is_reversed": is_reversed,
        })
    return result


def build_prompt(spread_type: str, situation: str, runes: list[dict]) -> str:
    template = PROMPTS[spread_type]
    kwargs   = {"situation": situation}
    for i, rune in enumerate(runes, 1):
        kwargs[f"rune{i}_name"]     = rune["name"]
        kwargs[f"rune{i}_tags"]     = ", ".join(rune["tags"])
        kwargs[f"rune{i}_reversed"] = "да" if rune["is_reversed"] else "нет"
    return template.format(**kwargs)


def build_greeting(spread_type: str, name: str, runes: list[dict], interpretation: str) -> str:
    template = random.choice(PROMPTS["greetings"][spread_type])
    kwargs   = {"name": name, "interpretation": interpretation}
    for i, rune in enumerate(runes, 1):
        kwargs[f"rune{i}_name"] = rune["name"]
    return template.format(**kwargs)


# ── Кнопки ────────────────────────────────────────────────────────────────────

def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔮 Гадание")]],
        resize_keyboard=True, persistent=True
    )

def get_spread_keyboard(free_available: bool) -> InlineKeyboardMarkup:
    single_label = (
        f"{SPREADS['single']['name']} — бесплатно"
        if free_available
        else f"{SPREADS['single']['name']} — {SPREADS['single']['stars']} ⭐"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=single_label, callback_data="spread_single")],
        [InlineKeyboardButton(
            text=f"{SPREADS['triple']['name']} — {SPREADS['triple']['stars']} ⭐",
            callback_data="spread_triple"
        )],
        [InlineKeyboardButton(
            text=f"{SPREADS['five']['name']} — {SPREADS['five']['stars']} ⭐",
            callback_data="spread_five"
        )],
    ])

def get_channel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Подписаться на канал",
            url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
        )],
        [InlineKeyboardButton(text="✅ Я подписался!", callback_data="check_subscription")],
    ])

def get_again_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔮 Новое гадание", callback_data="new_casting")],
    ])

def get_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📖 Все руны",
            web_app=WebAppInfo(url=f"{MINIAPP_URL}/runes.html")
        )],
    ])


# ── Команды ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await get_or_create_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    await message.answer(
        "🎁 Подпишись на канал и получи первое гадание бесплатно!",
        reply_markup=get_channel_keyboard()
    )
    await message.answer(
        "Добро пожаловать. Руны хранят древнюю мудрость.\n\n"
        "Опиши свою ситуацию и нажми 🔮 Гадание — оракул услышит тебя.",
        reply_markup=get_main_keyboard()
    )
    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="🌌 Оракул",
            web_app=WebAppInfo(url=MINIAPP_URL)
        )
    )

@dp.message(Command("info"))
async def cmd_info(message: Message):
    await message.answer(
        "Старший Футарк — 24 руны древних германских народов.\n"
        "Каждая несёт в себе архетипическую силу и смысл.",
        reply_markup=get_info_keyboard()
    )

@dp.message(F.text == "🔮 Гадание")
async def ask_situation(message: Message):
    await message.answer(
        "Опиши свою ситуацию или задай вопрос.\nРуны услышат тебя."
    )

@dp.callback_query(F.data == "new_casting")
async def new_casting(callback: CallbackQuery):
    user_states.pop(callback.from_user.id, None)
    await callback.message.answer(
        "Опиши свою ситуацию или задай вопрос.\nРуны услышат тебя."
    )
    await callback.answer()

@dp.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        is_subscribed = False

    if not is_subscribed:
        await callback.answer("Ты ещё не подписался на канал 🌑", show_alert=True)
        return

    given = await give_channel_bonus(user_id)
    if given:
        await callback.answer(
            "Спасибо за подписку! Первое гадание — бесплатно ✨",
            show_alert=True
        )
        await callback.message.delete()
    else:
        await callback.answer("Бонус уже был получен ранее.", show_alert=True)
        await callback.message.delete()


# ── Основной обработчик — сохраняем ситуацию ─────────────────────────────────

@dp.message()
async def handle_message(message: Message):
    user_text = message.text
    if not user_text:
        return

    user_id = message.from_user.id
    await get_or_create_user(
        user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    balance_data   = await get_user_balance(user_id)
    free_available = balance_data["free_left"] > 0

    user_states[user_id] = {"situation": user_text}

    await message.answer(
        "Руны готовы. Выбери расклад:",
        reply_markup=get_spread_keyboard(free_available)
    )


# ── Выбор расклада ────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("spread_"))
async def handle_spread(callback: CallbackQuery):
    spread_type = callback.data.replace("spread_", "")
    user_id     = callback.from_user.id

    state = user_states.get(user_id)
    if not state:
        await callback.message.answer("Сначала опиши свою ситуацию.")
        await callback.answer()
        return

    balance_data   = await get_user_balance(user_id)
    free_available = balance_data["free_left"] > 0

    # Однорунное бесплатно если есть лимит
    if spread_type == "single" and free_available:
        spend_result = await check_and_spend_message(user_id)
        if spend_result not in ("banned", "no_messages"):
            await callback.answer()
            await _perform_casting(
                user_id=user_id,
                spread_type="single",
                situation=state["situation"],
                first_name=callback.from_user.first_name or "странник",
                chat_id=callback.message.chat.id,
                stars=0,
                charge_id=None,
            )
        return

    # Платное — отправляем invoice
    spread = SPREADS[spread_type]
    await bot.send_invoice(
        chat_id=user_id,
        title=spread["name"],
        description=spread["desc"],
        payload=f"casting_{spread_type}:{user_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=spread["name"], amount=spread["stars"])],
    )
    await callback.answer()


# ── Оплата ────────────────────────────────────────────────────────────────────

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    try:
        type_and_key, user_id_str = payload.split(":")
        spread_type = type_and_key.replace("casting_", "")
        user_id     = int(user_id_str)
    except Exception:
        return

    state = user_states.get(user_id)
    if not state:
        await message.answer("Ситуация не найдена. Напиши её заново и выбери расклад.")
        return

    await add_messages(
        user_id=user_id,
        messages_amount=1,
        stars_amount=message.successful_payment.total_amount,
        telegram_charge_id=message.successful_payment.telegram_payment_charge_id,
    )

    await _perform_casting(
        user_id=user_id,
        spread_type=spread_type,
        situation=state["situation"],
        first_name=message.from_user.first_name or "странник",
        chat_id=message.chat.id,
        stars=message.successful_payment.total_amount,
        charge_id=message.successful_payment.telegram_payment_charge_id,
    )


# ── Само гадание ──────────────────────────────────────────────────────────────

async def _perform_casting(
    user_id: int, spread_type: str, situation: str,
    first_name: str, chat_id: int,
    stars: int, charge_id: str | None
):
    runes = cast_runes(SPREADS[spread_type]["count"])

    await bot.send_message(
        chat_id,
        "Руны брошены. Расклад открывается...",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="🔮 Открыть расклад",
                web_app=WebAppInfo(url=f"{MINIAPP_URL}/casting.html")
            )
        ]])
    )

    interpretation = "Руны не смогли открыться. Попробуй снова."
    try:
        await bot.send_chat_action(chat_id, "typing")
        prompt = build_prompt(spread_type, situation, runes)

        start_time = datetime.now()
        response   = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_completion_tokens=400,
        )
        latency_ms     = int((datetime.now() - start_time).total_seconds() * 1000)
        interpretation = response.choices[0].message.content.strip()

        log_casting(
            user_id=user_id,
            spread_type=spread_type,
            stars=stars,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            latency_ms=latency_ms,
        )
    except Exception as e:
        print(f"ОШИБКА модели: {e}")

    greeting = build_greeting(spread_type, first_name, runes, interpretation)

    for rune in runes:
        try:
            await bot.send_photo(
                chat_id,
                photo=f"{MINIAPP_URL}/stones/{rune['image']}",
                caption=f"{'🔄 ' if rune['is_reversed'] else ''}{rune['name']}"
            )
        except Exception as e:
            print(f"Ошибка отправки фото {rune['image']}: {e}")

    await bot.send_message(chat_id, greeting, reply_markup=get_again_keyboard())
    user_states.pop(user_id, None)


# ── HTTP эндпоинты ────────────────────────────────────────────────────────────

def validate_init_data(init_data: str) -> dict | None:
    try:
        parsed     = dict(parse_qsl(init_data, strict_parsing=True))
        hash_value = parsed.pop("hash", None)
        if not hash_value:
            return None
        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key    = hmac.new(b"WebAppData", TELEGRAM_TOKEN.encode(), hashlib.sha256).digest()
        computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed_hash, hash_value):
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

async def handle_set_webhook(request: web.Request):
    await bot.delete_webhook(drop_pending_updates=True)
    result = await bot.set_webhook(WEBHOOK_URL)
    return web.json_response({"ok": True, "webhook": WEBHOOK_URL, "result": str(result)})


# ── Запуск ────────────────────────────────────────────────────────────────────

async def on_startup(app: web.Application):
    await init_users_db()
    await init_castings_table()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL)
    print(f"Webhook: {WEBHOOK_URL}")
    print("Рунный бот запущен")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()
    await close_db()

def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/set_webhook", handle_set_webhook)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=False,
            expose_headers="*",
            allow_headers="*",
            allow_methods=["POST", "OPTIONS", "GET"],
        )
    })

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()