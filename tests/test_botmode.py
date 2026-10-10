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

    kwargs: list = None

    async def answer(self, text, **kw):
        if self.kwargs is None:
            self.kwargs = []
        self.kwargs.append(kw)
        self.replies.append(text)
        return types.SimpleNamespace(message_id=1)

    async def edit_message_text(self, *a, **kw):  # для progress_updater
        pass


import pytest


@pytest.fixture(autouse=True)
def _setup():
    botmode.engine = checker.Engine(FakeClient())
    botmode.cq = queue.CheckQueue(botmode.engine)


def _cmd(args=None):
    return types.SimpleNamespace(args=args)


def _link(d):
    import base64, json
    return "j_" + base64.urlsafe_b64encode(json.dumps(d, ensure_ascii=False).encode()).decode().rstrip("=")


def test_start_banner_without_keyboard():
    m = FakeMsg("/start")
    asyncio.run(botmode.cmd_start(m, _cmd()))
    r = m.replies[0]
    assert "NICK CHECKER" in r and "Свободен" in r and "Занят" in r
    from aiogram.types import ReplyKeyboardRemove
    assert isinstance(m.kwargs[0].get("reply_markup"), ReplyKeyboardRemove)   # нижняя панель убрана


def test_deeplink_single():
    m = FakeMsg("/start")
    asyncio.run(botmode.cmd_start(m, _cmd(_link({"t": "single", "u": "durov"}))))
    assert any("durov" in r and "занят" in r for r in m.replies)


def test_deeplink_auto_finds_free():
    m = FakeMsg("/start")
    async def go():
        await botmode.cq.start()
        await botmode.cmd_start(m, _cmd(_link({"t": "job", "m": "auto", "b": "freeabc", "n": 5})))
        for _ in range(60):
            await asyncio.sleep(0.2)
            if any("Найден свободный" in r for r in m.replies):
                break
    asyncio.run(go())
    assert any("Найден свободный" in r for r in m.replies)


def test_deeplink_list_compact():
    m = FakeMsg("/start")
    async def go():
        await botmode.cq.start()
        await botmode.cmd_start(m, _cmd(_link({"t": "job", "m": "list", "x": "durov,freeone,freetwo", "n": 5000})))
        await asyncio.sleep(0.3)
    asyncio.run(go())
    assert any("список" in r for r in m.replies)


def test_deeplink_garbage():
    m = FakeMsg("/start")
    asyncio.run(botmode.cmd_start(m, _cmd("j_!!!не-base64")))
    assert any("Не удалось" in r for r in m.replies)


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
