"""Обработчики бота: прямые вызовы с моком движка (спека §11)."""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("APP_URL", "https://example.com/")

from app import botmode, checker, queue  # noqa


class FakeClient:
    async def __call__(self, req):
        name = getattr(req, "username", "")
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        if isinstance(req, ResolveUsernameRequest):
            if name == "durov":
                u = types.SimpleNamespace(first_name="Дуров", last_name="", username="durov", bot=False)
                return types.SimpleNamespace(users=[u], chats=[])
            raise __import__("telethon").errors.UsernameNotOccupiedError(request=None)
        return name.startswith("free")


class FakeUser:
    id = 42


class FakeMsg:
    from_user = FakeUser()
    chat = types.SimpleNamespace(id=100)

    def __init__(self, text="", web=None):
        self.text = text
        self.web_app_data = web
        self.replies = []

    async def answer(self, text, **kw):
        self.replies.append(text)
        return types.SimpleNamespace(message_id=1)

    async def edit_message_text(self, *a, **kw):  # для progress_updater
        pass


import pytest


@pytest.fixture(autouse=True)
def _setup():
    botmode.engine = checker.Engine(FakeClient())
    botmode.cq = queue.CheckQueue(botmode.engine)


def test_start_banner():
    m = FakeMsg("/start")
    asyncio.run(botmode.cmd_start(m))
    r = m.replies[0]
    assert "NICK CHECKER" in r and "Свободен" in r and "Занят" in r


def test_single_taken():
    m = FakeMsg("@durov")
    async def go():
        await botmode.cq.start()
        await botmode.on_text(m)
    asyncio.run(go())
    assert "🔴" in m.replies[0] and "Дуров" in m.replies[0]


def test_single_invalid():
    m = FakeMsg("ab")
    async def go():
        await botmode.cq.start()
        await botmode.on_text(m)
    asyncio.run(go())
    assert "короткий" in m.replies[0]


def test_list_starts_job():
    m = FakeMsg("durov, freenick1, freenick2")
    async def go():
        await botmode.cq.start()
        await botmode.on_text(m)
        await asyncio.sleep(0.3)
    asyncio.run(go())
    assert "список · 3" in m.replies[0]
    j = botmode.cq.job
    assert j and j.counts["taken"] >= 1


def test_pattern_detected():
    m = FakeMsg("ka??a")
    async def go():
        await botmode.cq.start()
        await botmode.on_text(m)
        await asyncio.sleep(0.2)
    asyncio.run(go())
    assert "шаблон ka??a" in m.replies[0]


def test_webapp_single():
    w = types.SimpleNamespace(data='{"type":"single","username":"durov"}')
    m = FakeMsg(None, web=w)
    async def go():
        await botmode.cq.start()
        await botmode.on_web_app(m)
    asyncio.run(go())
    assert "🔴" in m.replies[0]


def test_unavailable_engine_honest():
    m = FakeMsg("durov")
    botmode.engine = checker.Engine(None)  # сессии нет
    async def go():
        await botmode.cq.start()
        await botmode.on_text(m)
    asyncio.run(go())
    assert "не привязан" in m.replies[0]


def test_auto_search_stops_and_saves():
    """Авто-поиск: стоп на первом свободном + автосохранение в избранное."""
    from app import db

    cq = botmode.cq
    items = ["taken1", "taken2", "free_nick", "free2"]

    async def go():
        await cq.start()
        job = await cq.submit(42, items, "auto", 0, len(items), stop_on_free=True, auto_save=True)
        for _ in range(100):
            await asyncio.sleep(0.02)
            if job.status in ("done", "stopped"):
                break
        return job

    job = asyncio.run(go())
    assert job.status == "done"
    assert job.free_recent == ["free_nick"]
    assert "free_nick" in [f["username"] for f in db.favorites(42)]
    # после свободного ник не перебирает дальше
    assert job.done == 3
