import aiohttp_cors
import asyncio
import os
import sys
import yaml
import hmac
import hashlib
import json
import random
from urllib.parse import parse_qsl, quote
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
    check_and_spend_coins, add_coins, give_channel_bonus,
    SPREAD_COST,
)

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
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
        "count": 1,
        "cost":  SPREAD_COST["single"],
    },
    "triple": {
        "name":  "🌿 Тень Иггдрасиль",
        "desc":  "3 руны — прошлое, настоящее, будущее",
        "count": 3,
        "cost":  SPREAD_COST["triple"],
    },
    "five": {
        "name":  "🌌 Посох Одина",
        "desc":  "5 рун — глубокий расклад",
        "count": 5,
        "cost":  SPREAD_COST["five"],
    },
}

# ── Пакеты монет ──────────────────────────────────────────────────────────────

PACKAGES = {
    "pack_10":  {"stars": 15,  "coins": 10,  "label": "10 монет — 15 ⭐"},
    "pack_30":  {"stars": 40,  "coins": 30,  "label": "30 монет — 40 ⭐"},
    "pack_100": {"stars": 120, "coins": 100, "label": "100 монет — 120 ⭐"},
}

client = OpenAI(api_key=OPENAI_API_KEY)

# ── Загрузка данных ───────────────────────────────────────────────────────────

def load_data():
    with open("Casting/runes.yaml", encoding="utf-8") as f:
        runes_data = yaml.safe_load(f)
    with open("Casting/prompts.yaml", encoding="utf-8") as f:
        prompts_data = yaml.safe_load(f)
    return runes_data["runes"], prompts_data["prompts"], prompts_data["system"]

RUNES, PROMPTS, SYSTEM_PROMPT = load_data()

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
            "name_ru":     rune["name_ru"],
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
        kwargs[f"rune{i}_name"]     = rune["name_ru"]
        kwargs[f"rune{i}_tags"]     = ", ".join(rune["tags"])
        kwargs[f"rune{i}_reversed"] = "да" if rune["is_reversed"] else "нет"
    return template.format(**kwargs)


def build_greeting(spread_type: str, name: str, runes: list[dict], interpretation: str) -> str:
    template = random.choice(PROMPTS["greetings"][spread_type])
    kwargs   = {"name": name, "interpretation": interpretation}
    for i, rune in enumerate(runes, 1):
        kwargs[f"rune{i}_name"] = rune["name_ru"]
    return template.format(**kwargs)


# ── Кнопки ────────────────────────────────────────────────────────────────────

def get_main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔮 Гадание")],
            [KeyboardButton(text="💰 Пополнить баланс")],
        ],
        resize_keyboard=True,
        persistent=True,
    )

def get_spread_keyboard(free_available: bool) -> InlineKeyboardMarkup:
    single_label = (
        f"{SPREADS['single']['name']} — бесплатно"
        if free_available
        else f"{SPREADS['single']['name']} — {SPREADS['single']['cost']} монета"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=single_label, callback_data="spread_single")],
        [InlineKeyboardButton(
            text=f"{SPREADS['triple']['name']} — {SPREADS['triple']['cost']} монеты",
            callback_data="spread_triple"
        )],
        [InlineKeyboardButton(
            text=f"{SPREADS['five']['name']} — {SPREADS['five']['cost']} монет",
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

def get_result_keyboard(spread_type: str) -> InlineKeyboardMarkup:
    cost = SPREADS[spread_type]["cost"]
    cost_label = (
        f"{cost} монета" if cost == 1
        else f"{cost} монеты" if cost in (2, 3, 4)
        else f"{cost} монет"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🎲 Перебросить руны ({cost_label})",
            callback_data=f"recast_{spread_type}"
        )],
        [InlineKeyboardButton(text="🔮 Новое гадание", callback_data="new_casting")],
    ])

def get_topup_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 монет — 10 ⭐",       callback_data="buy_pack_10")],
        [InlineKeyboardButton(text="25 монет — 50 ⭐",      callback_data="buy_pack_50")],
        [InlineKeyboardButton(text="100 монет — 180 ⭐",    callback_data="buy_pack_180")],
        [InlineKeyboardButton(text="500 монет — 800 ⭐ 🔥", callback_data="buy_pack_800")],
    ])

def get_no_coins_keyboard() -> InlineKeyboardMarkup:
    return get_topup_keyboard()

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
    if not message.from_user:
        return
    await get_or_create_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    await message.answer(
        "🎁 Подпишись на канал и получи 3 монеты бесплатно!",
        reply_markup=get_channel_keyboard()
    )

    balance_data   = await get_user_balance(message.from_user.id)
    free_available = balance_data["free_left"] > 0
    coins          = balance_data["coins_balance"]
    free_text      = "✅ бесплатное гадание доступно" if free_available else "❌ бесплатное гадание использовано"

    await message.answer(
        f"Добро пожаловать. Руны хранят древнюю мудрость.\n\n"
        f"💰 Монеты: {coins}\n"
        f"{free_text}\n\n"
        f"Опиши свою ситуацию и нажми 🔮 Гадание — оракул услышит тебя.",
        reply_markup=get_main_keyboard()
    )

    await bot.set_chat_menu_button(
        chat_id=message.chat.id,
        menu_button=MenuButtonWebApp(
            text="🌌 Оракул",
            web_app=WebAppInfo(url=f"{MINIAPP_URL}/?free={1 if free_available else 0}")
        )
    )

@dp.message(Command("info"))
async def cmd_info(message: Message):
    await message.answer(
        "Старший Футарк — 24 руны древних германских народов.\n"
        "Каждая несёт в себе архетипическую силу и смысл.",
        reply_markup=get_info_keyboard()
    )

@dp.message(F.text == "💰 Пополнить баланс")
async def ask_topup(message: Message):
    balance_data = await get_user_balance(message.from_user.id)
    coins = balance_data["coins_balance"]
    await message.answer(
        f"💰 Твой баланс: {coins} монет\n\n"
        f"Купи монеты — и руны откроют путь.\n"
        f"Выбери подходящий пакет:",
        reply_markup=get_topup_keyboard()
    )

@dp.callback_query(F.data.startswith("buy_pack_"))
async def handle_buy_pack(callback: CallbackQuery):
    if not callback.from_user:
        return
    pack_key = (callback.data or "").replace("buy_", "")
    pack = PACKAGES.get(pack_key)
    if not pack:
        await callback.answer("Пакет не найден.", show_alert=True)
        return

    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Монеты Одина",
        description=pack["label"],
        payload=f"{pack_key}:{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=pack["label"], amount=pack["stars"])],
    )

@dp.callback_query(F.data == "new_casting")
async def new_casting(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    user_states.pop(callback.from_user.id, None)
    await callback.message.answer(  # type: ignore
        "Опиши свою ситуацию или задай вопрос.\nРуны услышат тебя."
    )
    await callback.answer()

@dp.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
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
            "Спасибо за подписку! +3 монеты зачислены ✨",
            show_alert=True
        )
        await callback.message.delete()  # type: ignore
    else:
        await callback.answer("Бонус уже был получен ранее.", show_alert=True)
        await callback.message.delete()  # type: ignore


@dp.message(F.web_app_data)
async def handle_web_app_data(message: Message):
    if not message.from_user or not message.web_app_data:
        return
    try:
        data = json.loads(message.web_app_data.data)
    except Exception:
        return

    if data.get("action") != "spread":
        return

    spread_type = data.get("spread_type", "single")
    situation   = data.get("situation", "")

    if not situation:
        await message.answer("Вопрос не получен. Попробуй снова.")
        return

    user_id = message.from_user.id
    await get_or_create_user(
        user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    # spend_result = await check_and_spend_coins(user_id, spread_type)

    # if spend_result == "banned":
    #     return

    # if spend_result == "no_coins":
    #     await message.answer(
    #         "Недостаточно монет для этого расклада.\nПополни баланс и продолжи путь.",
    #         reply_markup=get_no_coins_keyboard()
    #     )
    #     return

    user_states[user_id] = {"situation": situation, "spread_type": spread_type}

    await _perform_casting(
        user_id=user_id,
        spread_type=spread_type,
        situation=situation,
        first_name=message.from_user.first_name or "странник",
        chat_id=message.chat.id,
    )


# ── Основной обработчик — сохраняем ситуацию ─────────────────────────────────

@dp.message()
async def handle_message(message: Message):
    if not message.from_user:
        return
    user_text = message.text
    if not user_text:
        return

    user_id = message.from_user.id
    await get_or_create_user(
        user_id,
        username=message.from_user.username,  # type: ignore
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
    if not callback.from_user or not callback.message:
        return
    spread_type = (callback.data or "").replace("spread_", "")
    user_id     = callback.from_user.id

    state = user_states.get(user_id)
    if not state:
        await callback.message.answer(  # type: ignore
            "Сначала опиши свою ситуацию."
        )
        await callback.answer()
        return

    # spend_result = await check_and_spend_coins(user_id, spread_type)

    # if spend_result == "banned":
    #     await callback.answer("Доступ ограничен.", show_alert=True)
    #     return

    # if spend_result == "no_coins":
    #     await callback.answer()
    #     await callback.message.answer(  # type: ignore
    #         "Недостаточно монет для этого расклада.\nПополни баланс и продолжи путь.",
    #         reply_markup=get_no_coins_keyboard()
    #     )
    #     return

    user_states[user_id]["spread_type"] = spread_type

    await callback.answer()
    await _perform_casting(
        user_id=user_id,
        spread_type=spread_type,
        situation=state["situation"],
        first_name=callback.from_user.first_name or "странник",
        chat_id=callback.message.chat.id,
    )


# ── Перебросить руны ──────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("recast_"))
async def handle_recast(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    spread_type = (callback.data or "").replace("recast_", "")
    user_id     = callback.from_user.id

    state = user_states.get(user_id)
    if not state or not state.get("situation"):
        await callback.message.answer(  # type: ignore
            "Сессия истекла. Опиши ситуацию заново."
        )
        await callback.answer()
        return

    # spend_result = await check_and_spend_coins(user_id, spread_type)

    # if spend_result == "banned":
    #     await callback.answer("Доступ ограничен.", show_alert=True)
    #     return

    # if spend_result == "no_coins":
    #     await callback.answer()
    #     await callback.message.answer(  # type: ignore
    #         "Недостаточно монет.\nПополни баланс и продолжи путь.",
    #         reply_markup=get_no_coins_keyboard()
    #     )
    #     return

    await callback.answer()
    await _perform_casting(
        user_id=user_id,
        spread_type=spread_type,
        situation=state["situation"],
        first_name=callback.from_user.first_name or "странник",
        chat_id=callback.message.chat.id,
    )


# ── Оплата ────────────────────────────────────────────────────────────────────

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    if not message.from_user or not message.successful_payment:
        return
    payload = message.successful_payment.invoice_payload
    try:
        pack_key, user_id_str = payload.split(":")
        user_id = int(user_id_str)
    except Exception:
        return

    pack = PACKAGES.get(pack_key)
    if not pack:
        return

    success = await add_coins(
        user_id=user_id,
        coins_amount=pack["coins"],
        stars_amount=message.successful_payment.total_amount,
        telegram_charge_id=message.successful_payment.telegram_payment_charge_id,
    )

    if success:
        await message.answer(
            f"✨ На счёт зачислено {pack['coins']} монет.\n"
            f"Руны ждут твоего вопроса."
        )


# ── Само гадание ──────────────────────────────────────────────────────────────

async def _perform_casting(
    user_id: int, spread_type: str, situation: str,
    first_name: str, chat_id: int,
):
    runes = cast_runes(SPREADS[spread_type]["count"])

    interpretation = "Руны не смогли открыться. Попробуй снова."
    try:
        await bot.send_chat_action(chat_id, "typing")
        prompt = build_prompt(spread_type, situation, runes)

        start_time = datetime.now()
        response   = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.9,
            max_completion_tokens=400,
        )
        latency_ms     = int((datetime.now() - start_time).total_seconds() * 1000)
        interpretation = response.choices[0].message.content.strip()  # type: ignore

        log_casting(
            user_id=user_id,
            spread_type=spread_type,
            stars=0,
            input_tokens=response.usage.prompt_tokens,  # type: ignore
            output_tokens=response.usage.completion_tokens,  # type: ignore
            latency_ms=latency_ms,
        )
    except Exception as e:
        print(f"ОШИБКА модели: {e}")

    greeting = build_greeting(spread_type, first_name, runes, interpretation)

    rune_list = " · ".join([
        f"{r['symbol']} {r['name_ru']}{'  🔄' if r['is_reversed'] else ''}"
        for r in runes
    ])

    if spread_type == "single":
        rune = runes[0]
        url = (
            f"{MINIAPP_URL}/casting_single.html"
            f"?rune={quote(rune['name_ru'])}"
            f"&reversed={'1' if rune['is_reversed'] else '0'}"
            f"&text={quote(greeting)}"
        )
        await bot.send_message(
            chat_id, rune_list,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚡ Открыть расклад", web_app=WebAppInfo(url=url))],
                [InlineKeyboardButton(text=f"🎲 Перебросить руны ({SPREADS['single']['cost']} монета)", callback_data="recast_single")],
                [InlineKeyboardButton(text="🔮 Новое гадание", callback_data="new_casting")],
            ])
        )

    elif spread_type == "triple":
        url = (
            f"{MINIAPP_URL}/casting_triple.html"
            f"?rune1={quote(runes[0]['name_ru'])}&reversed1={'1' if runes[0]['is_reversed'] else '0'}"
            f"&rune2={quote(runes[1]['name_ru'])}&reversed2={'1' if runes[1]['is_reversed'] else '0'}"
            f"&rune3={quote(runes[2]['name_ru'])}&reversed3={'1' if runes[2]['is_reversed'] else '0'}"
            f"&text={quote(greeting)}"
        )
        await bot.send_message(
            chat_id, rune_list,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌿 Открыть расклад", web_app=WebAppInfo(url=url))],
                [InlineKeyboardButton(text=f"🎲 Перебросить руны ({SPREADS['triple']['cost']} монеты)", callback_data="recast_triple")],
                [InlineKeyboardButton(text="🔮 Новое гадание", callback_data="new_casting")],
            ])
        )

    elif spread_type == "five":
        url = (
            f"{MINIAPP_URL}/casting_five.html"
            f"?rune1={quote(runes[0]['name_ru'])}&reversed1={'1' if runes[0]['is_reversed'] else '0'}"
            f"&rune2={quote(runes[1]['name_ru'])}&reversed2={'1' if runes[1]['is_reversed'] else '0'}"
            f"&rune3={quote(runes[2]['name_ru'])}&reversed3={'1' if runes[2]['is_reversed'] else '0'}"
            f"&rune4={quote(runes[3]['name_ru'])}&reversed4={'1' if runes[3]['is_reversed'] else '0'}"
            f"&rune5={quote(runes[4]['name_ru'])}&reversed5={'1' if runes[4]['is_reversed'] else '0'}"
            f"&text={quote(greeting)}"
        )
        await bot.send_message(
            chat_id, rune_list,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌌 Открыть расклад", web_app=WebAppInfo(url=url))],
                [InlineKeyboardButton(text=f"🎲 Перебросить руны ({SPREADS['five']['cost']} монет)", callback_data="recast_five")],
                [InlineKeyboardButton(text="🔮 Новое гадание", callback_data="new_casting")],
            ])
        )


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

async def handle_user_info(request: web.Request):
    """Баланс пользователя для Mini App."""
    init_data = request.rel_url.query.get("init_data", "")
    user_data = validate_init_data(init_data)
    if not user_data:
        return web.json_response({"ok": False, "error": "invalid init_data"})
    user_id = int(user_data.get("id", 0))
    if not user_id:
        return web.json_response({"ok": False, "error": "no user_id"})
    balance_data = await get_user_balance(user_id)
    return web.json_response({"ok": True, **balance_data})

async def handle_create_invoice(request: web.Request):
    """Создать invoice для покупки монет из Mini App."""
    data      = await request.json()
    pack_key  = data.get("pack_key")
    init_data = data.get("init_data", "")
    user_data = validate_init_data(init_data)
    if not user_data:
        return web.json_response({"ok": False, "error": "invalid init_data"})
    user_id = int(user_data.get("id", 0))
    if not user_id:
        return web.json_response({"ok": False, "error": "no user_id"})
    pack = PACKAGES.get(pack_key)
    if not pack:
        return web.json_response({"ok": False, "error": "unknown pack"})
    await bot.send_invoice(
        chat_id=user_id,
        title="Монеты Одина",
        description=pack["label"],
        payload=f"{pack_key}:{user_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=pack["label"], amount=pack["stars"])],
    )
    return web.json_response({"ok": True})

async def handle_set_webhook(request: web.Request):
    await bot.delete_webhook(drop_pending_updates=True)
    result = await bot.set_webhook(WEBHOOK_URL)
    return web.json_response({"ok": True, "webhook": WEBHOOK_URL, "result": str(result)})


# ── Запуск ────────────────────────────────────────────────────────────────────

async def on_startup(app: web.Application):
    await init_users_db()
    await init_castings_table()
    await bot.delete_webhook(drop_pending_updates=True)
    print("Бот запущен")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()
    await close_db()

def main_webhook():
    app = web.Application()
    app.on_startup.append(on_startup)  # type: ignore
    app.on_shutdown.append(on_shutdown)  # type: ignore
    app.router.add_get("/set_webhook", handle_set_webhook)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=False,
            expose_headers="*",
            allow_headers="*",
            allow_methods=["POST", "OPTIONS", "GET"],
        )
    })

    resource_info = cors.add(app.router.add_resource("/user_info"))
    cors.add(resource_info.add_route("GET", handle_user_info))

    resource_invoice = cors.add(app.router.add_resource("/create_invoice"))
    cors.add(resource_invoice.add_route("POST", handle_create_invoice))

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main_webhook()