"""Движок проверки: contacts.ResolveUsername + account.CheckUsername (спека §1).

Реальные запросы к Telegram, никаких имитаций. Проверка, существует ли аккаунт
и можно ли взять ник, выполняется ТОЛЬКО через MTProto-методы.
"""
from telethon import errors
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.functions.account import CheckUsernameRequest

from . import rules

FREE = "free"
TAKEN = "taken"
UNAVAILABLE = "unavailable"
ERROR = "error"

STATUS_LABEL = {FREE: "свободен", TAKEN: "занят", UNAVAILABLE: "недоступен", ERROR: "ошибка"}


class EngineUnavailable(Exception):
    """Сессия проверяльщика не привязана."""


def _owner_from_resolve(res) -> dict | None:
    """Кому принадлежит занятый ник: тип + публичное имя. Без приватных данных."""
    try:
        u = [x for x in (res.users or []) if getattr(x, "username", None) is not None or True][0] if res.users else None
        if u is not None:
            name = " ".join(x for x in (getattr(u, "first_name", ""), getattr(u, "last_name", "")) if x)
            name = name or ("@" + u.username if getattr(u, "username", None) else "")
            return {"type": "бот" if getattr(u, "bot", False) else "пользователь", "name": name or "—"}
        c = (res.chats or [None])[0] if res.chats else None
        if c is not None:
            kind = "группа" if getattr(c, "megagroup", False) else "канал"
            return {"type": kind, "name": getattr(c, "title", "") or "—"}
    except Exception:
        pass
    return {"type": "неизвестно", "name": ""}


class Engine:
    """client — авторизованный TelegramClient (личный аккаунт-проверяльщик)."""

    def __init__(self, client=None):
        self.client = client

    @property
    def available(self) -> bool:
        return self.client is not None

    async def check(self, username: str) -> dict:
        ok, why = rules.validate(username)
        u = rules.normalize(username)
        if not ok:
            return {"username": u, "status": ERROR, "error": why, "owner": None}
        if not self.available:
            raise EngineUnavailable()
        # 1) Занят ли ник кем-то
        try:
            res = await self.client(ResolveUsernameRequest(username=u))
            own = _owner_from_resolve(res)
            return {"username": u, "status": TAKEN, "error": None, "owner": own}
        except errors.UsernameNotOccupiedError:
            pass  # ник не отображается ни у кого -> проверяем, можно ли взять
        # 2) Можно ли установить именно сейчас
        try:
            can = await self.client(CheckUsernameRequest(username=u))
        except errors.FloodWaitError:
            raise
        except errors.UsernameInvalidError:
            return {"username": u, "status": UNAVAILABLE, "error": None,
                    "owner": None}  # Telegram сам отклонил формат при проверке
        except errors.RPCError as e:
            return {"username": u, "status": ERROR, "error": _short_reason(e), "owner": None}
        return {"username": u, "status": FREE if can else UNAVAILABLE, "error": None, "owner": None}


def _short_reason(e: Exception) -> str:
    """Человекочитаемая причина ошибки, без трейсбеков (спека §8)."""
    if isinstance(e, errors.AuthUnregisteredError) or "auth" in str(type(e)).lower():
        return "сессия аккаунта разлогинена — нужен повторный вход (python login.py)"
    if isinstance(e, errors.RPCError):
        m = str(e.message or e).strip()
        return f"Telegram: {m[:120]}"
    return f"сетевая ошибка: {type(e).__name__}"
