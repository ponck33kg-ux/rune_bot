import os
import asyncpg
from datetime import datetime, timezone, timedelta

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
                ADD COLUMN IF NOT EXISTS city          TEXT
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_visits_user_id ON user_visits (user_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_user_visits_created_at ON user_visits (created_at)")
       
async def close_db():
    global pool
    if pool:
        await pool.close()


async def get_or_create_user(
    user_id: int,
    username: str | None = None,
    first_name: str | None = None
):
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
        return user


def _next_midnight_msk() -> datetime:
    now_msk = datetime.now(MSK)
    return (now_msk + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


async def check_and_spend_coins(user_id: int, spread_type: str) -> str:
    """
    Проверить баланс и списать монеты за гадание.
    Возвращает:
      'spend_free'  — списано бесплатное (single раз в сутки)
      'spend_paid'  — списаны монеты
      'no_coins'    — недостаточно монет
      'banned'      — пользователь заблокирован
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

            if spread_type == "single" and free_used < 1:
                await conn.execute("""
                    UPDATE users SET free_used_today = free_used_today + 1
                    WHERE user_id = $1
                """, user_id)
                return "spend_free"

            balance = user["coins_balance"]
            if balance < cost:
                return "no_coins"

            await conn.execute("""
                UPDATE users SET coins_balance = coins_balance - $1
                WHERE user_id = $2
            """, cost, user_id)
            await conn.execute("""
                INSERT INTO transactions (user_id, type, coins_amount)
                VALUES ($1, 'spend', $2)
            """, user_id, cost)
            return "spend_paid"


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