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
            if any("Свободных найдено" in r for r in m.replies):
                break
    asyncio.run(go())
    assert any("Свободных найдено" in r for r in m.replies)


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


def test_auto_checks_all_variants_by_default():
    """Авто-поиск без «стоп на первом» проверяет все варианты и собирает все свободные."""
    m = FakeMsg("/auto freeabc 25")
    async def go():
        await botmode.cq.start()
        await botmode.run_auto(m, "freeabc", limit=25, delay=0.3)
        for _ in range(150):
            await asyncio.sleep(0.2)
            if botmode.cq.job and botmode.cq.job.status == "done":
                break
    asyncio.run(go())
    j = botmode.cq.job
    assert j.status == "done" and j.done == 25
    assert j.counts["free"] > 1                       # собраны все свободные, не только первый
    assert len(j.last_results) == 25                  # каждый результат попал в ленту аппа


def test_auto_stop_first_keeps_result_in_feed():
    m = FakeMsg("/auto freeabc first")
    async def go():
        await botmode.cq.start()
        await botmode.run_auto(m, "freeabc", limit=25, delay=0.3, stop_first=True)
        for _ in range(100):
            await asyncio.sleep(0.2)
            if botmode.cq.job and botmode.cq.job.status == "done":
                break
    asyncio.run(go())
    j = botmode.cq.job
    assert j.done < 25 and j.counts["free"] == 1
    assert j.last_results[-1]["status"] == "free"     # найденный ник виден в ленте


def test_auto_variants_letters_only():
    """Авто-поиск: варианты только из букв и «_», никаких цифр и leet-подмен."""
    from app import generators
    for base in ("doston", "dostonxoja", "abc", "ka"):
        for v in generators.auto_variants(base, 5000):
            assert not any(c.isdigit() for c in v), f"цифра в варианте {v!r}"
    assert "d0570n" not in list(generators.auto_variants("doston", 5000))
    vs = list(generators.auto_variants("doston", 40))
    assert vs[0] == "doston" and len(vs) == 40


class AllFreeClient:
    """Движок, где свободно всё, что длиннее 4 символов."""
    async def __call__(self, req):
        name = getattr(req, "username", "")
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        if isinstance(req, ResolveUsernameRequest):
            raise __import__("telethon").errors.UsernameNotOccupiedError(request=None)
        return len(name) > 4


def test_hunt_letters_only_saves_and_lists():
    """Охота: все найденные сохраняются, список приходит, охота идёт дальше."""
    import app.botmode as bm
    old_engine, old_cq = bm.engine, bm.cq
    bm.engine = checker.Engine(AllFreeClient()); bm.cq = queue.CheckQueue(bm.engine)
    bm.HUNT_BATCH, bm.HUNT_DELAY = 5, 0.05
    from app import db as _db
    UID = 424200
    class HuntMsg(FakeMsg):
        class from_user:
            id = UID
        class chat:
            id = UID
    _db.hunt_set(UID, False, 0)
    m = HuntMsg("/hunt")
    async def go():
        await bm.cq.start()
        await bm.hunt_start(m)
        for _ in range(80):
            await asyncio.sleep(0.2)
            if any("Свободные ники" in r for r in m.replies):
                break
    asyncio.run(go())
    assert any("Охота запущена" in r for r in m.replies)
    assert any("Свободные ники" in r for r in m.replies), "список находок не прислан"
    favs = [f["username"] for f in _db.favorites(UID)]
    assert len(favs) >= 5 and all(u.isalpha() and 5 <= len(u) <= 7 for u in favs), favs
    assert _db.hunt_get(UID)["enabled"] == 1
    # остановка
    m2 = HuntMsg("/hunt off")
    asyncio.run(bm.hunt_stop(m2))
    import time as _t
    for _ in range(30):                     # watcher замечает стоп до 10 сек
        if _db.hunt_get(UID)["enabled"] == 0:
            break
        _t.sleep(0.5)
    assert _db.hunt_get(UID)["enabled"] == 0
    bm.HUNT_BATCH, bm.HUNT_DELAY = 400, 1.2
    bm.engine, bm.cq = old_engine, old_cq


def test_hunt_generator_no_digits_no_underscores():
    """Охота: только буквы (+ чуть «_»), без цифр, по возрастанию длины."""
    from app import generators
    vs = list(generators.hunt_candidates(300))
    assert len(vs) == len(set(vs)) == 300
    assert all(not any(c.isdigit() for c in u) for u in vs), "только буквы!"
    ls = [len(u) for u in vs]
    assert all(ls[i] <= ls[i + 1] for i in range(len(ls) - 1)), "по возрастанию!"
    assert 5 <= min(ls) and vs[0] == "amber"                  # с самых коротких
    big = list(generators.hunt_candidates(4000))
    assert any("_" in u for u in big), "варианты с «_» должны быть"
    assert all(all(c.isalpha() or c == "_" for c in u) for u in big)
    # исключение проверенных двигает охоту к длинным именам
    ex = set(vs)
    vs2 = list(generators.hunt_candidates(300, exclude=ex))
    assert not (set(vs2) & ex), "не должен повторять проверенное"
    assert len(vs2) == 300
