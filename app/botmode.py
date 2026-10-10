"""Режим бота: живёт на GitHub Actions, проверяет ники и отвечает в чате.

Бот НЕ хранит деньги и не касается чужих транзакций — только проверка ников.
Прогресс очереди редактируется одним сообщением; данные для мини-аппа
коммитятся в ветку data каждые 60 сек.
"""
import asyncio
import json
import logging
import os
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, KeyboardButton, Message,
                           ReplyKeyboardMarkup, WebAppInfo)

from . import db, generators, queue, sync
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
    "⚡️ <b>NICK CHECKER</b> ⚡️\n"
    "💎 <i>Проверка юзернеймов Telegram — по-настоящему</i>\n\n"
    "🟢 Свободен · 🔴 Занят · 🟡 Недоступен\n\n"
    "Просто пришли ник сообщением — например <code>@durov</code>.\n"
    "Или пришли сразу список: <code>nick1, nick2, nick3</code>.\n"
    "Шаблон: <code>ka??a</code> ( ? буква, # цифра, * любая ).\n"
    "Генератор: <code>/gen 6 произносимые 50</code>\n"
    "Авто-поиск: <code>/auto doston</code> — сам найду свободный и сохраню ⭐\n\n"
    "👇 Или открой приложение кнопкой ниже."
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
@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(BANNER, reply_markup=menu(msg.from_user.id))


@dp.message(Command("gen"))
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


@dp.message(Command("auto"))
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
    await run_auto(msg, base, limit=limit, delay=1.5)


@dp.message(Command("stop"))
async def cmd_stop(msg: Message):
    ok = cq.control("stop")
    await msg.answer("⏹ Очередь остановлена." if ok else "Активной очереди нет.")


@dp.message(Command("pause"))
async def cmd_pause(msg: Message):
    ok = cq.control("pause")
    await msg.answer("⏸ Пауза." if ok else "Нечего ставить на паузу.")


@dp.message(Command("resume"))
async def cmd_resume(msg: Message):
    ok = cq.control("resume")
    await msg.answer("▶ Продолжаю." if ok else "Очереди на паузе нет.")


@dp.message(F.text == "⏹ Стоп")
async def btn_stop(msg: Message):
    await cmd_stop(msg)


@dp.message(F.text == "⭐ Избранное")
async def btn_fav(msg: Message):
    favs = db.favorites(msg.from_user.id)
    if not favs:
        await msg.answer("⭐ Избранное пустое. Отмечайте ники кнопкой ★ под результатом проверки.")
        return
    await report_job(msg, f"Избранное · {len(favs)} ников")
    await cq.submit(msg.from_user.id, [f["username"] for f in favs],
                   "избранное · " + str(len(favs)), 1.5, len(favs))
    asyncio.create_task(progress_updater(msg.chat.id))


async def run_auto(msg: Message, base: str, limit: int = 40, delay: float = 1.5):
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
                         len(items), stop_on_free=True, auto_save=True)
    asyncio.create_task(progress_updater(msg.chat.id))
    asyncio.create_task(auto_watcher(msg, job, len(items)))


async def auto_watcher(msg: Message, job, total: int):
    """Ждёт конца авто-поиска и присылает итог отдельным сообщением."""
    while job.status in ("queued", "running", "paused", "flood"):
        await asyncio.sleep(2)
    if job.status == "stopped":
        return  # прогресс-сообщение уже сказало всё
    if job.free_recent:
        u = job.free_recent[-1]
        await msg.answer(
            f"🟢 <b>Найден свободный: @{u}</b>\n"
            f"⭐ Уже сохранён в избранное.\n"
            f"Проверено вариантов: {job.done}",
            reply_markup=fav_kb(u))
    else:
        await msg.answer(
            f"🔴 Свободных не нашёл: перебрал {job.done} вариантов вокруг ника.\n"
            "Попробуй другую основу или увеличь лимит: <code>/auto doston 100</code>")


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


@dp.message(F.web_app_data)
async def on_web_app(msg: Message):
    try:
        payload = json.loads(msg.web_app_data.data)
    except Exception:
        await msg.answer("⚠️ Не понял данные приложения.")
        return
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
            await run_auto(msg, payload.get("base", ""), limit=payload.get("limit") or 40,
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


@dp.message(F.text)
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
        r = await cq.check_one(msg.from_user.id, items[0])
        await msg.answer(result_text(r, msg.from_user.id), reply_markup=fav_kb(items[0]))
    else:
        await report_job(msg, f"список · {len(items)} ников")
        await cq.submit(msg.from_user.id, items, f"список · {len(items)} ников", 1.5, len(items))
        asyncio.create_task(progress_updater(msg.chat.id))


# ---------- выгрузка для мини-аппа ----------
async def sync_loop():
    """Каждые 60 сек: БД + состояние пользователя -> ветка data."""
    last = 0
    while True:
        try:
            if time.time() - last > 60:
                last = time.time()
                db.checkpoint()
                files = {"heartbeat.json": json.dumps({"ts": int(time.time()), "engine": engine.available}, ensure_ascii=False)}
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
    cq = queue.CheckQueue(engine)
    await cq.start()
    if BOT_TOKEN:
        # синяя кнопка мини-аппа слева в поле ввода (открывает приложение)
        try:
            from aiogram.types import MenuButtonWebApp
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                type="web_app", text="Ник-чекер", web_app=WebAppInfo(url=APP_URL)))
        except Exception:
            log.exception("menu button setup failed")
        await bot.delete_webhook(drop_pending_updates=False)
        try:
            from aiogram.types import BotCommand
            await bot.set_my_commands([BotCommand(command="start", description="Проверка ников"),
                                       BotCommand(command="gen", description="Генератор ников"),
                                       BotCommand(command="auto", description="Авто-поиск свободного"),
                                       BotCommand(command="stop", description="Остановить очередь"),
                                       BotCommand(command="pause", description="Пауза"),
                                       BotCommand(command="resume", description="Продолжить")])
        except Exception:
            pass
        asyncio.create_task(sync_loop())
        await dp.start_polling(bot)
