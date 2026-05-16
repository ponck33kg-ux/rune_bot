import os
import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL")

pool: asyncpg.Pool | None = None


async def init_db():
    global pool
    if pool is None:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS castings (
                id           SERIAL PRIMARY KEY,
                user_id      BIGINT NOT NULL,
                spread_type  TEXT NOT NULL,
                stars        INT DEFAULT 0,
                input_tokens  INT DEFAULT 0,
                output_tokens INT DEFAULT 0,
                latency_ms   INT DEFAULT 0,
                created_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
