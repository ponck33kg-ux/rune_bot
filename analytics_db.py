async def init_castings_table():
    from database import pool
    async with pool.acquire() as conn: # type: ignore
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS castings (
                id            SERIAL PRIMARY KEY,
                user_id       BIGINT NOT NULL,
                spread_type   TEXT NOT NULL,
                stars         INT DEFAULT 0,
                input_tokens  INT DEFAULT 0,
                output_tokens INT DEFAULT 0,
                latency_ms    INT DEFAULT 0,
                created_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)