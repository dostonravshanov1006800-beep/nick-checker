"""Одна очередь: один запрос за раз, пауза, FloodWait, повторы (спека §6)."""
import asyncio
import time

from telethon import errors

from . import checker, db, rules, webcheck

NET_RETRIES = 3
FLOOD_MARGIN = 60          # запас к паузе, который просит Telegram
JITTER = (1.0, 1.8)        # случайный множитель паузы (не робот, а человек)
MAX_DELAY_FACTOR = 4.0     # потолок адаптивного замедления после флуда


class Job:
    def __init__(self, user_id: int, items, label: str = "", delay: float = 1.5, total: int | None = None,
                 stop_on_free: bool = False, auto_save: bool = False):
        self.stop_on_free = stop_on_free   # авто-поиск: стоп на первом свободном
        self.auto_save = auto_save         # сохранить свободный ник в избранное
        self.user_id = user_id
        self.items = iter(items)
        self.label = label
        self.delay = max(0.3, float(delay))
        self.total_hint = total
        self.done = 0
        self.counts = {"free": 0, "taken": 0, "unavailable": 0, "error": 0}
        self.free_recent: list[str] = []
        self.last_results: list[dict] = []
        self.status = "queued"      # queued|running|paused|flood|stopped|done
        self.note = ""
        self.flood_until = 0.0
        self.started_at = time.time()
        self.finished_at: float | None = None

    def snapshot(self) -> dict:
        eta = 0
        if self.status in ("running", "queued", "flood", "paused") and self.total_hint:
            eta = max(0, int((self.total_hint - self.done) * self.delay))
        return {
            "user_id": self.user_id, "label": self.label, "status": self.status,
            "done": self.done, "total": self.total_hint,
            "counts": self.counts, "eta_sec": eta, "note": self.note,
            "flood_wait_left": max(0, int(self.flood_until - time.time())),
            "free_recent": self.free_recent[-10:],
            "last_results": self.last_results[-30:],
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


class CheckQueue:
    """Единственная очередь на процесс. Подписчики получают события (SSE/бот)."""

    def __init__(self, engine: checker.Engine, web_engine: webcheck.WebEngine | None = None):
        self.engine = engine
        self.web_engine = web_engine    # веб-фолбэк на время MTProto-бана
        self.web_until = 0.0             # до какого момента идём через веб
        self.job: Job | None = None
        self.delay_factor = 1.0     # адаптивное замедление: растёт после каждого флуда
        self._subscribers: list[asyncio.Queue] = []
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # --- события ---
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def _publish(self, event: dict):
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # --- жизненный цикл воркера ---
    async def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._worker())
        return self._task

    async def submit(self, user_id: int, items, label="", delay=1.5, total=None,
                     stop_on_free=False, auto_save=False) -> Job:
        old = self.job
        if old and old.status in ("queued", "running", "paused", "flood"):
            old.status = "stopped"
            old.note = "заменена новой очередью"
        self.job = Job(user_id, items, label, delay, total, stop_on_free, auto_save)
        self._wake.set()
        await self._publish({"type": "job", "job": self.job.snapshot()})
        return self.job

    def control(self, action: str) -> bool:
        j = self.job
        if not j:
            return False
        if action == "pause" and j.status in ("running", "queued"):
            j.status = "paused"
            j.note = "пауза"
        elif action == "resume" and j.status == "paused":
            j.status = "running"
            j.note = ""
            self._wake.set()
        elif action == "stop" and j.status in ("running", "queued", "paused", "flood"):
            j.status = "stopped"
            j.note = "остановлена"
            self._wake.set()
        else:
            return False
        return True

    # --- одиночная проверка (с кэшем 10 минут, спека §7) ---
    async def check_one(self, user_id: int, username: str) -> dict:
        cached = db.cached(username, user_id)
        if cached:
            return {"username": cached["username"], "status": cached["status"],
                    "owner": {"type": cached["owner_type"], "name": cached["owner_name"]} if cached["owner_type"] else None,
                    "error": cached["error"], "from_cache": True, "checked_at": cached["checked_at"]}
        r = await self._check(user_id, username)
        db.save_check(user_id, r)
        return r

    async def _check(self, user_id: int, username: str) -> dict:
        """Реальная проверка: FloodWait прокидывается, сеть — до 3 повторов."""
        if self.web_engine is not None and time.time() < self.web_until:
            return await self.web_engine.check(username)
        last_reason = "сетевая ошибка"
        for attempt in range(NET_RETRIES):
            try:
                return await self.engine.check(username)
            except checker.EngineUnavailable:
                return {"username": rules.normalize(username), "status": checker.ERROR,
                        "error": "аккаунт-проверяльщик не привязан — нужен вход (python login.py)",
                        "owner": None}
            except errors.FloodWaitError as e:
                # MTProto в бане: НЕ останавливаемся - уходим в веб
                # (t.me + Fragment) до конца бана и продолжаем проверять
                if self.web_engine is not None:
                    self.web_until = time.time() + e.seconds + FLOOD_MARGIN
                    self.delay_factor = min(MAX_DELAY_FACTOR, self.delay_factor * 1.5)
                    if self.job:
                        self.job.note = (f"MTProto-бан {e.seconds // 60} мин — "
                                          f"проверяю через веб (t.me + Fragment)")
                        await self._publish({"type": "job", "job": self.job.snapshot()})
                    return await self.web_engine.check(username)
                raise
            except Exception as e:
                last_reason = getattr(e, "message", None) or type(e).__name__
                await asyncio.sleep(1 + attempt * 2)
        return {"username": rules.normalize(username), "status": checker.ERROR,
                "error": f"не удалось проверить после {NET_RETRIES} попыток: {last_reason}", "owner": None}

    # --- воркер очереди ---
    async def _worker(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            job = self.job
            if not job or job.status != "queued":
                continue
            job.status = "running"
            await self._publish({"type": "job", "job": job.snapshot()})

            pending = None
            while True:
                # пауза
                if job.status == "paused":
                    await self._wake.wait()
                    self._wake.clear()
                if job.status == "stopped":
                    break
                # FloodWait: стоим до конца паузы, потом ПОВТОРЯЕМ тот же ник
                if job.status == "flood":
                    await asyncio.sleep(min(2, max(0.5, job.flood_until - time.time())))
                    if time.time() >= job.flood_until and job.status == "flood":
                        job.status = "running"
                        job.note = ""
                    continue
                # берём следующий
                if pending is None:
                    try:
                        pending = next(job.items)
                    except StopIteration:
                        job.status = "done"
                        break
                username = rules.normalize(pending if isinstance(pending, str) else str(pending))
                try:
                    r = await self._check(job.user_id, username)
                except errors.FloodWaitError as e:
                    self.handle_flood(e.seconds)
                    await self._publish({"type": "job", "job": job.snapshot()})
                    continue  # pending остаётся — повтор после паузы
                pending = None
                db.save_check(job.user_id, r)
                job.done += 1
                if r["status"] in job.counts:
                    job.counts[r["status"]] += 1
                if r["status"] == "free":
                    job.free_recent.append(r["username"])
                    if job.auto_save:
                        db.add_favorite(job.user_id, r["username"])
                job.last_results.append({k: r.get(k) for k in ("username", "status", "error", "owner")})
                if r["status"] == "free" and job.stop_on_free:
                    job.status = "done"
                    job.note = f"🟢 найден свободный @{r['username']}"
                    await self._publish({"type": "result", "result": r, "job": job.snapshot()})
                    pending = None
                    break
                await self._publish({"type": "result", "result": r, "job": job.snapshot()})
                if job.total_hint and job.done >= job.total_hint:
                    job.status = "done"
                    break
                import random as _r
                await asyncio.sleep(job.delay * self.delay_factor * _r.uniform(*JITTER))
            job.finished_at = job.finished_at or time.time()
            await self._publish({"type": "job", "job": job.snapshot()})

    def handle_flood(self, seconds: int):
        """Вся очередь на паузу при FloodWait: ЖДЁМ и продолжаем сами.

        Никогда не останавливаем очередь из-за флуда — бот обязан
        пережидать бан и продолжать (переживает и рестарты GitHub Actions).
        После каждого флуда темп замедляется в 1.5 раза (потолок 4x),
        чтобы флуды не повторялись.
        """
        j = self.job
        if not j:
            return
        wait = seconds + min(FLOOD_MARGIN, max(1, seconds // 10))
        j.status = "flood"
        j.flood_until = time.time() + wait
        self.delay_factor = min(MAX_DELAY_FACTOR, self.delay_factor * 1.5)
        hrs = seconds / 3600
        j.note = (f"Telegram просит ждать {seconds // 60} мин ({hrs:.1f} ч) — "
                  f"встаю на паузу и продолжу сам, темп замедляю")
