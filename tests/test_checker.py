import sys, os, asyncio, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import errors
from app import checker, queue
from app.queue import CheckQueue


class FakeClient:
    """Мок Telethon-клиента — ТОЛЬКО для тестов (спека §11)."""

    def __init__(self, resolve_map=None, check_map=None, flood_first=None):
        self.resolve_map = resolve_map or {}
        self.check_map = check_map or {}
        self.flood_first = flood_first  # (username, seconds): один раз кинуть FloodWait
        self.calls = 0

    async def __call__(self, req):
        self.calls += 1
        name = getattr(req, "username", None)
        if self.flood_first and name == self.flood_first[0] and self.calls == 1:
            raise errors.FloodWaitError(request=None, capture=self.flood_first[1])
        if isinstance(req, __import__("telethon.tl.functions.contacts", fromlist=["ResolveUsernameRequest"]).ResolveUsernameRequest):
            if name in self.resolve_map:
                return self.resolve_map[name]
            raise errors.UsernameNotOccupiedError(request=None)
        if isinstance(req, __import__("telethon.tl.functions.account", fromlist=["CheckUsernameRequest"]).CheckUsernameRequest):
            return self.check_map.get(name, True)
        raise RuntimeError("unexpected request")


def user(first="Дуров", username="durov", bot=False):
    u = types.SimpleNamespace(first_name=first, last_name="", username=username, bot=bot)
    return types.SimpleNamespace(users=[u], chats=[])


def chat(title="Канал", megagroup=False):
    c = types.SimpleNamespace(title=title, megagroup=megagroup)
    return types.SimpleNamespace(users=[], chats=[c])


def test_status_taken_user():
    e = checker.Engine(FakeClient(resolve_map={"durov": user()}))
    r = asyncio.run(e.check("@durov"))
    assert (r["status"], r["owner"]["type"], r["owner"]["name"]) == ("taken", "пользователь", "Дуров")


def test_status_taken_bot_channel_group():
    e = checker.Engine(FakeClient(resolve_map={
        "bid_bot": user("Bot", "bid_bot", bot=True),
        "somechan": chat("Some Channel"),
        "somegrp": chat("Some Group", megagroup=True)}))
    assert asyncio.run(e.check("bid_bot"))["owner"]["type"] == "бот"
    assert asyncio.run(e.check("somechan"))["owner"]["type"] == "канал"
    assert asyncio.run(e.check("somegrp"))["owner"]["type"] == "группа"


def test_status_free_and_unavailable():
    e = checker.Engine(FakeClient(check_map={"freeeee": True, "reservvv": False}))
    assert asyncio.run(e.check("freeeee"))["status"] == "free"
    r = asyncio.run(e.check("reservvv"))
    assert r["status"] == "unavailable" and r["error"] is None  # без выдуманных причин


def test_invalid_never_hits_network():
    fc = FakeClient()
    e = checker.Engine(fc)
    r = asyncio.run(e.check("ab"))
    assert r["status"] == "error" and fc.calls == 0


def test_engine_unavailable():
    e = checker.Engine(None)
    q = CheckQueue(e)
    r = asyncio.run(q.check_one(1, "freshnick_zz"))
    assert r["status"] == "error" and "не привязан" in r["error"]


def test_queue_flow_and_counts():
    e = checker.Engine(FakeClient(resolve_map={"durov": user()}, check_map={"freeeee": True, "reservvv": False}))
    q = CheckQueue(e)
    async def go():
        await q.start()
        await q.submit(1, ["durov", "freeeee", "reservvv", "ab"], delay=0.01, total=4)
        for _ in range(200):
            if q.job and q.job.status in ("done", "stopped", "failed"):
                break
            await asyncio.sleep(0.02)
        return q.job.snapshot()
    snap = asyncio.run(go())
    assert snap["status"] == "done" and snap["done"] == 4
    assert snap["counts"] == {"free": 1, "taken": 1, "unavailable": 1, "error": 1}
    assert "freeeee" in snap["free_recent"]


def test_queue_floodwait_pauses_and_resumes():
    e = checker.Engine(FakeClient(resolve_map={"durov": user()}, flood_first=("durov", 1)))
    q = CheckQueue(e)
    async def go():
        await q.start()
        await q.submit(1, ["durov", "freeeee"], delay=0.01, total=2)
        saw_flood = False
        for _ in range(300):
            if q.job and q.job.status == "flood":
                saw_flood = True
            if q.job and q.job.status in ("done", "stopped"):
                break
            await asyncio.sleep(0.02)
        return saw_flood, q.job.snapshot()
    saw_flood, snap = asyncio.run(go())
    assert saw_flood, "очередь должна была встать на паузу при FloodWait"
    assert snap["done"] == 2 and snap["counts"]["taken"] == 1  # durov проверен ПОСЛЕ паузы


def test_queue_control():
    e = checker.Engine(FakeClient(check_map={"freeeee": True}))
    q = CheckQueue(e)
    assert q.control("pause") is False  # очереди ещё нет
    async def go():
        await q.start()
        await q.submit(1, ["freeeee", "freeeee", "freeeee"], delay=0.3, total=3)
        await asyncio.sleep(0.05)
        assert q.job.status == "running"
        q.control("pause")
        assert q.job.status == "paused"
        q.control("stop")
        await asyncio.sleep(0.2)
        return q.job.snapshot()
    snap = asyncio.run(go())
    assert snap["status"] == "stopped" and snap["done"] <= 1


# --- веб-движок (t.me + Fragment) ---

def test_web_engine_classifies():
    from app import webcheck

    class T:
        def __init__(self, pages): self.pages = pages
        async def __call__(self, url):
            for pat, body in self.pages.items():
                if pat in url: return body
            return ""

    taken_html = "<div class='tgme_page_photo'></div><div class='tgme_page_title'>X</div>"
    view_html = "<title>Telegram: View @afley</title><div class='tgme_page_icon'></div>"
    contact_html = "<title>Telegram: Contact @free</title><div class='tgme_page_icon'></div>"
    frag_html = "<label for='bid_value'>Minimum bid</label>"
    menu_html = "<div>Sold</div><a>On auction</a><b>Minimum bid</b>"   # меню любой страницы
    nochat = lambda n: _async(False)

    async def _noprobe(n): return False
    async def _chatprobe(n): return True

    async def go():
        # фото/имя -> занят
        e = webcheck.WebEngine(transport=T({"t.me/durov": taken_html}), chat_probe=_noprobe)
        r = await e.check("durov")
        assert r["status"] == "taken" and r["source"] == "web"
        # РЕГРЕССИЯ @afley: канал без фото - «View @» -> занят (раньше ложно free)
        e = webcheck.WebEngine(transport=T({"t.me/afley": view_html}), chat_probe=_noprobe)
        assert (await e.check("afley"))["status"] == "taken", "канал без фото = занят"
        # getChat нашёл канал, а t.me пустой -> занят
        e = webcheck.WebEngine(transport=T({"t.me/": contact_html}), chat_probe=_chatprobe)
        assert (await e.check("hidden"))["status"] == "taken", "getChat: канал найден"
        # Fragment: коллекционный ник
        e = webcheck.WebEngine(transport=T({"t.me/nike": contact_html,
                                            "fragment.com/username/nike": frag_html}), chat_probe=_noprobe)
        assert (await e.check("nike"))["status"] == "taken"
        # все три молчат -> свободен (кандидат)
        e = webcheck.WebEngine(transport=T({"t.me/": contact_html, "fragment.com/": "<html></html>"}),
                               chat_probe=_noprobe)
        r = await e.check("freeeee")
        assert r["status"] == "free" and r["source"] == "web"
        assert (await e.check("@Freeeee"))["status"] == "free", "нормализация @"
        # меню Fragment («Sold», «On auction») на странице СВОБОДНОГО ника - не занятость
        e = webcheck.WebEngine(transport=T({"t.me/": contact_html, "fragment.com/": menu_html}),
                               chat_probe=_noprobe)
        assert (await e.check("freeeee"))["status"] == "free", "меню Fragment не маркер"
        # сеть упала -> ошибка, а не ложный free
        class Boom:
            async def __call__(self, url): raise OSError("сеть")
        e = webcheck.WebEngine(transport=Boom(), chat_probe=_noprobe)
        r = await e.check("x")
        assert r["status"] == "error" and "веб" in r["error"]
    asyncio.run(go())


def test_web_mode_checks_in_parallel_fast():
    """Веб-режим: пачка из 20 проверок идёт параллельно, без пауз между ними."""
    import time as _t
    from app import webcheck, queue, checker as ck

    async def slow_transport(url):
        await asyncio.sleep(0.3)               # каждый запрос = 0.3 c
        return "<title>Telegram: Contact @x</title>"
    async def noprobe(n):
        await asyncio.sleep(0.3); return False

    class Floody:
        async def check(self, u):
            from telethon import errors as te
            raise te.FloodWaitError(request=None, capture=3600)

    async def go():
        import tempfile, os as _os
        from app import db as _db
        _db.DB_PATH = _os.path.join(tempfile.mkdtemp(), "p.db"); _db._conn = None; _db.conn()
        web = webcheck.WebEngine(transport=slow_transport, chat_probe=noprobe)
        cq = queue.CheckQueue(Floody(), web_engine=web)
        await cq.start()
        names = [f"qqzzy{c}{d}" for c in "abcdefgh" for d in "ghijk"][:40]
        t0 = _t.time()
        await cq.submit(1, iter(names), label="p", delay=1.5, total=40)
        for _ in range(200):
            await asyncio.sleep(0.1)
            if cq.job and cq.job.done >= 40: break
        dt = _t.time() - t0
        assert cq.job.done >= 40, cq.job.done
        # последовательно было бы 40 x 0.3 c = 12 c и паузы 1.5 c x 40 = 60+ c
        assert dt < 6, f"слишком медленно: {dt:.1f} c"
    asyncio.run(go())


def test_queue_flood_switches_to_web_and_continues():
    """MTProto-бан не останавливает очередь: проверка уходит в веб."""
    from app import webcheck

    class Floody:
        def __init__(self): self.calls = 0
        async def __call__(self, req):
            self.calls += 1
            raise errors.FloodWaitError(request=None, capture=3600)

    class Web:
        def __init__(self): self.calls = 0
        async def check(self, name):
            self.calls += 1
            return {"username": name, "status": "free", "error": None,
                    "owner": None, "source": "web"}

    e = checker.Engine(Floody())
    w = Web()
    q = CheckQueue(e, web_engine=w)

    async def go():
        await q.start()
        await q.submit(1, ["alpha", "bravo"], "тест", delay=0.01, total=2)
        for _ in range(400):
            if q.job and q.job.status == "done":
                break
            await asyncio.sleep(0.02)
        snap = q.job.snapshot()
        assert snap["done"] == 2, "очередь должна была дойти до конца через веб"
        assert snap["counts"]["free"] == 2
        assert w.calls == 2 and e.client.calls >= 1, "первый флуд, дальше веб"
        assert "веб" in snap["note"], "юзер должен видеть причину переключения"
        assert q.delay_factor == 1.5
    asyncio.run(go())
