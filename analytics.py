import asyncio

def log_casting(
    user_id: int,
    spread_type: str,
    stars: int,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    source: str = "bot",
    situation: str = "",
    answer_text: str = "",
    prompt_variant: str | None = None,
):
    asyncio.create_task(_log(user_id, spread_type, stars, input_tokens, output_tokens, latency_ms, source, situation, answer_text, prompt_variant))


async def _log(
    user_id: int,
    spread_type: str,
    stars: int,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    source: str,
    situation: str,
    answer_text: str,
    prompt_variant: str | None,
):
    try:
        from database import pool
        async with pool.acquire() as conn: # type: ignore
            await conn.execute("""
                INSERT INTO castings
                    (user_id, spread_type, stars, input_tokens, output_tokens, latency_ms, source, situation, response, prompt_variant)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """, user_id, spread_type, stars, input_tokens, output_tokens, latency_ms, source, situation, answer_text, prompt_variant)
    except Exception as e:
        print(f"Analytics error: {e}")