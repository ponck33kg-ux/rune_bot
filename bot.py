import aiohttp_cors
import asyncio
import os
import sys
import yaml
import hmac
import hashlib
import json
import random
import time
from urllib.parse import parse_qsl
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from openai import OpenAI
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton,
    PreCheckoutQuery, LabeledPrice, MenuButtonWebApp
)
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter, TelegramBadRequest
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
    get_users_for_reminder, mark_reminder_sent, mark_bot_blocked,
    get_users_for_reminder_pt, mark_reminder_sent_pt,
    get_users_for_channel_reminder, mark_channel_reminder_sent,
    get_user_language, set_user_language, has_chosen_language,
    assign_prompt_variant, get_prompt_variant,
    has_birthdate_record, save_birthdate, save_birthdate_skipped,
    redeem_gift_code,
    SPREAD_COST,
)
from zodiac import get_zodiac_sign
from constants import SUPPORTED_LANGUAGES

from i18n import (
    load_i18n, t, format_coins,
    get_prompt_data, get_system_prompt, get_runes_data, reversed_word,
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
    "single": {"count": 1, "cost": SPREAD_COST["single"]},
    "triple": {"count": 3, "cost": SPREAD_COST["triple"]},
    "five":   {"count": 5, "cost": SPREAD_COST["five"]},
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
casting_in_progress: set[int] = set()
awaiting_birthdate: dict[int, dict] = {}

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


def cast_runes_i18n(count: int, lang: str) -> list[dict]:
    """
    Версия cast_runes для чат-флоу — берёт руны из языковых файлов (Casting/runes/{lang}.yaml),
    а не из старого глобального RUNES (тот используется только миниапкой).
    """
    runes_data = get_runes_data(lang)
    keys   = random.sample(list(runes_data.keys()), count)
    result = []
    for key in keys:
        rune        = runes_data[key]
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


def build_prompt_i18n(lang: str, spread_type: str, situation: str, runes: list[dict], variant: str | None = None) -> str:
    """
    Версия build_prompt для чат-флоу — берёт шаблон из Casting/prompts/{lang}.yaml.
    Для lang="ru" и variant="b" берёт альтернативный промпт (A/B тест).
    """
    template = get_prompt_data(lang, variant)[spread_type]
    kwargs   = {"situation": situation}
    for i, rune in enumerate(runes, 1):
        kwargs[f"rune{i}_name"]     = rune["name"]
        kwargs[f"rune{i}_tags"]     = ", ".join(rune["tags"])
        kwargs[f"rune{i}_reversed"] = reversed_word(lang, rune["is_reversed"])
    return template.format(**kwargs)


# ── Кнопки ────────────────────────────────────────────────────────────────────

def get_main_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "btn_casting"))],
            [KeyboardButton(text=t(lang, "btn_topup"))],
            [KeyboardButton(text=t(lang, "btn_gift"))],
        ],
        resize_keyboard=True,
        persistent=True,
    )

def get_spread_keyboard(free_available: bool, lang: str) -> InlineKeyboardMarkup:
    triple_name  = t(lang, "spread_triple_name")
    triple_label = (
        f"{triple_name} — {t(lang, 'label_free')}"
        if free_available
        else f"{triple_name} — {format_coins(SPREADS['triple']['cost'], lang)}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{t(lang, 'spread_single_name')} — {format_coins(SPREADS['single']['cost'], lang)}",
            callback_data="spread_single"
        )],
        [InlineKeyboardButton(text=triple_label, callback_data="spread_triple")],
        [InlineKeyboardButton(
            text=f"{t(lang, 'spread_five_name')} — {format_coins(SPREADS['five']['cost'], lang)}",
            callback_data="spread_five"
        )],
    ])
    
def get_channel_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=t(lang, "btn_subscribe_channel"),
            url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
        )],
        [InlineKeyboardButton(text=t(lang, "btn_subscribed_confirm"), callback_data="check_subscription")],
    ])

def get_result_keyboard(spread_type: str, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_next_question"), callback_data="new_casting")],
    ])

def get_topup_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "pack_10_desc"),  callback_data="buy_pack_10")],
        [InlineKeyboardButton(text=t(lang, "pack_50_desc"),  callback_data="buy_pack_50")],
        [InlineKeyboardButton(text=t(lang, "pack_180_desc"), callback_data="buy_pack_180")],
        [InlineKeyboardButton(text=t(lang, "pack_800_desc"), callback_data="buy_pack_800")],
    ])

def get_no_coins_keyboard(lang: str) -> InlineKeyboardMarkup:
    return get_topup_keyboard(lang)


def get_language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Русский", callback_data="setlang_ru")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="setlang_en")],
        [InlineKeyboardButton(text="🇵🇹 Português", callback_data="setlang_pt")],
        [InlineKeyboardButton(text="🇪🇸 Español", callback_data="setlang_es")],
    ])
    
def get_skip_birthdate_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "btn_skip"), callback_data="skip_birthdate")],
    ])

# ── Команды ───────────────────────────────────────────────────────────────────

async def _do_start(
    chat_id: int, user_id: int, username: str | None, first_name: str | None,
    lang: str, welcome_key: str = "start_welcome",
):
    """
    Основной флоу приветствия — общий для cmd_start (когда язык уже выбран)
    и handle_language_selection (сразу после выбора языка / после шага с датой
    рождения). Принимает явные параметры, а не Message/CallbackQuery, чтобы
    не путать from_user бота (в callback.message) с from_user реального
    пользователя.
    welcome_key — какой ключ приветствия использовать: обычный "start_welcome"
    (не меняется для старых пользователей) или "start_welcome_new" (с приглашающим
    вопросом, показывается один раз сразу после шага с датой рождения).
    """
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        is_subscribed = False

    bonus_received = await has_channel_bonus(user_id)

    if not (is_subscribed and bonus_received):
        await bot.send_message(
            chat_id,
            t(lang, "subscribe_prompt"),
            reply_markup=get_channel_keyboard(lang)
        )

    balance_data   = await get_user_balance(user_id)
    free_available = balance_data["free_left"] > 0
    coins          = balance_data["coins_balance"]
    free_text      = t(lang, "free_available") if free_available else t(lang, "free_used")

    await bot.send_message(
        chat_id,
        t(lang, welcome_key, coins=coins, free_text=free_text),
        reply_markup=get_main_keyboard(lang)
    )

    await bot.set_chat_menu_button(
        chat_id=chat_id,
        menu_button=MenuButtonWebApp(
            text=t(lang, "menu_button_oracle"),
            web_app=WebAppInfo(url=f"{MINIAPP_URL}/?free={1 if free_available else 0}")
        )
    )


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

    if not await has_chosen_language(user_id):
        await message.answer(
            "Выбери язык / Choose language / Escolha o idioma:",
            reply_markup=get_language_keyboard()
        )
        return

    lang = await get_user_language(user_id)
    await _do_start(
        chat_id=message.chat.id,
        user_id=user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        lang=lang,
    )


@dp.callback_query(F.data.startswith("setlang_"))
async def handle_language_selection(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    lang = (callback.data or "").replace("setlang_", "")
    if lang not in SUPPORTED_LANGUAGES:
        await callback.answer()
        return

    user_id = callback.from_user.id
    await set_user_language(user_id, lang)
    if lang == "ru":
        await assign_prompt_variant(user_id)
    await callback.answer()

    try:
        await callback.message.delete()  # type: ignore
    except TelegramBadRequest:
        pass

    chat_id    = callback.message.chat.id
    username   = callback.from_user.username
    first_name = callback.from_user.first_name

    # Португальских пользователей не трогаем — у них флоу как раньше.
    # Остальных спрашиваем дату рождения, только если ещё не спрашивали.
    if lang != "pt" and not await has_birthdate_record(user_id):
        awaiting_birthdate[user_id] = {
            "chat_id": chat_id, "lang": lang,
            "username": username, "first_name": first_name,
        }
        await bot.send_message(
            chat_id,
            t(lang, "birthdate_prompt"),
            reply_markup=get_skip_birthdate_keyboard(lang)
        )
        return

    await _do_start(
        chat_id=chat_id, user_id=user_id,
        username=username, first_name=first_name, lang=lang,
    )

def _is_awaiting_birthdate(message: Message) -> bool:
    return bool(message.from_user) and message.from_user.id in awaiting_birthdate


def _try_parse_birthdate(text: str):
    """
    Пробуем распознать дату в нескольких распространённых форматах.
    Если не получилось — просто возвращаем None, это НЕ ошибка: исходный
    текст в любом случае будет сохранён как есть (raw_input), без повторного
    запроса у пользователя.
    """
    text = text.strip()
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt).date()
            if 1900 <= parsed.year <= datetime.now().year:
                return parsed
        except ValueError:
            continue
    return None


@dp.message(_is_awaiting_birthdate)
async def handle_birthdate_input(message: Message):
    if not message.from_user or not message.text:
        return
    user_id = message.from_user.id
    state   = awaiting_birthdate.get(user_id)
    if not state:
        return
    lang = state["lang"]

    raw_input   = message.text[:200]
    birth_date  = _try_parse_birthdate(raw_input)
    zodiac_sign = get_zodiac_sign(birth_date) if birth_date else None

    await save_birthdate(user_id, raw_input, birth_date, zodiac_sign)
    awaiting_birthdate.pop(user_id, None)

    await _do_start(
        chat_id=state["chat_id"], user_id=user_id,
        username=state["username"], first_name=state["first_name"],
        lang=lang, welcome_key="start_welcome_new",
    )


@dp.callback_query(F.data == "skip_birthdate")
async def handle_skip_birthdate(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    user_id = callback.from_user.id
    state   = awaiting_birthdate.get(user_id)
    await callback.answer()

    if not state:
        return

    await save_birthdate_skipped(user_id)
    awaiting_birthdate.pop(user_id, None)

    try:
        await callback.message.delete()  # type: ignore
    except TelegramBadRequest:
        pass

    await _do_start(
        chat_id=state["chat_id"], user_id=user_id,
        username=state["username"], first_name=state["first_name"],
        lang=state["lang"], welcome_key="start_welcome_new",
    )

@dp.message(Command("support"))
async def cmd_support(message: Message):
    if not message.from_user:
        return
    lang = await get_user_language(message.from_user.id)
    await message.answer(t(lang, "support_text"))


@dp.message(Command("language"))
async def cmd_language(message: Message):
    if not message.from_user:
        return
    await get_or_create_user(
        message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )
    await message.answer(
        "Выбери язык / Choose language / Escolha o idioma:",
        reply_markup=get_language_keyboard()
    )

@dp.message(F.text.in_(["💰 Пополнить баланс", "💰 Top up balance", "💰 Adicionar saldo"]))
async def ask_topup(message: Message):
    if not message.from_user:
        return
    lang = await get_user_language(message.from_user.id)
    balance_data = await get_user_balance(message.from_user.id)
    coins = balance_data["coins_balance"]
    await message.answer(
        t(lang, "topup_prompt", coins=coins),
        reply_markup=get_topup_keyboard(lang)
    )

@dp.callback_query(F.data.startswith("buy_pack_"))
async def handle_buy_pack(callback: CallbackQuery):
    if not callback.from_user:
        return
    lang = await get_user_language(callback.from_user.id)
    pack_key = (callback.data or "").replace("buy_", "")
    pack = PACKAGES.get(pack_key)
    if not pack:
        await callback.answer(t(lang, "pack_not_found"), show_alert=True)
        return

    pack_label = t(lang, f"{pack_key}_desc")

    await callback.answer()
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=t(lang, "invoice_title"),
        description=pack_label,
        payload=f"{pack_key}:{callback.from_user.id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=pack_label, amount=pack["stars"])],
    )
    
@dp.callback_query(F.data == "new_casting")
async def new_casting(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    user_states.pop(callback.from_user.id, None)
    lang = await get_user_language(callback.from_user.id)
    await callback.message.answer(t(lang, "new_casting_prompt"))  # type: ignore
    await callback.answer()

@dp.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    user_id = callback.from_user.id
    lang = await get_user_language(user_id)
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        is_subscribed = member.status in ("member", "administrator", "creator")
    except Exception:
        is_subscribed = False

    if not is_subscribed:
        await callback.answer(t(lang, "not_subscribed_yet"), show_alert=True)
        return

    given = await give_channel_bonus(user_id)
    if given:
        await callback.answer(t(lang, "subscribe_bonus_given"), show_alert=True)
        try:
            await callback.message.delete()  # type: ignore
        except TelegramBadRequest:
            pass
    else:
        await callback.answer(t(lang, "subscribe_bonus_already_given"), show_alert=True)
        try:
            await callback.message.delete()  # type: ignore
        except TelegramBadRequest:
            pass

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
    situation   = data.get("situation", "")[:500]

    user_id = message.from_user.id
    lang = await get_user_language(user_id)

    if not situation:
        await message.answer(t(lang, "no_situation_received"))
        return

    await get_or_create_user(
        user_id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
    )

    if user_id in casting_in_progress:
        await message.answer(t(lang, "casting_already_in_progress"))
        return
    casting_in_progress.add(user_id)

    try:
        check_result = await check_coins(user_id, spread_type)

        if check_result == "banned":
            return

        if check_result == "no_coins":
            await message.answer(
                t(lang, "no_coins_for_spread"),
                reply_markup=get_no_coins_keyboard(lang)
            )
            return

        user_states[user_id] = {"situation": situation, "spread_type": spread_type}

        await _perform_casting(
            user_id=user_id,
            spread_type=spread_type,
            situation=situation,
            first_name=message.from_user.first_name or t(lang, "default_stranger_name"),
            chat_id=message.chat.id,
            check_result=check_result,
            lang=lang,
        )
    finally:
        casting_in_progress.discard(user_id)
        
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

    lang = await get_user_language(user_id)

    success = await add_coins(
        user_id=user_id,
        coins_amount=pack["coins"],
        stars_amount=message.successful_payment.total_amount,
        telegram_charge_id=message.successful_payment.telegram_payment_charge_id,
    )
    print(f"successful_payment: add_coins вернул success={success}, user_id={user_id}, coins={pack['coins']}")

    if success:
        await message.answer(t(lang, "payment_success", coins=pack["coins"]))
    else:
        print(f"successful_payment: add_coins вернул False — возможен дублирующийся telegram_charge_id")
        
# ── Основной обработчик — сохраняем ситуацию ─────────────────────────────────

@dp.message(F.text.in_(["🔮 Гадание", "🔮 Divination", "🔮 Adivinhação"]))
async def handle_casting_button(message: Message):
    if not message.from_user:
        return
    lang = await get_user_language(message.from_user.id)
    await message.answer(t(lang, "describe_situation_first"))


# ── Подарочные коды ───────────────────────────────────────────────────────────

GIFT_ATTEMPT_LIMIT  = 5
GIFT_ATTEMPT_WINDOW = 600  # 10 минут

gift_code_attempts: dict[int, list[float]] = {}


def _gift_attempts_exceeded(user_id: int) -> bool:
    now      = time.monotonic()
    attempts = [t for t in gift_code_attempts.get(user_id, []) if now - t < GIFT_ATTEMPT_WINDOW]
    gift_code_attempts[user_id] = attempts
    return len(attempts) >= GIFT_ATTEMPT_LIMIT


def _record_gift_attempt(user_id: int):
    gift_code_attempts.setdefault(user_id, []).append(time.monotonic())


@dp.message(Command("gift"))
async def handle_gift_command(message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    lang    = await get_user_language(user_id)
    parts   = (message.text or "").split(maxsplit=1)

    if len(parts) < 2 or not parts[1].strip():
        await message.answer(t(lang, "gift_missing_code"))
        return

    if _gift_attempts_exceeded(user_id):
        await message.answer(t(lang, "gift_too_many_attempts"))
        return

    code   = parts[1].strip()
    result = await redeem_gift_code(user_id, code)

    if result["status"] == "ok":
        await message.answer(t(lang, "gift_activated", coins=result["coins_amount"]))
    elif result["status"] == "already_used":
        _record_gift_attempt(user_id)
        await message.answer(t(lang, "gift_already_used"))
    elif result["status"] == "banned":
        await message.answer(t(lang, "gift_banned"))
    else:
        # not_found, expired, exhausted — один и тот же текст,
        # чтобы не облегчать подбор действующих кодов
        _record_gift_attempt(user_id)
        await message.answer(t(lang, "gift_invalid"))


@dp.message(F.text.in_(["🎁 Подарочный код", "🎁 Gift code", "🎁 Código de presente", "🎁 Código de regalo"]))
async def handle_gift_button(message: Message):
    if not message.from_user:
        return
    lang = await get_user_language(message.from_user.id)
    await message.answer(t(lang, "gift_button_hint"))


@dp.message()
async def handle_message(message: Message):
    if not message.from_user:
        return
    user_text = message.text
    if not user_text:
        return

    user_id = message.from_user.id
    lang = await get_user_language(user_id)
    await get_or_create_user(
        user_id,
        username=message.from_user.username,  # type: ignore
        first_name=message.from_user.first_name,
    )

    balance_data   = await get_user_balance(user_id)
    free_available = balance_data["free_left"] > 0

    user_states[user_id] = {"situation": user_text[:500]}

    await message.answer(
        t(lang, "runes_ready_choose_spread"),
        reply_markup=get_spread_keyboard(free_available, lang)
    )


# ── Выбор расклада ────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("spread_"))

async def handle_spread(callback: CallbackQuery):
    if not callback.from_user or not callback.message:
        return
    spread_type = (callback.data or "").replace("spread_", "")
    user_id     = callback.from_user.id
    lang        = await get_user_language(user_id)

    state = user_states.get(user_id)
    if not state:
        await callback.message.answer(t(lang, "describe_situation_only"))  # type: ignore
        await callback.answer()
        return

    if user_id in casting_in_progress:
        await callback.answer(t(lang, "casting_already_in_progress"), show_alert=True)
        return
    casting_in_progress.add(user_id)

    try:
        check_result = await check_coins(user_id, spread_type)

        if check_result == "banned":
            await callback.answer(t(lang, "access_restricted"), show_alert=True)
            return

        if check_result == "no_coins":
            await callback.answer()
            await callback.message.answer(  # type: ignore
                t(lang, "no_coins_for_spread"),
                reply_markup=get_no_coins_keyboard(lang)
            )
            return

        user_states[user_id]["spread_type"] = spread_type

        await callback.answer()
        await _perform_casting(
            user_id=user_id,
            spread_type=spread_type,
            situation=state["situation"],
            first_name=callback.from_user.first_name or t(lang, "default_stranger_name"),
            chat_id=callback.message.chat.id,
            check_result=check_result,
            lang=lang,
        )
    finally:
        casting_in_progress.discard(user_id)


# ── Само гадание ──────────────────────────────────────────────────────────────

async def _perform_casting(
    user_id: int, spread_type: str, situation: str,
    first_name: str, chat_id: int, check_result: str, lang: str,
):
    runes   = cast_runes_i18n(SPREADS[spread_type]["count"], lang)
    variant = await get_prompt_variant(user_id) if lang == "ru" else None

    try:
        await bot.send_chat_action(chat_id, "typing")
        prompt        = build_prompt_i18n(lang, spread_type, situation, runes, variant=variant)
        system_prompt = get_system_prompt(lang, variant)

        start_time = datetime.now()
        response   = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
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
            situation=situation,
            answer_text=interpretation,
            prompt_variant=variant,
        )
    except Exception as e:
        print(f"ОШИБКА модели: {e}")
        await bot.send_message(chat_id, t(lang, "runes_failed"))
        return

    await spend_coins(user_id, spread_type, check_result)

    lines = interpretation.split('\n')
    recap = lines[0].strip()
    body = '\n'.join(lines[1:]).strip()
    full_message = t(lang, "casting_result_header", first_name=first_name, recap=recap) + f"\n\n{body}"

    rune_list = " · ".join([
        f"{r['symbol']} {r['name']}{'  🔄' if r['is_reversed'] else ''}"
        for r in runes
    ])
    await bot.send_message(chat_id, rune_list)
    await bot.send_message(chat_id, full_message, reply_markup=get_result_keyboard(spread_type, lang))


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
    situation   = data.get("situation", "")[:500]

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

    if user_id in casting_in_progress:
        return web.json_response({"ok": False, "error": "already_in_progress"})
    casting_in_progress.add(user_id)

    try:
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
                situation=situation,
                answer_text=interpretation,
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
    finally:
        casting_in_progress.discard(user_id)
   
    # ── Ежедневное напоминание ────────────────────────────────────────────────────

REMINDER_MSK = timezone(timedelta(hours=3))
DAILY_REMINDER_HOUR_MSK = 11
CHANNEL_REMINDER_HOUR_MSK = 20

REMINDER_RIO = timezone(timedelta(hours=-3))
DAILY_REMINDER_HOUR_RIO = 10

def _next_reminder_time_msk() -> datetime:
    now_msk = datetime.now(REMINDER_MSK)
    target  = now_msk.replace(hour=DAILY_REMINDER_HOUR_MSK, minute=0, second=0, microsecond=0)
    if target <= now_msk:
        target += timedelta(days=1)
    return target


def _next_reminder_time_rio() -> datetime:
    now_rio = datetime.now(REMINDER_RIO)
    target  = now_rio.replace(hour=DAILY_REMINDER_HOUR_RIO, minute=0, second=0, microsecond=0)
    if target <= now_rio:
        target += timedelta(days=1)
    return target


def _next_channel_reminder_time_msk() -> datetime:
    now_msk = datetime.now(REMINDER_MSK)
    target  = now_msk.replace(hour=CHANNEL_REMINDER_HOUR_MSK, minute=0, second=0, microsecond=0)
    if target <= now_msk:
        target += timedelta(days=1)
    return target


async def _send_reminder(user_id: int, lang: str):
    reminder_text = t(lang, "daily_reminder")
    try:
        await bot.send_message(user_id, reminder_text)
        await mark_reminder_sent(user_id)
    except TelegramForbiddenError:
        await mark_bot_blocked(user_id)
    except TelegramBadRequest as e:
        if "chat not found" in str(e).lower():
            await mark_bot_blocked(user_id)
        else:
            print(f"Ошибка отправки напоминания user_id={user_id}: {e}")
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.send_message(user_id, reminder_text)
            await mark_reminder_sent(user_id)
        except Exception as e2:
            print(f"Ошибка повторной отправки напоминания user_id={user_id}: {e2}")
    except Exception as e:
        print(f"Ошибка отправки напоминания user_id={user_id}: {e}")


async def send_daily_reminders():
    users = await get_users_for_reminder()
    if not users:
        return
    print(f"Рассылка напоминаний: {len(users)} пользователей")
    for user_id, lang in users:
        await _send_reminder(user_id, lang)
        await asyncio.sleep(0.05)
    print("Рассылка напоминаний завершена")


async def daily_reminder_scheduler():
    while True:
        target       = _next_reminder_time_msk()
        wait_seconds = (target - datetime.now(REMINDER_MSK)).total_seconds()
        await asyncio.sleep(wait_seconds)
        try:
            await send_daily_reminders()
        except Exception as e:
            print(f"Ошибка планировщика напоминаний: {e}")

async def send_daily_reminders_pt():
    user_ids = await get_users_for_reminder_pt()
    if not user_ids:
        return
    print(f"Рассылка напоминаний (pt/Rio): {len(user_ids)} пользователей")
    for user_id in user_ids:
        await _send_reminder_pt(user_id)
        await asyncio.sleep(0.05)
    print("Рассылка напоминаний (pt/Rio) завершена")


async def _send_reminder_pt(user_id: int):
    reminder_text = t("pt", "daily_reminder")
    try:
        await bot.send_message(user_id, reminder_text)
        await mark_reminder_sent_pt(user_id)
    except TelegramForbiddenError:
        await mark_bot_blocked(user_id)
    except TelegramBadRequest as e:
        if "chat not found" in str(e).lower():
            await mark_bot_blocked(user_id)
        else:
            print(f"Ошибка отправки напоминания (pt) user_id={user_id}: {e}")
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.send_message(user_id, reminder_text)
            await mark_reminder_sent_pt(user_id)
        except Exception as e2:
            print(f"Ошибка повторной отправки напоминания (pt) user_id={user_id}: {e2}")
    except Exception as e:
        print(f"Ошибка отправки напоминания (pt) user_id={user_id}: {e}")


async def daily_reminder_scheduler_pt():
    while True:
        target       = _next_reminder_time_rio()
        wait_seconds = (target - datetime.now(REMINDER_RIO)).total_seconds()
        await asyncio.sleep(wait_seconds)
        try:
            await send_daily_reminders_pt()
        except Exception as e:
            print(f"Ошибка планировщика напоминаний (pt): {e}")

async def _send_channel_reminder(user_id: int):
    reminder_text = t("ru", "channel_reminder_text")
    try:
        await bot.send_message(user_id, reminder_text)
        await mark_channel_reminder_sent(user_id)
    except TelegramForbiddenError:
        await mark_bot_blocked(user_id)
    except TelegramBadRequest as e:
        if "chat not found" in str(e).lower():
            await mark_bot_blocked(user_id)
        else:
            print(f"Ошибка отправки напоминания о подписке user_id={user_id}: {e}")
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await bot.send_message(user_id, reminder_text)
            await mark_channel_reminder_sent(user_id)
        except Exception as e2:
            print(f"Ошибка повторной отправки напоминания о подписке user_id={user_id}: {e2}")
    except Exception as e:
        print(f"Ошибка отправки напоминания о подписке user_id={user_id}: {e}")


async def send_channel_reminders():
    users = await get_users_for_channel_reminder()
    if not users:
        return
    print(f"Рассылка напоминаний о подписке: {len(users)} пользователей")
    for user_id in users:
        await _send_channel_reminder(user_id)
        await asyncio.sleep(0.05)
    print("Рассылка напоминаний о подписке завершена")


async def channel_reminder_scheduler():
    while True:
        target       = _next_channel_reminder_time_msk()
        wait_seconds = (target - datetime.now(REMINDER_MSK)).total_seconds()
        await asyncio.sleep(wait_seconds)
        try:
            await send_channel_reminders()
        except Exception as e:
            print(f"Ошибка планировщика напоминаний о подписке: {e}")
    
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
    load_i18n()
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(set_webhook_delayed())
    asyncio.create_task(daily_reminder_scheduler())
    asyncio.create_task(daily_reminder_scheduler_pt())
    asyncio.create_task(channel_reminder_scheduler())
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