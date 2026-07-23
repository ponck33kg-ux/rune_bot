from datetime import date


ZODIAC_RANGES = [
    ("capricorn", (12, 22), (1, 19)),
    ("aquarius",  (1, 20),  (2, 18)),
    ("pisces",    (2, 19),  (3, 20)),
    ("aries",     (3, 21),  (4, 19)),
    ("taurus",    (4, 20),  (5, 20)),
    ("gemini",    (5, 21),  (6, 20)),
    ("cancer",    (6, 21),  (7, 22)),
    ("leo",       (7, 23),  (8, 22)),
    ("virgo",     (8, 23),  (9, 22)),
    ("libra",     (9, 23),  (10, 22)),
    ("scorpio",   (10, 23), (11, 21)),
    ("sagittarius", (11, 22), (12, 21)),
]


def get_zodiac_sign(birth_date: date) -> str:
    """
    Вычислить знак зодиака по дате рождения. Возвращает код на английском
    (aries, taurus...) — не локализованный текст, чтобы маркетинг мог
    сам решить, как и на каком языке его показывать.
    Используется ТОЛЬКО в маркетинговых целях — никогда не используется
    для гадания и не передаётся в промпт GPT.
    """
    m, d = birth_date.month, birth_date.day
    for sign, (start_m, start_d), (end_m, end_d) in ZODIAC_RANGES:
        if start_m == end_m:
            if m == start_m and start_d <= d <= end_d:
                return sign
        elif start_m < end_m:
            if (m == start_m and d >= start_d) or (m == end_m and d <= end_d):
                return sign
        else:  # диапазон через границу года (capricorn: дек→янв)
            if (m == start_m and d >= start_d) or (m == end_m and d <= end_d):
                return sign
    return "capricorn"  # защита на случай пропуска диапазона, не должно происходить