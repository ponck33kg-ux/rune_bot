import aiohttp_cors
import asyncio
import os
import sys
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
    check_coins, spend_coins, add_coins, give_channel_bonus, has_channel_bonus,
    log_visit, update_user_geo,
    track_referral_click, track_referral_conversion,
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
CHANNEL_USERNAME = "@runecast"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET") or ""
if not WEBHOOK_SECRET:
    print("ПРЕДУПРЕЖДЕНИЕ: WEBHOOK_SECRET не задан — /set_webhook и /webhook не защищены!")
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
    "pack_10":  {"stars": 10,  "coins": 5,   "label": "5 монет — 10 ⭐"},
    "pack_50":  {"stars": 50,  "coins": 25,  "label": "25 монет — 50 ⭐"},
    "pack_180": {"stars": 180, "coins": 100, "label": "100 монет — 180 ⭐"},
    "pack_800": {"stars": 800, "coins": 500, "label": "500 монет — 800 ⭐"},
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


# ── Команды ───────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    _, is_new = await get_or_create_user(
        user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    await log_visit(user_id, source="bot")
    await update_user_geo(user_id, language_code=message.from_user.language_code)

    # ── Реферальный трекинг ───────────────────────────────────────────────────
    text   = message.text or ""
    parts  = text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        ref_code = parts[1][4:].split("_")[0] + "_" + "_".join(parts[1][4:].split("_")[1:])
        # ref_CODE или ref_CODE_spread → берём всё после "ref_" до конца как код
        # Код в БД хранится как CAMPAIGN_MMDD, без spread_type
        raw      = parts[1][4:]           # CODE или CODE_spread
        segments = raw.split("_")
        # Последний сегмент — spread если он в ('single','triple','five'), иначе часть кода
        known_spreads = {"single", "triple", "five"}
        if segments[-1] in known_spreads:
            ref_code = "_".join(segments[:-1])
        else:
            ref_code = raw
        await track_referral_click(ref_code)
        if is_new:
            await track_referral_conversion(ref_code)
            
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        is_subscribed = False

    bonus_received = await has_channel_bonus(user_id)

    if not (is_subscribed and bonus_received):
        await message.answer(
            "🎁 Подпишись на канал и получи 10 монет бесплатно!",
            reply_markup=get_channel_keyboard()
        )

    balance_data   = await get_user_balance(user_id)
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


@dp.message(Command("support"))
async def cmd_support(message: Message):
    await message.answer(
        "По вопросам связанным с работой бота, "
        "предложениями сотрудничества и рекламой обращайтесь: @RuneSupport_Bot"
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
            "Спасибо за подписку! 10 монет зачислено ✨",
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

    check_result = await check_coins(user_id, spread_type)

    if check_result == "banned":
        return

    if check_result == "no_coins":
        await message.answer(
            "Недостаточно монет для этого расклада.\nПополни баланс и продолжи путь.",
            reply_markup=get_no_coins_keyboard()
        )
        return

    user_states[user_id] = {"situation": situation, "spread_type": spread_type}

    await _perform_casting(
        user_id=user_id,
        spread_type=spread_type,
        situation=situation,
        first_name=message.from_user.first_name or "странник",
        chat_id=message.chat.id,
        check_result=check_result,
    )

# ── Оплата ────────────────────────────────────────────────────────────────────

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    if not message.from_user or not message.successful_payment:
        print("successful_payment: нет from_user или successful_payment в message")
        return
    payload = message.successful_payment.invoice_payload
    print(f"successful_payment: получен payload={payload!r}")
    try:
        pack_key, user_id_str = payload.split(":")
        user_id = int(user_id_str)
    except Exception as e:
        print(f"successful_payment: ОШИБКА парсинга payload {payload!r}: {e}")
        return

    pack = PACKAGES.get(pack_key)
    if not pack:
        print(f"successful_payment: pack_key={pack_key!r} не найден в PACKAGES")
        return

    success = await add_coins(
        user_id=user_id,
        coins_amount=pack["coins"],
        stars_amount=message.successful_payment.total_amount,
        telegram_charge_id=message.successful_payment.telegram_payment_charge_id,
    )
    print(f"successful_payment: add_coins вернул success={success}, user_id={user_id}, coins={pack['coins']}")

    if success:
        await message.answer(
            f"✨ На счёт зачислено {pack['coins']} монет.\n"
            f"Руны ждут твоего вопроса."
        )
    else:
        print(f"successful_payment: add_coins вернул False — возможен дублирующийся telegram_charge_id")
        
# ── Основной обработчик — сохраняем ситуацию ─────────────────────────────────
@dp.message(F.text == "🔮 Гадание")
async def handle_casting_button(message: Message):
    await message.answer(
        "Сначала опиши свою ситуацию или задай вопрос."
    )
    
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

    check_result = await check_coins(user_id, spread_type)

    if check_result == "banned":
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    if check_result == "no_coins":
        await callback.answer()
        await callback.message.answer(  # type: ignore
            "Недостаточно монет для этого расклада.\nПополни баланс и продолжи путь.",
            reply_markup=get_no_coins_keyboard()
        )
        return

    user_states[user_id]["spread_type"] = spread_type

    await callback.answer()
    await _perform_casting(
        user_id=user_id,
        spread_type=spread_type,
        situation=state["situation"],
        first_name=callback.from_user.first_name or "странник",
        chat_id=callback.message.chat.id,
        check_result=check_result,
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

    check_result = await check_coins(user_id, spread_type)

    if check_result == "banned":
        await callback.answer("Доступ ограничен.", show_alert=True)
        return

    if check_result == "no_coins":
        await callback.answer()
        await callback.message.answer(  # type: ignore
            "Недостаточно монет.\nПополни баланс и продолжи путь.",
            reply_markup=get_no_coins_keyboard()
        )
        return

    await callback.answer()
    await _perform_casting(
        user_id=user_id,
        spread_type=spread_type,
        situation=state["situation"],
        first_name=callback.from_user.first_name or "странник",
        chat_id=callback.message.chat.id,
        check_result=check_result,
    )

# ── Само гадание ──────────────────────────────────────────────────────────────

async def _perform_casting(
    user_id: int, spread_type: str, situation: str,
    first_name: str, chat_id: int, check_result: str,
):
    runes = cast_runes(SPREADS[spread_type]["count"])

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
            max_completion_tokens=600,
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
            source="bot",
        )
    except Exception as e:
        print(f"ОШИБКА модели: {e}")
        await bot.send_message(chat_id, "Руны не смогли открыться. Попробуй снова.")
        return

    await spend_coins(user_id, spread_type, check_result)

    lines = interpretation.split('\n')
    recap = lines[0].strip()
    body = '\n'.join(lines[1:]).strip()
    full_message = f"Расклад рун для {first_name} о {recap}\n\n{body}"

    rune_list = " · ".join([
        f"{r['symbol']} {r['name_ru']}{'  🔄' if r['is_reversed'] else ''}"
        for r in runes
    ])
    await bot.send_message(chat_id, rune_list)
    await bot.send_message(chat_id, full_message, reply_markup=get_result_keyboard(spread_type))


# ── HTTP эндпоинты ────────────────────────────────────────────────────────────

def validate_init_data(init_data: str) -> dict | None:
    try:
        parsed     = dict(parse_qsl(init_data, strict_parsing=True))
        hash_value = parsed.pop("hash", None)
        if not hash_value:
            return None

        auth_date = int(parsed.get("auth_date", 0))
        if not auth_date or (datetime.now().timestamp() - auth_date) > 86400:
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

    await log_visit(user_id, source="miniapp")
    await update_user_geo(user_id, language_code=user_data.get("language_code"))

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

async def handle_cast(request: web.Request):
    """Гадание из Mini App."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"})

    init_data   = data.get("init_data", "")
    spread_type = data.get("spread_type", "single")
    situation   = data.get("situation", "")

    if not situation:
        return web.json_response({"ok": False, "error": "no situation"})

    if spread_type not in SPREADS:
        return web.json_response({"ok": False, "error": "unknown spread"})

    user_data = validate_init_data(init_data)
    if not user_data:
        return web.json_response({"ok": False, "error": "invalid init_data"})

    user_id    = int(user_data.get("id", 0))
    first_name = user_data.get("first_name", "странник")

    if not user_id:
        return web.json_response({"ok": False, "error": "no user_id"})

    await get_or_create_user(user_id, username=user_data.get("username"), first_name=first_name)

    check_result = await check_coins(user_id, spread_type)
    if check_result == "banned":
        return web.json_response({"ok": False, "error": "banned"})
    if check_result == "no_coins":
        return web.json_response({"ok": False, "error": "no_coins"})

    runes = cast_runes(SPREADS[spread_type]["count"])
    
    try:
        prompt     = build_prompt(spread_type, situation, runes)
        start_time = datetime.now()
        response   = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt}
            ],
            temperature=0.9,
            max_completion_tokens=600,
        )
        latency_ms     = int((datetime.now() - start_time).total_seconds() * 1000)
        interpretation = response.choices[0].message.content.strip()
        log_casting(
            user_id=user_id,
            spread_type=spread_type,
            stars=0,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            latency_ms=latency_ms,
            source="miniapp",
        )
        
    except Exception as e:
        print(f"ОШИБКА модели: {e}")
        return web.json_response({"ok": False, "error": "generation_failed"})

    await spend_coins(user_id, spread_type, check_result)

    lines = interpretation.split('\n')
    recap = lines[0].strip()
    body = '\n'.join(lines[1:]).strip()
    full_message = f"Расклад рун для {first_name} о {recap}\n\n{body}"

    return web.json_response({
        "ok": True,
        "spread_type": spread_type,
        "text": full_message,
        "runes": [
            {
                "name_ru":     r["name_ru"],
                "reversed":    r["is_reversed"],
                "file":        r["image"],
            }
            for r in runes
        ]
    })
    
# ── Запуск ────────────────────────────────────────────────────────────────────
async def set_webhook_delayed():
    await asyncio.sleep(10)
    for attempt in range(5):
        try:
            await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
            print(f"Webhook установлен: {WEBHOOK_URL}")
            break
        except Exception as e:
            print(f"Webhook попытка {attempt + 1} не удалась: {e}")
            await asyncio.sleep(5)
    else:
        print("Webhook не удалось установить после 5 попыток")

    
async def on_startup(app: web.Application):
    await init_users_db()
    await init_castings_table()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(set_webhook_delayed())
    print("Бот запущен")

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()
    await close_db()
async def handle_set_webhook(request: web.Request):
    token = request.headers.get("X-Secret", "")
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        return web.json_response({"ok": False}, status=403)
    await bot.delete_webhook(drop_pending_updates=True)
    result = await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
    return web.json_response({"ok": True, "webhook": WEBHOOK_URL, "result": str(result)})

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

    resource_cast = cors.add(app.router.add_resource("/cast"))
    cors.add(resource_cast.add_route("POST", handle_cast))

    resource_info = cors.add(app.router.add_resource("/user_info"))
    cors.add(resource_info.add_route("GET", handle_user_info))

    resource_invoice = cors.add(app.router.add_resource("/create_invoice"))
    cors.add(resource_invoice.add_route("POST", handle_create_invoice))

    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main_webhook()