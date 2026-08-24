import os
import random
import asyncpg
from datetime import datetime, timezone, timedelta
from constants import SUPPORTED_LANGUAGES

MSK = timezone(timedelta(hours=3))

pool: asyncpg.Pool | None = None  # type: ignore

SPREAD_COST = {
    "single": 1,
    "triple": 3,
    "five":   5,
}


async def init_db():
    global pool

    database_url = os.getenv("DATABASE_URL")

    if database_url:
        # Railway — одна переменная DATABASE_URL
        pool = await asyncpg.create_pool(
            database_url,
            min_size=2,
            max_size=10,
            ssl="require",
        )
    else:
        # Локально — отдельные переменные
        pool = await asyncpg.create_pool(
            host=os.getenv("DB_HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", ""),
            database=os.getenv("DB_NAME", "rune_bot"),
            min_size=2,
            max_size=10,
        )

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id             BIGINT PRIMARY KEY,
                username            TEXT,
                first_name          TEXT,
                coins_balance       INT DEFAULT 0,
                free_used_today     INT DEFAULT 0,
                free_reset_at       TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                channel_bonus_given BOOLEAN DEFAULT FALSE,
                is_banned           BOOLEAN DEFAULT FALSE,
                created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id                  SERIAL PRIMARY KEY,
                user_id             BIGINT REFERENCES users(user_id),
                type                TEXT NOT NULL,
                coins_amount        INT NOT NULL,
                stars_amount        INT,
                telegram_charge_id  TEXT UNIQUE,
                created_at          TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_visits (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT REFERENCES users(user_id),
                source      TEXT NOT NULL,
                created_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        
        await conn.execute("""
            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS language_code TEXT,
                ADD COLUMN IF NOT EXISTS country_code  TEXT,
                ADD COLUMN IF NOT EXISTS city          TEXT,
                ADD COLUMN IF NOT EXISTS interface_lang TEXT,
                ADD COLUMN IF NOT EXISTS prompt_variant TEXT
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_visits_user_id ON user_visits (user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_visits_created_at ON user_visits (created_at)")

        await conn.execute("""
            ALTER TABLE users
                ADD COLUMN IF NOT EXISTS bot_blocked BOOLEAN DEFAULT FALSE
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_reminders (
                user_id    BIGINT NOT NULL,
                sent_date  DATE NOT NULL,
                PRIMARY KEY (user_id, sent_date)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_reminders_pt (
                user_id    BIGINT NOT NULL,
                sent_date  DATE NOT NULL,
                PRIMARY KEY (user_id, sent_date)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_zodiac (
                user_id      BIGINT PRIMARY KEY REFERENCES users(user_id),
                raw_input    TEXT,
                birth_date   DATE,
                zodiac_sign  TEXT,
                skipped      BOOLEAN DEFAULT FALSE,
                created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_subscribe_reminders (
                user_id         BIGINT PRIMARY KEY REFERENCES users(user_id),
                last_sent_date  DATE NOT NULL
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gift_codes (
                code              TEXT PRIMARY KEY,
                coins_amount      INT NOT NULL,
                max_activations   INT,
                activations_used  INT NOT NULL DEFAULT 0,
                is_active         BOOLEAN NOT NULL DEFAULT TRUE,
                expires_at        TIMESTAMP WITH TIME ZONE,
                comment           TEXT,
                created_at        TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS gift_code_redemptions (
                id            SERIAL PRIMARY KEY,
                code          TEXT NOT NULL REFERENCES gift_codes(code),
                user_id       BIGINT NOT NULL REFERENCES users(user_id),
                coins_amount  INT NOT NULL,
                created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE (code, user_id)
            )
        """)
       
async def close_db():
    global pool
    if pool:
        await pool.close()


async def get_or_create_user(
    user_id: int,
    username: str | None = None,
    first_name: str | None = None
) -> tuple:
    async with pool.acquire() as conn:  # type: ignore
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE user_id = $1", user_id
        )
        if not user:
            user = await conn.fetchrow("""
                INSERT INTO users (user_id, username, first_name)
                VALUES ($1, $2, $3)
                RETURNING *
            """, user_id, username, first_name)
            return user, True
        return user, False


def _next_midnight_msk() -> datetime:
    now_msk = datetime.now(MSK)
    return (now_msk + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


async def check_coins(user_id: int, spread_type: str) -> str:
    """
    Проверить доступность гадания без списания.
    Возвращает:
      'free'     — доступно бесплатное гадание (triple раз в сутки)
      'paid'     — будет списано с баланса монет
      'no_coins' — недостаточно монет
      'banned'   — пользователь заблокирован
    """
    cost = SPREAD_COST.get(spread_type, 1)

    async with pool.acquire() as conn:  # type: ignore
        async with conn.transaction():
            user = await conn.fetchrow("""
                INSERT INTO users (user_id)
                VALUES ($1)
                ON CONFLICT (user_id) DO UPDATE SET user_id = EXCLUDED.user_id
                RETURNING *
            """, user_id)

            if user["is_banned"]:
                return "banned"

            now       = datetime.now(MSK)
            free_used = user["free_used_today"]
            reset_at  = user["free_reset_at"]

            if reset_at and reset_at.astimezone(MSK).date() < now.date():
                free_used = 0
                await conn.execute("""
                    UPDATE users
                    SET free_used_today = 0, free_reset_at = $1
                    WHERE user_id = $2
                """, _next_midnight_msk(), user_id)

            if spread_type == "triple" and free_used < 1:
                return "free"

            balance = user["coins_balance"]
            if balance < cost:
                return "no_coins"

            return "paid"


async def spend_coins(user_id: int, spread_type: str, check_result: str):
    """
    Списать монеты/бесплатное гадание после успешной генерации.
    check_result — результат check_coins ('free' | 'paid').
    """
    cost = SPREAD_COST.get(spread_type, 1)

    async with pool.acquire() as conn:  # type: ignore
        async with conn.transaction():
            if check_result == "free":
                await conn.execute("""
                    UPDATE users SET free_used_today = free_used_today + 1
                    WHERE user_id = $1
                """, user_id)
            elif check_result == "paid":
                await conn.execute("""
                    UPDATE users SET coins_balance = coins_balance - $1
                    WHERE user_id = $2
                """, cost, user_id)
                await conn.execute("""
                    INSERT INTO transactions (user_id, type, coins_amount)
                    VALUES ($1, 'spend', $2)
                """, user_id, cost)


async def add_coins(
    user_id: int,
    coins_amount: int,
    stars_amount: int,
    telegram_charge_id: str
) -> bool:
    async with pool.acquire() as conn:  # type: ignore
        async with conn.transaction():
            try:
                await conn.execute("""
                    INSERT INTO transactions
                        (user_id, type, coins_amount, stars_amount, telegram_charge_id)
                    VALUES ($1, 'purchase', $2, $3, $4)
                """, user_id, coins_amount, stars_amount, telegram_charge_id)
            except asyncpg.UniqueViolationError:
                return False

            await conn.execute("""
                UPDATE users SET coins_balance = coins_balance + $1
                WHERE user_id = $2
            """, coins_amount, user_id)
            return True


async def get_user_balance(user_id: int) -> dict:
    async with pool.acquire() as conn:  # type: ignore
        user = await conn.fetchrow(
            "SELECT coins_balance, free_used_today, free_reset_at FROM users WHERE user_id = $1",
            user_id
        )
        if not user:
            return {"coins_balance": 0, "free_left": 1, "free_total": 1}

        now       = datetime.now(MSK)
        free_used = user["free_used_today"]
        reset_at  = user["free_reset_at"]

        if reset_at and reset_at.astimezone(MSK).date() < now.date():
            free_used = 0

        return {
            "coins_balance": user["coins_balance"],
            "free_left":     max(0, 1 - free_used),
            "free_total":    1,
        }


async def give_channel_bonus(user_id: int) -> bool:
    async with pool.acquire() as conn:  # type: ignore
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT channel_bonus_given FROM users WHERE user_id = $1", user_id
            )
            if not user or user["channel_bonus_given"]:
                return False

            await conn.execute("""
                UPDATE users
                SET coins_balance       = coins_balance + 10,
                    channel_bonus_given = TRUE
                WHERE user_id = $1
            """, user_id)
            await conn.execute("""
                INSERT INTO transactions (user_id, type, coins_amount)
                VALUES ($1, 'channel_bonus', 10)
            """, user_id)
            return True


async def has_channel_bonus(user_id: int) -> bool:
    async with pool.acquire() as conn:  # type: ignore
        row = await conn.fetchrow(
            "SELECT channel_bonus_given FROM users WHERE user_id = $1", user_id
        )
        return bool(row and row["channel_bonus_given"])


async def ban_user(user_id: int):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute(
            "UPDATE users SET is_banned = TRUE WHERE user_id = $1", user_id
        )


async def unban_user(user_id: int):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute(
            "UPDATE users SET is_banned = FALSE WHERE user_id = $1", user_id
        )


async def grant_coins(user_id: int, amount: int):
    async with pool.acquire() as conn:  # type: ignore
        async with conn.transaction():
            await conn.execute("""
                UPDATE users SET coins_balance = coins_balance + $1
                WHERE user_id = $2
            """, amount, user_id)
            await conn.execute("""
                INSERT INTO transactions (user_id, type, coins_amount)
                VALUES ($1, 'grant', $2)
            """, user_id, amount)

async def log_visit(user_id: int, source: str):
    """
    Записать заход пользователя.
    source: 'bot' | 'miniapp'
    """
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            INSERT INTO user_visits (user_id, source)
            VALUES ($1, $2)
        """, user_id, source)

async def update_user_geo(
    user_id: int,
    language_code: str | None = None,
    country_code: str | None = None,
    city: str | None = None
):
    """
    Обновить гео-данные пользователя.
    Передавать только те поля, которые нужно обновить — остальные останутся как есть.
    """
    fields = []
    values = []
    idx = 1

    if language_code is not None:
        fields.append(f"language_code = ${idx}")
        values.append(language_code)
        idx += 1
    if country_code is not None:
        fields.append(f"country_code = ${idx}")
        values.append(country_code)
        idx += 1
    if city is not None:
        fields.append(f"city = ${idx}")
        values.append(city)
        idx += 1

    if not fields:
        return

    values.append(user_id)
    query = f"UPDATE users SET {', '.join(fields)} WHERE user_id = ${idx}"

    async with pool.acquire() as conn:  # type: ignore
        await conn.execute(query, *values)
        
async def track_referral_click(code: str):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            UPDATE referrals SET clicks = clicks + 1
            WHERE code = $1
        """, code)
        await conn.execute("""
            INSERT INTO referral_events (code, event_type)
            VALUES ($1, 'click')
        """, code)


async def track_referral_conversion(code: str):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            UPDATE referrals SET conversions = conversions + 1
            WHERE code = $1
        """, code)
        await conn.execute("""
            INSERT INTO referral_events (code, event_type)
            VALUES ($1, 'conversion')
        """, code)
        
async def get_users_for_reminder() -> list[tuple[int, str]]:
    """
    Пользователи, у которых бесплатное гадание фактически доступно
    на момент рассылки (11:00 МСК), не забанены, не заблокировали бота,
    НЕ выбрали португальский (для них отдельная рассылка по времени Рио —
    см. get_users_for_reminder_pt), и ещё не получали напоминание сегодня
    (по МСК-дате).
    """
    async with pool.acquire() as conn:  # type: ignore
        rows = await conn.fetch("""
            SELECT user_id, interface_lang FROM users
            WHERE is_banned = FALSE
              AND bot_blocked = FALSE
              AND interface_lang IS DISTINCT FROM 'pt'
              AND (
                    free_used_today = 0
                    OR (free_reset_at + interval '3 hours')::date
                       < (NOW() + interval '3 hours')::date
                  )
              AND NOT EXISTS (
                    SELECT 1 FROM daily_reminders
                    WHERE daily_reminders.user_id = users.user_id
                      AND daily_reminders.sent_date = (NOW() + interval '3 hours')::date
              )
        """)
        return [
            (row["user_id"], row["interface_lang"] if row["interface_lang"] in SUPPORTED_LANGUAGES else "ru")
            for row in rows
        ]


async def mark_reminder_sent(user_id: int):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            INSERT INTO daily_reminders (user_id, sent_date)
            VALUES ($1, (NOW() + interval '3 hours')::date)
            ON CONFLICT (user_id, sent_date) DO NOTHING
        """, user_id)


async def mark_bot_blocked(user_id: int):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            UPDATE users SET bot_blocked = TRUE WHERE user_id = $1
        """, user_id)


async def get_user_language(user_id: int) -> str:
    """
    Вернуть выбранный пользователем язык интерфейса.
    Фолбэк на 'ru', если язык ещё не выбран (NULL) или не входит в поддерживаемые.
    """
    async with pool.acquire() as conn:  # type: ignore
        row = await conn.fetchrow(
            "SELECT interface_lang FROM users WHERE user_id = $1", user_id
        )
        lang = row["interface_lang"] if row else None
        if lang not in SUPPORTED_LANGUAGES:
            return "ru"
        return lang


async def set_user_language(user_id: int, lang: str):
    """
    Сохранить выбранный пользователем язык интерфейса.
    """
    if lang not in SUPPORTED_LANGUAGES:
        return
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            UPDATE users SET interface_lang = $1
            WHERE user_id = $2
        """, lang, user_id)


async def has_chosen_language(user_id: int) -> bool:
    """
    Проверить, выбрал ли пользователь язык интерфейса явно (нажал кнопку).
    В отличие от get_user_language, не возвращает фолбэк — нужен именно
    факт "выбор ещё не сделан", чтобы решить, показывать ли клавиатуру выбора.
    """
    async with pool.acquire() as conn:  # type: ignore
        row = await conn.fetchrow(
            "SELECT interface_lang FROM users WHERE user_id = $1", user_id
        )
        return bool(row and row["interface_lang"] in SUPPORTED_LANGUAGES)


async def assign_prompt_variant(user_id: int) -> str:
    """
    Назначить пользователю группу A/B теста промптов, если ещё не назначена.
    Атомарно через COALESCE — повторный вызов для уже назначенного
    пользователя ничего не меняет и возвращает существующее значение.
    """
    variant = random.choice(["a", "b"])
    async with pool.acquire() as conn:  # type: ignore
        row = await conn.fetchrow("""
            UPDATE users
            SET prompt_variant = COALESCE(prompt_variant, $2)
            WHERE user_id = $1
            RETURNING prompt_variant
        """, user_id, variant)
        return row["prompt_variant"] if row else variant


async def get_prompt_variant(user_id: int) -> str | None:
    """Вернуть назначенную группу A/B теста промптов ('a' / 'b' / None, если не назначена)."""
    async with pool.acquire() as conn:  # type: ignore
        row = await conn.fetchrow(
            "SELECT prompt_variant FROM users WHERE user_id = $1", user_id
        )
        return row["prompt_variant"] if row else None


CHANNEL_REMINDER_LANGUAGE_CODES = (
    'ru', 'uk', 'be', 'kk', 'uz', 'ky', 'tg', 'az', 'hy', 'ka'
)


async def get_users_for_channel_reminder() -> list[int]:
    """
    Пользователи, которые ещё не подписались на канал (не забрали бонус),
    не забанены, не заблокировали бота, чей язык интерфейса Telegram
    (language_code — автоопределение, не выбор в боте) входит в список
    языков региона СНГ/русскоязычной аудитории, и которым либо никогда
    не слали напоминание про подписку, либо слали 3+ дня назад (по МСК-дате).
    """
    async with pool.acquire() as conn:  # type: ignore
        rows = await conn.fetch("""
            SELECT u.user_id
            FROM users u
            LEFT JOIN channel_subscribe_reminders r ON r.user_id = u.user_id
            WHERE u.is_banned = FALSE
              AND u.bot_blocked = FALSE
              AND u.channel_bonus_given = FALSE
              AND u.language_code = ANY($1::text[])
              AND (
                    r.last_sent_date IS NULL
                    OR r.last_sent_date <= (NOW() + interval '3 hours')::date - 3
                  )
        """, list(CHANNEL_REMINDER_LANGUAGE_CODES))
        return [row["user_id"] for row in rows]


async def mark_channel_reminder_sent(user_id: int):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            INSERT INTO channel_subscribe_reminders (user_id, last_sent_date)
            VALUES ($1, (NOW() + interval '3 hours')::date)
            ON CONFLICT (user_id) DO UPDATE SET last_sent_date = EXCLUDED.last_sent_date
        """, user_id)
async def get_users_for_reminder_pt() -> list[int]:
    """
    Пользователи с interface_lang = 'pt' (считаем бразильцами по умолчанию),
    у которых бесплатное гадание доступно на момент рассылки (10:00 по времени
    Рио), не забанены, не заблокировали бота, и ещё не получали это напоминание
    сегодня (по дате Рио).
    """
    async with pool.acquire() as conn:  # type: ignore
        rows = await conn.fetch("""
            SELECT user_id FROM users
            WHERE is_banned = FALSE
              AND bot_blocked = FALSE
              AND interface_lang = 'pt'
              AND (
                    free_used_today = 0
                    OR (free_reset_at - interval '3 hours')::date
                       < (NOW() - interval '3 hours')::date
                  )
              AND NOT EXISTS (
                    SELECT 1 FROM daily_reminders_pt
                    WHERE daily_reminders_pt.user_id = users.user_id
                      AND daily_reminders_pt.sent_date = (NOW() - interval '3 hours')::date
              )
        """)
        return [row["user_id"] for row in rows]


async def mark_reminder_sent_pt(user_id: int):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            INSERT INTO daily_reminders_pt (user_id, sent_date)
            VALUES ($1, (NOW() - interval '3 hours')::date)
            ON CONFLICT (user_id, sent_date) DO NOTHING
        """, user_id)

async def has_birthdate_record(user_id: int) -> bool:
    """
    Проверить, была ли уже задана дата рождения ИЛИ нажата кнопка "Пропустить".
    В обоих случаях повторно этот шаг показывать не нужно.
    """
    async with pool.acquire() as conn:  # type: ignore
        row = await conn.fetchrow(
            "SELECT 1 FROM user_zodiac WHERE user_id = $1", user_id
        )
        return row is not None


async def save_birthdate(user_id: int, raw_input: str, birth_date=None, zodiac_sign: str | None = None):
    """
    Сохранить ответ пользователя на шаге "дата рождения". raw_input сохраняется
    всегда, каким бы ни был введённый текст. birth_date/zodiac_sign заполняются
    только если удалось распознать дату — если нет, остаются NULL, запись
    всё равно сохраняется без повторного запроса у пользователя.
    """
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            INSERT INTO user_zodiac (user_id, raw_input, birth_date, zodiac_sign, skipped)
            VALUES ($1, $2, $3, $4, FALSE)
            ON CONFLICT (user_id) DO UPDATE
                SET raw_input = EXCLUDED.raw_input,
                    birth_date = EXCLUDED.birth_date,
                    zodiac_sign = EXCLUDED.zodiac_sign,
                    skipped = FALSE
        """, user_id, raw_input, birth_date, zodiac_sign)


async def save_birthdate_skipped(user_id: int):
    async with pool.acquire() as conn:  # type: ignore
        await conn.execute("""
            INSERT INTO user_zodiac (user_id, skipped)
            VALUES ($1, TRUE)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)


async def redeem_gift_code(user_id: int, code: str) -> dict:
    """
    Активировать подарочный код.
    Возвращает {"status": ..., "coins_amount": int | None}
    status:
      'ok'           — код активирован, монеты начислены
      'not_found'    — код не существует или деактивирован
      'expired'      — код истёк
      'exhausted'    — исчерпан лимит активаций
      'already_used' — этот пользователь уже использовал этот код
      'banned'       — пользователь заблокирован
    """
    code = code.strip().upper()

    async with pool.acquire() as conn:  # type: ignore
        async with conn.transaction():
            user = await conn.fetchrow(
                "SELECT is_banned FROM users WHERE user_id = $1", user_id
            )
            if user and user["is_banned"]:
                return {"status": "banned", "coins_amount": None}

            gift = await conn.fetchrow("""
                SELECT * FROM gift_codes
                WHERE code = $1
                FOR UPDATE
            """, code)

            if not gift or not gift["is_active"]:
                return {"status": "not_found", "coins_amount": None}

            if gift["expires_at"] and gift["expires_at"] < datetime.now(timezone.utc):
                return {"status": "expired", "coins_amount": None}

            if gift["max_activations"] is not None and gift["activations_used"] >= gift["max_activations"]:
                return {"status": "exhausted", "coins_amount": None}

            inserted = await conn.fetchrow("""
                INSERT INTO gift_code_redemptions (code, user_id, coins_amount)
                VALUES ($1, $2, $3)
                ON CONFLICT (code, user_id) DO NOTHING
                RETURNING id
            """, code, user_id, gift["coins_amount"])

            if not inserted:
                return {"status": "already_used", "coins_amount": None}

            await conn.execute("""
                UPDATE gift_codes SET activations_used = activations_used + 1
                WHERE code = $1
            """, code)

            await conn.execute("""
                UPDATE users SET coins_balance = coins_balance + $1
                WHERE user_id = $2
            """, gift["coins_amount"], user_id)

            await conn.execute("""
                INSERT INTO transactions (user_id, type, coins_amount)
                VALUES ($1, 'gift_code', $2)
            """, user_id, gift["coins_amount"])

            return {"status": "ok", "coins_amount": gift["coins_amount"]}