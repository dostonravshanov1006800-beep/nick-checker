"""Генераторы кандидатов: список, шаблон, случайные, словарь (спека §4)."""
import itertools
import random
import string

from . import rules

MAX_LIST = 5000  # лимит списка из спеки

VOWELS = "aeiouy"
CONSONANTS = "bcdfghjklmnpqrstvwxz"


def parse_list(text: str) -> list[str]:
    """Пробел / запятая / точка с запятой / новая строка. Убирает @ и t.me-ссылки."""
    items = []
    for tok in __import__("re").split(r"[,\s;]+", (text or "")):
        t = rules.normalize(tok)
        if t and t not in items:
            items.append(t)
    return items[:MAX_LIST]


def pattern_candidates(pattern: str, limit: int = MAX_LIST, random_order: bool = False):
    """ka??a: ? — буква, # — цифра, * — буква или цифра. Ленивый перебор."""
    pat = rules.normalize(pattern)
    alphabets = []
    for ch in pat:
        if ch == "?":
            alphabets.append(string.ascii_lowercase)
        elif ch == "#":
            alphabets.append(string.digits)
        elif ch == "*":
            alphabets.append(string.ascii_lowercase + string.digits)
        elif ch.isalnum() or ch == "_":
            alphabets.append(ch)
        else:
            return  # недопустимый символ шаблона — пусто
    if random_order:
        total = 1
        for a in alphabets:
            total *= len(a)
        if total > 500_000:
            return  # защита от комбинаторного взрыва
        seen = set()
        while len(seen) < min(total, limit):
            pick = "".join(random.choice(a) for a in alphabets)
            if pick in seen:
                continue
            seen.add(pick)
            ok, _ = rules.validate(pick)
            if ok:
                yield pick
    else:
        n = 0
        for combo in itertools.product(*alphabets):
            cand = "".join(combo)
            ok, _ = rules.validate(cand)
            if ok:
                yield cand
                n += 1
                if n >= limit:
                    return


def _pronounceable(length: int) -> str:
    out = ""
    syll = 0
    while len(out) < length:
        c = random.choice(CONSONANTS) if syll % 2 == 0 else random.choice(VOWELS)
        if len(out) == 0:
            out = c.upper()
        else:
            out += c
        syll += 1
    return out[:length].lower()


def random_candidates(length: int, style: str, limit: int):
    """style: pronounceable | letters | letters_digits. Длина 5-32."""
    length = max(5, min(32, int(length or 6)))
    n = 0
    tried = set()
    while n < limit:
        if style == "pronounceable":
            cand = _pronounceable(length)
        elif style == "letters":
            cand = "".join(random.choice(string.ascii_lowercase) for _ in range(length))
        else:  # letters_digits: первый символ — буква
            cand = random.choice(string.ascii_lowercase) + "".join(
                random.choice(string.ascii_lowercase + string.digits) for _ in range(length - 1))
        if cand in tried:
            continue
        tried.add(cand)
        ok, _ = rules.validate(cand)
        if ok:
            yield cand
            n += 1


def dictionary_candidates(words: list[str], limit: int = MAX_LIST):
    """Слова из словаря, приведённые к правилам (a-z, 5-32, без __)."""
    n = 0
    for w in words:
        w = rules.normalize(w)
        ok, _ = rules.validate(w)
        if ok:
            yield w
            n += 1
            if n >= limit:
                return


# ---------- авто-поиск: умные варианты вокруг желаемого ника ----------
LEET = {"o": "0", "e": "3", "i": "1", "a": "4", "l": "1", "s": "5", "t": "7"}
PREFIX = ["the", "its", "real", "mr", "im", "only"]
SUFFIX = ["hub", "tg", "app", "x", "pro", "ly", "io", "_"]


def _leet(w: str) -> str:
    return "".join(LEET.get(c, c) for c in w)


def auto_variants(base: str, limit: int = 40):
    """Упорядоченные кандидаты вокруг базы: ТОЛЬКО буквы и «_», никаких цифр.

    Порядок = «насколько похоже на желаемый»: сам ник, подчёркивание,
    удвоение буквы, буквенные префиксы/суффиксы, сокращения.
    Всё валидируется правилами Telegram; дубликаты убираются.
    """
    b = rules.normalize(base)
    if not b:
        return
    seen: set[str] = set()
    queue: list[str] = []

    def push(w: str):
        if w and w not in seen:
            seen.add(w)
            queue.append(w)

    push(b)                                   # 1) точный
    push(f"{b}_")                             # 2) подчёркивание
    push(f"{b}{b[-1]}")                        # 3) удвоение последней буквы
    for p in PREFIX:                           # 4) буквенные префиксы
        push(f"{p}{b}"), push(f"{p}_{b}")
    for s in SUFFIX:                           # 5) буквенные суффиксы
        push(f"{b}{s}"), push(f"{b}_{s}")
    if len(b) > 5:                             # 6) сокращения
        push(b[:-1]), push(b[:-2])
    if len(b) >= 6:                            # 7) разделение подчёркиванием
        mid = len(b) // 2
        push(f"{b[:mid]}_{b[mid:]}")
    for p in PREFIX:                           # 8) префикс + суффикс
        for sf in ("", "x", "tg", "hub", "app"):
            push(f"{p}{b}{sf}")
    seen.clear()
    n = 0
    for w in queue:
        ok, _ = rules.validate(w)
        if ok:
            yield w
            n += 1
            if n >= max(1, limit):
                return
