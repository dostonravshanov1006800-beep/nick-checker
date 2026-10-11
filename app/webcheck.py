"""Веб-движок проверок: t.me + Fragment.

Работает по HTTPS, когда MTProto поймал FloodWait: t.me показывает
аватар/имя только у привязанных ников, Fragment — страницу лота
только для коллекционных ников. Скорость не зависит от лимитов
MTProto, флуд-баны Telegram не действуют на этот канал.
"""
import aiohttp

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
# у привязанного ника t.me всегда рисует фото или заголовок с именем;
# у свободного - только иконку-заглушку
TME_TAKEN = ("tgme_page_photo", "tgme_page_title")
# страница лота Fragment (аукцион/продан) содержит эти строки;
# заглушка «ник не найден» - нет
FRAG_TAKEN = ("Minimum bid", "Telegram Username", "Your first bid")


class WebEngine:
    """Проверка ника через веб: t.me, при сомнении - Fragment."""

    def __init__(self, transport=None):
        # transport: async (url) -> html; для тестов подменяется
        self._transport = transport
        self._sess: aiohttp.ClientSession | None = None

    async def _get(self, url: str) -> str:
        if self._transport is not None:
            return await self._transport(url)
        if self._sess is None:
            self._sess = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25))
        async with self._sess.get(url, headers=UA) as r:
            return await r.text()

    async def check(self, username: str) -> dict:
        name = username.strip().lstrip("@").lower()
        try:
            h = await self._get(f"https://t.me/{name}")
            if any(m in h for m in TME_TAKEN):
                return {"username": name, "status": "taken", "error": None,
                        "owner": None, "source": "web"}
            h = await self._get(f"https://fragment.com/username/{name}")
            if any(m in h for m in FRAG_TAKEN):
                return {"username": name, "status": "taken", "error": None,
                        "owner": None, "source": "web"}
            return {"username": name, "status": "free", "error": None,
                    "owner": None, "source": "web"}
        except Exception as e:
            return {"username": name, "status": "error",
                    "error": f"веб-проверка не удалась: {type(e).__name__}",
                    "owner": None, "source": "web"}

    async def close(self):
        if self._sess:
            await self._sess.close()
            self._sess = None
