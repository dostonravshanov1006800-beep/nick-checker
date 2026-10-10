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
