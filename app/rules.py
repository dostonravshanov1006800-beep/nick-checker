"""Правила юзернеймов Telegram. Валидация ДО любого запроса (спека §5)."""
import re

MIN_LEN, MAX_LEN = 4, 32
LINK_RE = re.compile(r"(?:https?://)?(?:t(?:elegram)?\.me/)", re.I)
BAD_CHARS_RE = re.compile(r"[^a-z0-9_]")


def normalize(raw: str) -> str:
    """Убирает @, ссылки t.me/..., приводит к нижнему регистру."""
    s = (raw or "").strip()
    s = LINK_RE.sub("", s)
    s = s.lstrip("@").strip()
    return s.lower()


def validate(raw: str):
    """(ok, причина|None). Причина — человекочитаемая, на русском."""
    u = normalize(raw)
    if not u:
        return False, "пустой ввод"
    if BAD_CHARS_RE.search(u):
        return False, "допустимы только латиница a-z, цифры 0-9 и знак _"
    if len(u) < MIN_LEN:
        return False, f"слишком короткий (минимум {MIN_LEN} символов)"
    if len(u) > MAX_LEN:
        return False, f"слишком длинный (максимум {MAX_LEN} символа)"
    if not u[0].isalpha():
        return False, "начинается не с буквы"
    if u.endswith("_"):
        return False, "заканчивается на _"
    if "__" in u:
        return False, "содержит __ (два подчёркивания подряд)"
    return True, None
