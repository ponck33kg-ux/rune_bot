import asyncio

def log_casting(
    user_id: int,
    spread_type: str,
    stars: int,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
):
    asyncio.create_task(_log(user_id, spread_type, stars, input_tokens, output_tokens, latency_ms))

async def _log(
    user_id: int,
    spread_type: str,
    stars: int,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
):
    try:
        from database import pool
        async with pool.acquire() as conn: # type: ignore
            await conn.execute("""
                INSERT INTO castings
                    (user_id, spread_type, stars, input_tokens, output_tokens, latency_ms)
                VALUES ($1, $2, $3, $4, $5, $6)
            """, user_id, spread_type, stars, input_tokens, output_tokens, latency_ms)
    except Exception as e:
        print(f"Analytics error: {e}")