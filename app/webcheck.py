"""Веб-движок проверок: t.me + Fragment + Bot API getChat.

Работает по HTTPS, когда MTProto поймал FloodWait. Занятость определяют
независимые сигналы (достаточно любого):
  1) t.me: заголовок «View @x» (публичный канал/группа/бот) или фото/имя;
  2) Fragment: страница лота (Sold / On auction / Taken);
  3) Bot API getChat: находит публичные каналы и группы.
Свободным ник считается, только когда молчат все три. Это КАНДИДАТ:
пользователя без фото и имени Telegram в вебе не отдаёт, поэтому такие
ники перепроверяются через MTProto, как только бан закончится.
"""
import os

import aiohttp

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TME_TAKEN = ("tgme_page_photo", "tgme_page_title", "Telegram: View @")
# маркеры реальной страницы лота (проверено на живых данных: у свободного
# ника их нет; «Sold»/«On auction» лежат в меню ЛЮБОЙ страницы Fragment)
FRAG_TAKEN = ("tm-section-header-status", "Telegram Username", "bid_value", "Your first bid")


class WebEngine:
    """Проверка ника через веб; параллельные запросы, без пауз."""

    def __init__(self, transport=None, bot_token: str | None = None, chat_probe=None):
        # transport: async (url) -> html; chat_probe: async (name) -> bool|None (для тестов)
        self._transport = transport
        self._chat_probe = chat_probe
        self._token = bot_token if bot_token is not None else os.getenv("BOT_TOKEN", "")
        self._sess: aiohttp.ClientSession | None = None

    async def _session(self) -> aiohttp.ClientSession:
        if self._sess is None:
            self._sess = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                connector=aiohttp.TCPConnector(limit=40))
        return self._sess

    async def _get(self, url: str) -> str:
        if self._transport is not None:
            return await self._transport(url)
        s = await self._session()
        async with s.get(url, headers=UA) as r:
            return await r.text()

    async def _is_chat(self, name: str):
        """True - публичный канал/группа найдены, False - нет, None - сигнал недоступен."""
        if self._chat_probe is not None:
            return await self._chat_probe(name)
        if not self._token:
            return None
        s = await self._session()
        try:
            async with s.get(f"https://api.telegram.org/bot{self._token}/getChat",
                             params={"chat_id": f"@{name}"}) as r:
                d = await r.json()
            if d.get("ok"):
                return True
            if "chat not found" in (d.get("description") or ""):
                return False
        except Exception:
            pass
        return None

    async def check(self, username: str) -> dict:
        import asyncio
        name = username.strip().lstrip("@").lower()
        base = {"username": name, "error": None, "owner": None, "source": "web"}
        try:
            tme, frag, chat = await asyncio.gather(
                self._get(f"https://t.me/{name}"),
                self._get(f"https://fragment.com/username/{name}"),
                self._is_chat(name))
        except Exception as e:
            return {**base, "status": "error",
                    "error": f"веб-проверка не удалась: {type(e).__name__}"}
        if chat is True or any(m in tme for m in TME_TAKEN) or any(m in frag for m in FRAG_TAKEN):
            return {**base, "status": "taken"}
        return {**base, "status": "free"}

    async def close(self):
        if self._sess:
            await self._sess.close()
            self._sess = None
