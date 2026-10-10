"""Пароль + cookie-сессия для локального режима (спека §3)."""
import hashlib
import hmac
import os
import secrets

COOKIE = "zc_auth"


def access_password() -> str:
    return os.environ.get("ACCESS_PASSWORD", "")


def _secret() -> bytes:
    # ключ сессии: случайный на запуск сервера (рестарт = новый вход, это ок для LAN)
    global _S
    try:
        return _S
    except NameError:
        pass
    _S = secrets.token_bytes(32)
    return _S


def make_token() -> str:
    return hmac.new(hashlib.sha256(access_password().encode()).digest(), _secret(), hashlib.sha256).hexdigest()


def check_token(token: str | None) -> bool:
    if not token or not access_password():
        return False
    return hmac.compare_digest(token, make_token())


def ok() -> bool:
    """Вход выполнен корректно?"""
    return bool(access_password())
