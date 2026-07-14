import yaml
from constants import SUPPORTED_LANGUAGES

_UI: dict[str, dict] = {}
_PROMPTS: dict[str, dict] = {}
_SYSTEM_PROMPTS: dict[str, str] = {}
_RUNES: dict[str, dict] = {}


def load_i18n():
    """
    Загружает все языковые файлы (ui/, prompts/, runes/) в память.
    Вызывается один раз при старте бота, до обработки любых Update'ов.
    """
    for lang in SUPPORTED_LANGUAGES:
        with open(f"Casting/ui/{lang}.yaml", encoding="utf-8") as f:
            _UI[lang] = yaml.safe_load(f)

        with open(f"Casting/prompts/{lang}.yaml", encoding="utf-8") as f:
            prompts_data = yaml.safe_load(f)
            _PROMPTS[lang] = prompts_data["prompts"]
            _SYSTEM_PROMPTS[lang] = prompts_data["system"]

        with open(f"Casting/runes/{lang}.yaml", encoding="utf-8") as f:
            _RUNES[lang] = yaml.safe_load(f)["runes"]


def t(lang: str, key: str, **kwargs) -> str:
    """
    Вернуть UI-текст по ключу на нужном языке.
    Фолбэк на 'ru', если язык не поддерживается или ключ не найден.
    """
    lang_data = _UI.get(lang) or _UI["ru"]
    template = lang_data.get(key)
    if template is None:
        template = _UI["ru"].get(key, key)
    return template.format(**kwargs) if kwargs else template


def get_prompt_data(lang: str) -> dict:
    """Вернуть словарь шаблонов промптов (single/triple/five/greetings) для языка."""
    return _PROMPTS.get(lang) or _PROMPTS["ru"]


def get_system_prompt(lang: str) -> str:
    """Вернуть системный промпт для языка."""
    return _SYSTEM_PROMPTS.get(lang) or _SYSTEM_PROMPTS["ru"]


def get_runes_data(lang: str) -> dict:
    """Вернуть словарь рун (name/tags/tags_reversed) для языка."""
    return _RUNES.get(lang) or _RUNES["ru"]


def _ru_coin_form(n: int) -> str:
    """
    Склонение слова 'монета' по правилам русского языка.
    1, 21, 31... -> монета
    2-4, 22-24... -> монеты
    0, 5-20, 25-30... -> монет
    """
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return "coin_form_one"
    if 2 <= n_abs % 10 <= 4 and not (12 <= n_abs % 100 <= 14):
        return "coin_form_few"
    return "coin_form_many"


def format_coins(n: int, lang: str) -> str:
    """
    Вернуть строку "{n} <форма слова>" с правильным склонением для языка.
    ru: 3 формы (1 / 2-4 / 5+), en/pt: 2 формы (1 / остальное).
    """
    if lang == "ru":
        form_key = _ru_coin_form(n)
    else:
        form_key = "coin_form_one" if abs(n) == 1 else "coin_form_many"

    word = t(lang, form_key)
    return f"{n} {word}"


_REVERSED_YES = {"ru": "да", "en": "yes", "pt": "sim"}
_REVERSED_NO  = {"ru": "нет", "en": "no", "pt": "não"}


def reversed_word(lang: str, is_reversed: bool) -> str:
    """
    Слово 'да'/'нет' (или его эквивалент) для плейсхолдера {runeN_reversed}
    внутри промпта GPT. Это не UI-текст интерфейса, а часть заполнения
    шаблона промпта, поэтому не вынесено в ui/*.yaml.
    """
    d = _REVERSED_YES if is_reversed else _REVERSED_NO
    return d.get(lang, d["ru"])