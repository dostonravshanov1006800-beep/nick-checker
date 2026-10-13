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
    """/start отвечает баннером охоты без лишних команд."""
    import tempfile, os as _os
    from app import db as _db
    import app.botmode as bm
    tmp = tempfile.mkdtemp()
    _db.DB_PATH = _os.path.join(tmp, "t5.db"); _db._conn = None; _db.conn()
    sent = []
    class M:
        class from_user: id = 111   # не владелец: авто-охоты нет
        class chat: id = 111
        async def answer(self, text, **kw): sent.append(text)
        def __getattr__(self, n): return None
    async def go():
        await bm.cmd_start(M(), type("CO", (), {"args": ""})())
    asyncio.run(go())
    assert sent and "Охота за красивыми никами" in sent[0] and "/stop" in sent[0], sent







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
    """Охота: реальные английские слова, без цифр, строго по возрастанию длины."""
    from app import generators
    vs = list(generators.hunt_candidates(300))
    assert len(vs) == len(set(vs)) == 300
    assert all(not any(c.isdigit() for c in u) for u in vs), "только буквы!"
    assert all(u.isalpha() for u in vs), "короткие - вообще без «_»"
    ls = [len(u) for u in vs]
    assert all(l == 5 for l in ls), "охота начинается со ВСЕХ 5-буквенных"
    big = list(generators.hunt_candidates(100000))          # заявленная ёмкость
    assert len(big) == len(set(big)) == 100000
    bls = [len(u) for u in big]
    assert all(bls[i] <= bls[i + 1] for i in range(len(bls) - 1)), "по возрастанию!"
    assert all(5 <= l <= 12 for l in bls)
    full = list(generators.hunt_candidates(10**9))          # весь пул
    assert len(full) > 300000 and any("_" in u for u in full)
    assert len(set(full)) == len(full)
    # исключение проверенных двигает охоту вперёд
    ex = set(big)
    vs2 = list(generators.hunt_candidates(300, exclude=ex))
    assert not (set(vs2) & ex), "не должен повторять проверенное"
    assert len(vs2) == 300


# ---------- бренд-охота: p2p5<слово>, цель 100 ----------

def test_brand_candidates_pattern():
    from app import generators
    c = list(generators.brand_candidates("p2p", limit=12))
    assert c[:6] == ["p2p5uz", "p2p6uz", "p2p7uz", "p2p5uzb", "p2p6uzb", "p2p7uzb"], c[:6]
    assert all(x.startswith("p2p") for x in c)
    # одна цифра 5/6/7 между брендом и словом
    import re
    assert all(re.fullmatch(r"p2p[567][a-z]+", x) for x in c), c
    # без дублей
    assert len(c) == len(set(c))
    # exclude не повторяем
    c2 = list(generators.brand_candidates("p2p", limit=20, exclude=c[:10]))
    assert not (set(c[:10]) & set(c2))
    # лимит
    assert len(list(generators.brand_candidates("p2p", limit=3))) == 3
    # невалидный бренд - пусто
    assert list(generators.brand_candidates("")) == []


def test_brand_hunt_stops_at_target():
    """Охота p2p сама останавливается, найдя 100 свободных."""
    import tempfile, os as _os
    from app import db as _db
    from app import queue as _queue, checker as _checker, generators as _gen
    import app.botmode as bm
    from telethon.tl.functions.contacts import ResolveUsernameRequest

    tmp = tempfile.mkdtemp()
    _db.DB_PATH = _os.path.join(tmp, "t.db")
    _db._conn = None
    _db.conn()

    class BrandFree:
        async def __call__(self, req):
            if isinstance(req, ResolveUsernameRequest):
                raise __import__("telethon").errors.UsernameNotOccupiedError(request=None)
            return True

    bm.engine = _checker.Engine(BrandFree())
    bm.cq = _queue.CheckQueue(bm.engine)
    bm.HUNT_DELAY = 0.01
    bm.WATCH_POLL = 0.02
    old_batch = _gen.BRAND_BATCH
    _gen.BRAND_BATCH = 40   # 3 партии по 40 -> цель 100 закрывается на 3-й
    sent = []
    class M:
        class from_user: id = 42
        class chat: id = 42
        async def answer(self, text, **kw): sent.append(text)
        def __getattr__(self, n): return None

    async def _noop(msg=None, label=None):
        return None
    bm.report_job = _noop

    _db.hunt_set(42, True, 0, mode="p2p", target=100)

    async def go():
        await bm.cq.start()
        await bm._hunt_run(M(), 1)
        for _ in range(5000):   # ~4 мин запас: очередь держит минимум 0.3с/чек
            st = _db.hunt_get(42)
            job = bm.cq.job
            if job and job.status in ("done", "stopped") and not st["enabled"]:
                break
            await asyncio.sleep(0.05)
        _gen.BRAND_BATCH = old_batch
        st = _db.hunt_get(42)
        job = bm.cq.job
        # цель кумулятивна: прошлые партии + текущая = 100
        assert st["found"] >= 100, (st, dict(job.counts))
        assert job.status == "stopped", job.status
        assert not st["enabled"], "охота должна выключиться на цели"
        assert any("Цель достигнута" in s for s in sent), sent[-3:]
        favs = _db.favorites(42)
        assert len(favs) >= 100
        assert all(f["username"].startswith("p2p") for f in favs)
        # все находки соответствуют шаблону p2p[567]...
        import re as _re
        assert all(_re.fullmatch(r"p2p[567][a-z]+", f["username"]) for f in favs)
    asyncio.run(go())


# ---------- красивая охота: 5-8 букв, бренды мира ----------

def test_nice_candidates_brands_first_letters_only():
    from app import generators as _gen
    c = list(_gen.nice_candidates(limit=200))
    assert c, "пусто"
    # только буквы, длина 5-8
    assert all(w.isalpha() and w.islower() and 4 <= len(w) <= 8 for w in c), c[:20]
    # первый эшелон - бренды (алфавитный порядок внутри эшелона)
    brands_in = [w for w in c if w in set(_gen.NICE_BRANDS)]
    assert len(brands_in) == len(_gen.NICE_BRANDS), "бренды должны идти первыми и целиком"
    rest = [w for w in c if w not in set(_gen.NICE_BRANDS)]
    assert rest and all(len(w) <= 8 for w in rest)
    assert all(c.index(w) > c.index(brands_in[-1]) for w in rest[:3]), "словарь после брендов"
    # никаких цифр/подчёркиваний
    assert not any(w.isdigit() or "_" in w for w in c)
    # дубликатов нет
    assert len(c) == len(set(c))
    # exclude уважается
    c2 = list(_gen.nice_candidates(limit=250, exclude=c[:100]))
    assert not (set(c[:100]) & set(c2))
    # лимит точен
    assert len(list(_gen.nice_candidates(limit=5))) == 5


def test_nice_hunt_mode_runs():
    """'/hunt nice' запускает партию с брендовым эшелоном."""
    import tempfile, os as _os
    from app import db as _db, queue as _queue, checker as _checker, generators as _gen
    import app.botmode as bm
    from telethon.tl.functions.contacts import ResolveUsernameRequest

    tmp = tempfile.mkdtemp()
    _db.DB_PATH = _os.path.join(tmp, "t2.db"); _db._conn = None; _db.conn()

    class AllFree:
        async def __call__(self, req):
            if isinstance(req, ResolveUsernameRequest):
                raise __import__("telethon").errors.UsernameNotOccupiedError(request=None)
            return True

    bm.engine = _checker.Engine(AllFree())
    bm.cq = _queue.CheckQueue(bm.engine)
    bm.HUNT_BATCH, bm.HUNT_DELAY, bm.WATCH_POLL = 5, 0.01, 0.02
    old_nice = _gen.NICE_BRANDS
    _gen.NICE_BRANDS = ["apple", "gucci", "tokyo", "omega"]

    class M:
        class from_user: id = 77
        class chat: id = 77
        async def answer(self, text, **kw): return None
        def __getattr__(self, n): return None

    async def _noop(msg=None, label=None): return None
    bm.report_job = _noop
    _db.hunt_set(77, True, 0, mode="nice", target=0)

    async def go():
        await bm.cq.start()
        await bm._hunt_run(M(), 1)
        await asyncio.sleep(1.2)
        job = bm.cq.job
        assert job.label.startswith("красивые · партия 1"), job.label
        assert job.items  # партия жива
        checked = _db.checked_names(77)
        assert "apple" in checked and "gucci" in checked, f"бренды первыми: {sorted(checked)}"
        assert all(4 <= len(w) <= 8 and w.isalpha() for w in checked), checked
        _gen.NICE_BRANDS = old_nice
    asyncio.run(go())


def test_start_auto_hunts_for_owner():
    """/start владельца сам запускает красивую охоту; чужому — нет."""
    import tempfile, os as _os
    from app import db as _db, queue as _queue, checker as _checker
    import app.botmode as bm
    from aiogram.types import Message as _Msg

    tmp = tempfile.mkdtemp()
    _db.DB_PATH = _os.path.join(tmp, "t3.db"); _db._conn = None; _db.conn()

    bm.engine = _checker.Engine(None)          # engine_ok() -> unavailable(msg)
    bm.cq = _queue.CheckQueue(bm.engine)
    bm.WATCH_POLL = 0.02

    started = []
    async def _fake_hunt_start(msg, brand=None, nice=False):
        started.append((msg.from_user.id, nice))
    bm.hunt_start = _fake_hunt_start

    class Bot:
        async def __call__(self, chat_id, text=None, **kw):  # unavailable() шлёт сюда
            return None

    class M:
        class from_user: id = 0
        class chat: id = 0
        async def answer(self, text, **kw): return None
        def __getattr__(self, n): return None

    async def go():
        # владелец: охота не включена -> авто-старт
        m = M(); m.from_user = type("U", (), {"id": bm.OWNER_UID})(); m.chat = type("C", (), {"id": bm.OWNER_UID})()
        await bm.cmd_start(m, type("CO", (), {"args": ""})())
        assert started and started[-1] == (bm.OWNER_UID, True), started
        # теперь включена -> повторный /start не дублирует
        _db.hunt_set(bm.OWNER_UID, True, 0, mode="nice", target=0)
        await bm.cmd_start(m, type("CO", (), {"args": ""})())
        assert len(started) == 1, started
        # чужой юзер: ничего не запускается
        m2 = M(); m2.from_user = type("U", (), {"id": 111})(); m2.chat = type("C", (), {"id": 111})()
        await bm.cmd_start(m2, type("CO", (), {"args": ""})())
        assert len(started) == 1, started
    asyncio.run(go())


def test_stop_disables_hunt_and_queue():
    """/stop выключает и очередь, и режим охоты."""
    import tempfile, os as _os
    from app import db as _db, queue as _queue, checker as _checker, generators as _gen
    import app.botmode as bm
    tmp = tempfile.mkdtemp()
    _db.DB_PATH = _os.path.join(tmp, "t4.db"); _db._conn = None; _db.conn()
    bm.cq = _queue.CheckQueue(_checker.Engine(None))
    _db.hunt_set(42, True, 0, mode="nice", target=0)
    class M:
        class from_user: id = 42
        class chat: id = 42
        async def answer(self, text, **kw): return None
        def __getattr__(self, n): return None
    replies = []
    async def rec(text, **kw): replies.append(text)
    M.answer = staticmethod(rec)
    async def go():
        await bm.cmd_stop(M())
        st = _db.hunt_get(42)
        assert not st["enabled"], "охота должна выключиться"
        assert st["mode"] == "nice", "режим сохраняется для будущих /start"
        assert replies and "остановлена" in replies[-1]
    asyncio.run(go())
