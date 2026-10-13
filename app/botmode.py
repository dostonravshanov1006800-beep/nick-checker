"""Режим бота: живёт на GitHub Actions, проверяет ники и отвечает в чате.

Бот НЕ хранит деньги и не касается чужих транзакций — только проверка ников.
Прогресс очереди редактируется одним сообщением; данные для мини-аппа
коммитятся в ветку data каждые 60 сек.
"""
import asyncio
import base64
import json
import logging
import os
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo)

from . import db, generators, queue, sync, webcheck
from .checker import Engine, STATUS_LABEL

log = logging.getLogger("nc")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_URL = os.environ.get("APP_URL", "")

S = {"free": "🟢", "taken": "🔴", "unavailable": "🟡", "error": "⚪"}
EMO = {"free": "✅", "taken": "❌", "unavailable": "⚠️", "error": "⚪"}

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
cq: queue.CheckQueue | None = None
engine: Engine | None = None

# прогресс-сообщения: chat_id -> (message_id, next_edit_ts)
_prog: dict[int, list] = {}


def menu(uid: int | None = None) -> ReplyKeyboardMarkup:
    """Нижняя панель без кнопки мини-аппа — синяя кнопка открывает приложение (menu button)."""
    return ReplyKeyboardMarkup(
        is_persistent=True,
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text="⏹ Стоп"), KeyboardButton(text="⭐ Избранное")],
        ])


BANNER = (
    "💎 <b>Охота за красивыми никами</b>\n"
    "4-8 букв: мировые бренды + лучшие слова.\n"
    "Свободные ники сохраняются автоматически.\n\n"
    "Бот работает 24/7 — сам переживает баны и рестарты.\n"
    "⏹ Остановить: <code>/stop</code>\n"
)


# ---------- helpers ----------
def result_text(r: dict, uid: int) -> str:
    u = r["username"]
    base = f"{S.get(r['status'], '⚪')} <b>@{u}</b> — {STATUS_LABEL.get(r['status'], 'ошибка')}"
    if r.get("owner") and r["owner"].get("name"):
        base += f"\n👤 {r['owner']['type']}: {r['owner']['name']}"
    if r.get("error"):
        base += f"\n<i>{r['error']}</i>"
    if r.get("from_cache"):
        age = max(1, int((time.time() - (r.get("checked_at") or 0)) / 60))
        base += f"\n<i>из кэша, {age} мин назад</i>"
    return base


def prog_text(j) -> str:
    if not j:
        return "Очередей нет."
    snap = j.snapshot()
    c = snap["counts"]
    lines = [f"⏳ <b>{snap['label'] or 'Проверка'}</b> — {snap['status']}",
             f"Проверено: <b>{snap['done']}</b>" + (f" из {snap['total']}" if snap['total'] else ""),
             f"🟢 свободно: <b>{c['free']}</b> · 🔴 занято: <b>{c['taken']}</b> · 🟡 недоступно: <b>{c['unavailable']}</b> · ⚪ ошибок: <b>{c['error']}</b>"]
    if snap.get("eta_sec"):
        lines.append(f"⏱ " + (f"~{snap['eta_sec'] // 60} мин" if snap['eta_sec'] >= 60 else f"~{snap['eta_sec']} сек"))
    if snap.get("note"):
        lines.append(f"<i>{snap['note']}</i>")
    if snap["free_recent"]:
        lines.append("\nСвободные: " + ", ".join("@" + u for u in snap["free_recent"][-8:]))
    return "\n".join(lines)


async def progress_updater(chat_id: int):
    """Редактирует ОДНО сообщение прогресса, пока очередь жива."""
    while True:
        await asyncio.sleep(4)
        j = cq.job
        info = _prog.get(chat_id)
        if not info or not j:
            continue
        msg_id, next_edit = info
        if time.time() < next_edit:
            continue
        if j.user_id != _prog_user(chat_id):
            continue
        _prog[chat_id] = [msg_id, time.time() + 5]
        try:
            await bot.edit_message_text(prog_text(j), chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        if j.status in ("done", "stopped"):
            snap = j.snapshot()
            c = snap["counts"]
            tail = ""
            if snap["free_recent"]:
                tail = "\n\n🟢 Свободные:\n" + "\n".join("• @" + u for u in snap["free_recent"][-10:])
            try:
                await bot.edit_message_text(
                    prog_text(j) + tail + ("\n\n<i>Очередь остановлена</i>" if j.status == "stopped" else ""),
                    chat_id=chat_id, message_id=msg_id)
            except Exception:
                pass
            _prog.pop(chat_id, None)


def _prog_user(chat_id):
    return _prog_uids.get(chat_id)


_prog_uids: dict[int, int] = {}


async def report_job(msg: Message, label: str):
    m = await msg.answer(f"⏳ <b>{label}</b> — запускаю…")
    _prog[msg.chat.id] = [m.message_id, time.time() + 2]
    _prog_uids[msg.chat.id] = msg.from_user.id


# ---------- обработчики ----------
def expand_compact(d: dict) -> dict:
    """Компактный payload из deep-link -> полный формат run_payload."""
    if "t" not in d:
        return d
    out = {"type": d.get("t"), "mode": d.get("m")}
    for short, full in (("u", "username"), ("b", "base"), ("p", "pattern"), ("l", "length"),
                        ("s", "style"), ("n", "limit"), ("x", "text"), ("f", "stop_first")):
        if short in d:
            out[full] = d[short]
    if d.get("r"):
        out["random"] = True
    return out


def decode_job(token: str) -> dict | None:
    """j_<base64url(JSON)> из deep-link мини-аппа -> payload задания."""
    try:
        raw = token[2:] if token.startswith("j_") else token
        raw += "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(raw.encode()).decode("utf-8"))
        return expand_compact(data) if isinstance(data, dict) else None
    except Exception:
        return None


OWNER_UID = 8396883978   # владелец бота: /start автоматически включает охоту


@dp.message(CommandStart())
async def cmd_start(msg: Message, command: CommandObject):
    # убираем старую нижнюю клавиатуру у всех, кто её получал раньше
    await msg.answer(BANNER, reply_markup=ReplyKeyboardRemove())
    # владельцу: просто нажал /start — охота за красивыми никами идёт сама
    if msg.from_user.id == OWNER_UID:
        st = db.hunt_get(OWNER_UID)
        busy = cq.job and cq.job.status in ("queued", "running", "paused", "flood")
        if not busy and not (st and st["enabled"]):
            await hunt_start(msg, nice=True)



async def cmd_gen(msg: Message):
    if not engine_ok():
        await unavailable(msg)
        return
    parts = (msg.text or "").split()
    length = 6
    try:
        length = min(32, max(5, int(parts[1])))
    except Exception:
        pass
    style = "pronounceable" if "произнос" in (msg.text or "").lower() else \
            "letters_digits" if "цифр" in (msg.text or "").lower() else "letters"
    limit = 50
    try:
        limit = min(5000, max(1, int(parts[-1]))) if parts[-1].isdigit() and len(parts) > 2 else 50
    except Exception:
        pass
    items = generators.random_candidates(length, style, limit)
    await report_job(msg, f"Генератор · длина {length} · {limit} шт")
    await cq.submit(msg.from_user.id, items, f"генератор · длина {length}", 1.5, limit)
    asyncio.create_task(progress_updater(msg.chat.id))



async def cmd_auto(msg: Message):
    base = (msg.text or "").split(" ", 1)
    base = base[1].strip() if len(base) > 1 else ""
    if not base:
        await msg.answer("Формат: <code>/auto doston</code> — переберу варианты вокруг ника, найду свободный и сохраню в избранное.")
        return
    if not engine_ok():
        await unavailable(msg)
        return
    parts = (msg.text or "").split()
    limit = 40
    if len(parts) > 2 and parts[2].isdigit():
        limit = min(5000, max(1, int(parts[2])))
    await run_auto(msg, base, limit=limit, delay=1.5, stop_first="first" in (msg.text or "").lower())



async def cmd_frag(msg: Message):
    parts = (msg.text or "").split()
    if len(parts) < 2:
        await msg.answer("Формат: <code>/frag dost 200</code> — переберу ники, содержащие фрагмент, найду свободные и сохраню в избранное.")
        return
    if not engine_ok():
        await unavailable(msg)
        return
    limit = 200
    if len(parts) > 2 and parts[2].isdigit():
        limit = min(5000, max(1, int(parts[2])))
    await run_frag(msg, parts[1], limit=limit)


async def run_frag(msg: Message, frag: str, limit: int = 200, delay: float = 1.5):
    from . import rules as _r
    f = _r.normalize(frag)
    if len(f) < 2 or not all(c in "abcdefghijklmnopqrstuvwxyz_" for c in f):
        await msg.answer("⚠️ Фрагмент: 2+ символа, только a-z и _")
        return
    limit = min(5000, max(1, int(limit)))
    items = list(generators.fragment_candidates(f, limit))
    if not items:
        await msg.answer("⚠️ Не удалось построить варианты.")
        return
    await report_job(msg, f"фрагмент · {f} · {len(items)} вариантов")
    job = await cq.submit(msg.from_user.id, items, f"фрагмент · {f}", delay,
                         len(items), auto_save=True)
    asyncio.create_task(progress_updater(msg.chat.id))
    asyncio.create_task(auto_watcher(msg, job, len(items), kind="frag"))


HUNT_BATCH = 400          # вариантов за партию
HUNT_DELAY = 3.0          # пауза между проверками (× jitter 1.0-1.8 → 3-5.4 сек)
WATCH_POLL = 10   # как часто watcher смотрит счётчик (для тестов замедляем)
_hunt_report: dict[int, dict] = {}   # uid -> {found, ts, reported}



async def cmd_hunt(msg: Message):
    arg = (msg.text or "").lower().replace("/hunt", "").strip()
    if arg in ("off", "стоп", "stop", "выкл"):
        await hunt_stop(msg)
    elif arg in ("nice", "beauty", "красиво", "бренд"):
        await hunt_start(msg, nice=True)     # 5-8 букв: бренды мира + словарь
    elif arg and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in arg):
        await hunt_start(msg, brand=arg)      # /hunt p2p -> p2p5<слово>
    else:
        await hunt_start(msg)


async def hunt_start(msg: Message, brand: str | None = None, nice: bool = False):
    if not engine_ok():
        await unavailable(msg)
        return
    if (cq.job and cq.job.user_id == msg.from_user.id
            and cq.job.label.startswith("охота") and cq.job.status in ("queued", "running", "paused", "flood")):
        await msg.answer("🆓 Охота уже идёт. Остановить: <code>/hunt off</code>")
        return
    st = db.hunt_get(msg.from_user.id)
    batch = (st["batch"] if st else 0)
    brand = brand or (st or {}).get("mode")
    nice = nice or brand == "nice"
    if nice:
        brand = None
    target = int((st or {}).get("target") or 0)
    if brand and not target:
        target = generators.BRAND_TARGET
    mode = "nice" if nice else brand
    db.hunt_set(msg.from_user.id, True, batch, mode=mode, target=target)
    total_all = HUNT_BATCH * (batch + 1)
    eta = int(HUNT_BATCH * HUNT_DELAY / 60)
    if nice:
        await msg.answer(
            f"💎 <b>Охота за красивыми никами</b>\n"
            f"5-8 символов, ТОЛЬКО буквы.\n"
            f"Эшелон 1: мировые бренды и компании ({len(generators.NICE_BRANDS)}: apple, gucci, ferrari…).\n"
            f"Эшелон 2: лучшие словарные слова 5-8 букв.\n"
            f"Партия {batch + 1}: ~{HUNT_BATCH} вариантов, свободные — в ⭐ автоматически.\n"
            f"Работает 24/7: переживает баны (веб-проверки t.me + Fragment),\n"
            f"рестарты и флуд. Остановить: <code>/hunt off</code>")
    elif brand:
        await msg.answer(
            f"🆓 <b>Охота по бренду @{brand}</b>\n"
            f"Шаблон: <code>{brand}[цифра 5/6/7][слово]</code> — p2p5uz, p2p6ton…\n"
            f"Цель: {target} свободных, потом остановлюсь сам.\n"
            f"Проверяю партию {batch + 1} (~{generators.BRAND_BATCH} вариантов).\n"
            f"Можешь закрыть чат — находки буду присылать сюда.\n"
            f"Остановить: <code>/hunt off</code>")
    else:
        await msg.answer(
            f"🆓 <b>Охота запущена</b>\n"
            f"Ищу ники: настоящие английские слова, только буквы, без цифр.\n"
            f"Порядок: все 5-буквенные → все 6-буквенные → 7 и дальше.\n"
            f"Ёмкость словаря 300 000+ имён - хватит на сотни партий.\n"
            f"Партия {batch + 1}: ~{HUNT_BATCH} вариантов, примерно {eta} мин.\n"
            f"Проверено всего будет ~{total_all}, свободные сохраняются в ⭐ автоматически.\n\n"
            f"Можешь закрыть чат - охота идёт сама. Списки находок буду присылать сюда.\n"
            f"Если MTProto словит бан - перейду на веб-проверки (t.me + Fragment),\n"
            f"поиск не остановится. Баны пережидаю автоматически.\n"
            f"Остановить: <code>/hunt off</code>")
    await _hunt_run(msg, batch + 1)


async def _hunt_run(msg: Message, batch: int):
    cur = db.hunt_get(msg.from_user.id)
    if not (cur and cur["enabled"] and cur["batch"] + 1 == batch):
        return  # охота выключена или уже другая сессия - не запускаем
    brand = cur.get("mode")
    target = int(cur.get("target") or 0)
    exclude = db.checked_names(msg.from_user.id)
    if brand == "nice":
        items = list(generators.nice_candidates(HUNT_BATCH, exclude=exclude))
        if not items:
            items = list(generators.nice_candidates(HUNT_BATCH))
        label = f"красивые · партия {batch}"
    elif brand:
        items = list(generators.brand_candidates(brand, limit=generators.BRAND_BATCH,
                                                  exclude=exclude))
        if not items:
            items = list(generators.brand_candidates(brand, limit=generators.BRAND_BATCH))
        label = f"{brand}-охота · партия {batch}"
    else:
        items = list(generators.hunt_candidates(HUNT_BATCH, exclude=exclude))
        if not items:                            # всё свежее проверено - перепроверяем старое
            items = list(generators.hunt_candidates(HUNT_BATCH))
        label = f"охота · партия {batch}"
    await report_job(msg, label)
    job = await cq.submit(msg.from_user.id, items, label,
                         HUNT_DELAY, len(items), auto_save=True)
    _hunt_report.setdefault(msg.from_user.id, {"found": 0, "ts": time.time(), "reported": 0})
    asyncio.create_task(progress_updater(msg.chat.id))
    asyncio.create_task(hunt_watcher(msg, job, batch, target=target, poll=WATCH_POLL))


async def hunt_watcher(msg: Message, job, batch: int, target: int = 0, poll: float = 10):
    """Пока партия идёт: раз в 15 мин или каждые 10 находок шлёт список; после конца - следующая партия."""
    uid = msg.from_user.id
    st = _hunt_report.setdefault(uid, {"found": 0, "ts": time.time(), "reported": 0})
    found0 = int((db.hunt_get(uid) or {}).get("found") or 0)   # накоплено прошлыми партиями
    while job.status in ("queued", "running", "paused", "flood"):
        await asyncio.sleep(poll)
        new = job.counts["free"] - st["reported"]
        if new > 0:                              # каждый свободный ник - сразу в чат
            since = st["reported"]
            st["reported"] = job.counts["free"]
            st["ts"] = time.time()
            await _hunt_send_list(msg, job, since=since)
        if target and found0 + job.counts["free"] >= target:
            cq.control("stop")                    # цель достигнута - мягкая остановка
            break
    if job.status == "stopped":
        hit = target and found0 + job.counts["free"] >= target
        if job.free_recent:
            db.hunt_add_found(uid, len(job.free_recent))
        st_now = db.hunt_get(uid) or {}
        db.hunt_set(uid, False, batch, found=st_now.get("found", 0),
                    mode=st_now.get("mode"), target=st_now.get("target") or 0)
        if hit:
            await msg.answer(f"🏁 <b>Цель достигнута: {found0 + job.counts['free']} свободных ников шаблона найдено.</b>\n"
                             f"Все — в ⭐ избранном. Охота остановлена.")
        else:
            await msg.answer("⏹ Охота остановлена.")
        await _hunt_send_list(msg, job)
        return
    # партия кончилась - сразу следующая, пока включена
    cur = db.hunt_get(uid)
    if cur and cur["enabled"]:
        db.hunt_bump_batch(uid, batch)      # атомарно: только если охота всё ещё включена
        if job.free_recent:
            db.hunt_add_found(uid, len(job.free_recent))
        await _hunt_send_list(msg, job)
        await _hunt_run(msg, batch + 1)
    else:
        await msg.answer(f"🏁 Охота выключена. Проверено партий: {batch}.")


async def _hunt_send_list(msg: Message, job, since: int = 0):
    free = list(dict.fromkeys(job.free_recent))
    new = free[since:]
    if not new:
        return
    lst = "\n".join(f"🟢 @{u}" for u in new[:30])
    total = sum(job.counts.values())
    web = cq.web_engine is not None and time.time() < cq.web_until
    warn = ("\n⚠️ Проверено через веб: ~97% точность (пользователи без фото "
            "могут выглядеть свободными) - перед сменой ника откройте t.me/ник"
            if web else "")
    await msg.answer(
        f"🆓 <b>Свободные ники ({len(new)} новых)</b>\n{lst}\n\n"
        f"Проверено: {total} · свободных за партию: {job.counts['free']} · "
        f"все в ⭐ избранном{warn}")


async def hunt_stop(msg: Message):
    st = db.hunt_get(msg.from_user.id)
    off = cq.control("stop")
    if st and st["enabled"]:
        db.hunt_set(msg.from_user.id, False, st["batch"], found=st.get("found", 0),
                    mode=st.get("mode"), target=st.get("target") or 0)
    await msg.answer("⏹ Останавливаю охоту" + ("" if off else ": активной партии нет, режим выключен."))


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    ok = cq.control("stop")
    st = db.hunt_get(msg.from_user.id)
    if st and st["enabled"]:
        db.hunt_set(msg.from_user.id, False, st["batch"], found=st.get("found", 0),
                    mode=st.get("mode"), target=st.get("target") or 0)
    await msg.answer("⏹ Охота остановлена." if (ok or (st and st["enabled"]))
                     else "Охота не запущена.")



async def cmd_pause(msg: Message):
    ok = cq.control("pause")
    await msg.answer("⏸ Пауза." if ok else "Нечего ставить на паузу.")



async def cmd_resume(msg: Message):
    ok = cq.control("resume")
    await msg.answer("▶ Продолжаю." if ok else "Очереди на паузе нет.")



async def btn_stop(msg: Message):
    await cmd_stop(msg)



async def btn_fav(msg: Message):
    favs = db.favorites(msg.from_user.id)
    if not favs:
        await msg.answer("⭐ Избранное пустое. Отмечайте ники кнопкой ★ под результатом проверки.")
        return
    await report_job(msg, f"Избранное · {len(favs)} ников")
    await cq.submit(msg.from_user.id, [f["username"] for f in favs],
                   "избранное · " + str(len(favs)), 1.5, len(favs))
    asyncio.create_task(progress_updater(msg.chat.id))


async def run_auto(msg: Message, base: str, limit: int = 40, delay: float = 1.5, stop_first: bool = False):
    from . import rules as _r
    b = _r.normalize(base)
    ok, why = _r.validate(b)
    if not ok:
        await msg.answer(f"⚠️ {why}")
        return
    limit = min(5000, max(1, int(limit)))
    items = list(generators.auto_variants(b, limit))
    if not items:
        await msg.answer("⚠️ Не удалось построить варианты.")
        return
    await report_job(msg, f"авто-поиск · {b} · {len(items)} вариантов")
    job = await cq.submit(msg.from_user.id, items, f"авто-поиск · {b}", delay,
                         len(items), stop_on_free=stop_first, auto_save=True)
    asyncio.create_task(progress_updater(msg.chat.id))
    asyncio.create_task(auto_watcher(msg, job, len(items)))


async def auto_watcher(msg: Message, job, total: int, kind: str = "auto"):
    """Ждёт конца авто/фрагмент-поиска и присылает итог отдельным сообщением."""
    while job.status in ("queued", "running", "paused", "flood"):
        await asyncio.sleep(2)
    if job.status == "stopped":
        return  # прогресс-сообщение уже сказало всё
    web = cq.web_engine is not None and time.time() < cq.web_until
    warn = ("\n⚠️ Пока проверяю через веб (~97% точности): перед установкой "
            "открой t.me/ник и убедись, что он пуст") if web else ""
    if job.free_recent:
        free = list(dict.fromkeys(job.free_recent))
        lst = "\n".join(f"• @{x}" for x in free[:30])
        await msg.answer(
            f"🟢 <b>Свободных найдено: {len(free)}</b>\n{lst}\n\n"
            f"⭐ Все сохранены в избранное.\n"
            f"Проверено вариантов: {job.done} из {total}{warn}",
            reply_markup=fav_kb(free[0]))
    else:
        if kind == "frag":
            await msg.answer(
                f"🔴 Свободных не нашёл: перебрал {job.done} вариантов с фрагментом.\n"
                "Попробуй другой/длиннее фрагмент или увеличь лимит: <code>/frag dost 400</code>{warn}")
        else:
            await msg.answer(
                f"🔴 Свободных не нашёл: {job.done} вариантов вокруг ника заняты.\n"
                "Почти все короткие/красивые заняты или на аукционе Fragment.\n"
                "Попробуй длиннее или другой корень{warn}")
    # ручной поиск закончился: если охота была включена - продолжаем её сами
    try:
        st = db.hunt_get(msg.from_user.id)
        if st and st.get("enabled"):
            await _hunt_run(msg, int(st.get("batch") or 0) + 1)
    except Exception:
        log.exception("hunt resume after auto")


def is_pattern(t: str) -> bool:
    t = t.lower()
    return any(c in t for c in ("?", "#", "*")) and all(c in "abcdefghijklmnopqrstuvwxyz0123456789_?*#" for c in t)


def engine_ok() -> bool:
    return engine is not None and engine.available


async def unavailable(msg: Message):
    await msg.answer(
        "⚠️ <b>Аккаунт-проверяльщик не привязан.</b>\n"
        "Проверки сейчас невозможны — это честный ответ, без имитаций.\n\n"
        "Чтобы включить: пришлите API_ID и API_HASH с my.telegram.org, затем подтвердите код входа.")



async def on_web_app(msg: Message):
    try:
        payload = json.loads(msg.web_app_data.data)
    except Exception:
        await msg.answer("⚠️ Не понял данные приложения.")
        return
    await run_payload(msg, payload)


async def run_payload(msg: Message, payload: dict):
    t = payload.get("type")
    if not engine_ok():
        await unavailable(msg)
        return
    if t == "single":
        r = await cq.check_one(msg.from_user.id, payload.get("username", ""))
        await msg.answer(result_text(r, msg.from_user.id), reply_markup=fav_kb(payload.get("username", "")))
    elif t == "job" and payload.get("mode") == "favorites":
        await btn_fav(msg)
    elif t == "job":
        mode = payload.get("mode")
        delay = max(0.5, float(payload.get("delay") or 1.5))
        limit = min(5000, int(payload.get("limit") or 5000))
        if mode == "list":
            items = generators.parse_list(payload.get("text", ""))[:limit]
            label, total = f"список · {len(items)} ников", len(items)
        elif mode == "pattern":
            items = generators.pattern_candidates(payload.get("pattern", ""), limit=limit,
                                                  random_order=bool(payload.get("random")))
            label, total = f"шаблон {payload.get('pattern', '')}", None
        elif mode == "auto":
            await run_auto(msg, payload.get("base", ""), limit=min(5000, max(1, int(payload.get("limit") or 40))),
                           delay=delay, stop_first=bool(payload.get("stop_first")))
            return
        elif mode == "frag":
            await run_frag(msg, payload.get("text", ""), limit=min(5000, max(1, int(payload.get("limit") or 200))),
                           delay=delay)
            return
        elif mode == "random":
            items = generators.random_candidates(int(payload.get("length", 6)),
                                                 payload.get("style", "letters"), limit)
            label, total = f"генератор · длина {payload.get('length', 6)}", limit
        elif mode == "dictionary":
            items = generators.dictionary_candidates(generators.parse_list(payload.get("text", "")), limit)
            label, total = "словарь", None
        else:
            await msg.answer("⚠️ Неизвестный режим.")
            return
        if mode == "list" and not items:
            await msg.answer("⚠️ Список пуст.")
            return
        await report_job(msg, label)
        await cq.submit(msg.from_user.id, items, label, delay, total)
        asyncio.create_task(progress_updater(msg.chat.id))


def fav_kb(username: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav:{username}"),
        InlineKeyboardButton(text="♻️ Перепроверить", callback_data=f"re:{username}"),
    ]])


@dp.callback_query(F.data.startswith("fav:"))
async def cb_fav(c: CallbackQuery):
    u = c.data.split(":", 1)[1]
    added = db.toggle_favorite(c.from_user.id, u)
    await c.answer("⭐ Добавлено в избранное" if added else "Убрано из избранного", show_alert=False)


@dp.callback_query(F.data.startswith("re:"))
async def cb_re(c: CallbackQuery):
    if not engine_ok():
        await c.answer("Аккаунт-проверяльщик не привязан", show_alert=True)
        return
    u = c.data.split(":", 1)[1]
    await c.answer("Проверяю…")
    r = await cq.check_one(c.from_user.id, u)
    await c.message.answer(result_text(r, c.from_user.id), reply_markup=fav_kb(u))



async def on_text(msg: Message):
    text = (msg.text or "").strip()
    if not engine_ok():
        await unavailable(msg)
        return
    if is_pattern(text):
        items = generators.pattern_candidates(text, limit=5000)
        await report_job(msg, f"шаблон {text.lower()}")
        await cq.submit(msg.from_user.id, items, f"шаблон {text.lower()}", 1.5, None)
        asyncio.create_task(progress_updater(msg.chat.id))
        return
    items = generators.parse_list(text)
    if not items:
        await msg.answer("⚠️ Не распознал ники. Формат: <code>durov</code>, <code>@durov</code>, список или шаблон <code>ka??a</code>.")
        return
    if len(items) == 1:
        # юзер прислал ЖЕЛАЕМЫЙ ник: ищем его и буквенные альтернативы,
        # пока не найдём свободный (занято ~97%, либо аукцион Fragment)
        await run_auto(msg, items[0], limit=120)
    else:
        await report_job(msg, f"список · {len(items)} ников")
        await cq.submit(msg.from_user.id, items, f"список · {len(items)} ников", 1.5, len(items))
        asyncio.create_task(progress_updater(msg.chat.id))


# ---------- выгрузка для мини-аппа ----------
_was_active = [False]

async def sync_loop():
    """Каждые 60 сек: БД + состояние пользователя -> ветка data."""
    last = 0
    while True:
        try:
            active = bool(cq and cq.job and cq.job.status in ("queued", "running", "paused", "flood"))
            if time.time() - last > (8 if active else 60) or (not active and _was_active[0] and time.time() - last > 3):
                last = time.time()
                _was_active[0] = active
                db.checkpoint()
                hb = {"ts": int(time.time()),
                      "engine": bool(engine) and engine.available,
                      "job": cq.job.label if cq.job else None,
                      "job_status": cq.job.status if cq.job else None,
                      "web_mode": bool(cq.job) and time.time() < cq.web_until,
                      "web_until": int(max(0, cq.web_until - time.time()))}
                files = {"heartbeat.json": json.dumps(hb, ensure_ascii=False)}
                for uid in {u for (u,) in db.conn().execute("SELECT DISTINCT user_id FROM checks").fetchall()} | \
                         {u for (u,) in db.conn().execute("SELECT DISTINCT user_id FROM favorites").fetchall()}:
                    j = cq.job if (cq.job and cq.job.user_id == uid) else None
                    files[f"users/{uid}/state.json"] = json.dumps(j.snapshot() if j else {"status": "idle"}, ensure_ascii=False)
                    files[f"users/{uid}/history.json"] = json.dumps(db.history(uid, limit=2000), ensure_ascii=False)
                    files[f"users/{uid}/favorites.json"] = json.dumps(
                        [{"username": f["username"], "added_at": f["added_at"]} for f in db.favorites(uid)], ensure_ascii=False)
                with open(db.DB_PATH, "rb") as f:
                    files["db/checks.db"] = f.read()
                ok = sync.push_payload(files)
                if not ok:
                    log.error("sync push failed")
        except Exception:
            log.exception("sync error")
        await asyncio.sleep(15)


# ---------- запуск ----------
async def run():
    global cq, engine
    from .checker import Engine as E
    from .main import _connect_engine
    engine = E(await _connect_engine())
    cq = queue.CheckQueue(engine, web_engine=webcheck.WebEngine())
    await cq.start()

    async def _verify_loop():
        """Каждые 2 мин: если MTProto свободен - подтверждаем веб-кандидатов."""
        while True:
            await asyncio.sleep(120)
            try:
                async def _n(u):
                    await bot.send_message(OWNER_UID, f"✅ <b>Подтверждено MTProto:</b> @{u} свободен")
                await cq.verify_web_candidates(OWNER_UID, _n)
            except Exception:
                log.exception("verify loop")
    asyncio.create_task(_verify_loop())
    if BOT_TOKEN:
        # синяя кнопка мини-аппа слева в поле ввода (открывает приложение)
        try:
            await bot.set_chat_menu_button(menu_button=None)   # мини-апп скрыт
        except Exception:
            log.exception("menu button reset failed")
        await bot.delete_webhook(drop_pending_updates=False)
        try:
            from aiogram.types import BotCommand
            await bot.set_my_commands([BotCommand(command="start", description="Включить охоту"),
                                   BotCommand(command="stop", description="Остановить охоту")])
        except Exception:
            pass
        asyncio.create_task(sync_loop())
        async def _resume_hunt():
            for st in db.hunt_all():
                try:
                    uid = st["user_id"]
                    class _M:
                        class chat:
                            id = uid
                        class from_user:
                            id = uid
                        async def answer(self, text, **kw):
                            return await bot.send_message(uid, text, **kw)
                    await bot.send_message(uid, f"🆓 Охота продолжается после перезапуска (партия {st['batch'] + 1}).")
                    await _hunt_run(_M(), st["batch"] + 1)
                except Exception:
                    log.exception("hunt resume failed for %s", st["user_id"])
        asyncio.create_task(_resume_hunt())
        await dp.start_polling(bot)
