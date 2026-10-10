"""Разовый вход в Telegram-аккаунт-проверяльщик (спека §10.3).

Запуск: python login.py
1) Спросит api_id / api_hash (или возьмёт из .env).
2) Спросит телефон, код из Telegram, пароль 2FA (если включён).
3) Сохранит сессию в data/checker.session и напечатает StringSession
   для GitHub Secrets (SESSION) — чтобы бот работал на Actions.

Рекомендация спеки: используйте ОТДЕЛЬНЫЙ аккаунт-проверяльщик.
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


async def main():
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = os.environ.get("API_ID") or input("api_id: ").strip()
    api_hash = os.environ.get("API_HASH") or input("api_hash: ").strip()
    os.makedirs("data", exist_ok=True)
    client = TelegramClient("data/checker.session", int(api_id), api_hash)
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Вошли как: {me.first_name} (@{getattr(me, 'username', '—')})\n")
    s = StringSession.save(client.session)
    print("SESSION для GitHub Secrets (одна строка, хранить как секрет):\n")
    print(s)
    print("\nСессия сохранена в data/checker.session")
    await client.disconnect()


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv  # noqa
    except ImportError:
        pass
    asyncio.run(main())
