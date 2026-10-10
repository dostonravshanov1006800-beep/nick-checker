"""Локальный сервис: FastAPI + SSE. Спека §2, §3, §8.

Запуск: run.sh / run.bat (uvicorn app.main:app). Порт и пароль в .env.
Слушает 0.0.0.0 только если задан ACCESS_PASSWORD.
"""
import asyncio
import csv
import io
import json
import os
import socket
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import auth, checker, db, generators, queue

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
LOCAL_USER = 0  # в локальном режиме один пользователь

app = FastAPI(title="Nick Checker", docs=None, redoc_url=None)
engine = checker.Engine(None)
cq: queue.CheckQueue | None = None


@app.on_event("startup")
async def _startup():
    global cq, engine
    if not auth.ok():
        print("\n[!] ACCESS_PASSWORD не задан в .env — сервис не запустится на 0.0.0.0 (спека §3).\n")
        os._exit(1)
    engine = checker.Engine(await _connect_engine())
    cq = queue.CheckQueue(engine)
    await cq.start()
    port = int(os.environ.get("PORT", "8000"))
    print("\n" + "=" * 52)
    print(f"  Nick Checker запущен")
    print(f"  Локально:       http://127.0.0.1:{port}")
    print(f"  В вашей сети:   http://{_lan_ip()}:{port}")
    print(f"  Движок Telegram: {'подключён' if engine.available else 'НЕТ (python login.py)'}")
    print("=" * 52 + "\n")


async def _connect_engine():
    """Telethon-клиент из StringSession (env) или файла data/checker.session."""
    api_id = os.environ.get("API_ID")
    api_hash = os.environ.get("API_HASH")
    if not (api_id and api_hash):
        return None
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    sess_str = os.environ.get("SESSION", "")
    session = StringSession(sess_str) if sess_str else os.path.join(os.path.dirname(db.DB_PATH), "checker.session")
    client = TelegramClient(session, int(api_id), api_hash)
    await asyncio.wait_for(client.connect(), timeout=30)
    if not await client.is_user_authorized():
        await client.disconnect()
        return None
    return client


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "?.?.?"


# --- доступ / пароль ---
@app.middleware("http")
async def guard(request: Request, call_next):
    open_paths = ("/api/login", "/api/ping", "/app.js", "/style.css", "/favicon.svg")
    if request.url.path in open_paths or request.url.path.startswith("/api/login"):
        return await call_next(request)
    tok = request.cookies.get(auth.COOKIE, "")
    if not auth.check_token(tok):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "нужен вход"}, status_code=401)
        return HTMLResponse(_login_page(), status_code=401)
    return await call_next(request)


def _login_page() -> str:
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Nick Checker</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;background:#f2f2f7;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0}
.c{background:#fff;border-radius:18px;padding:36px 28px;max-width:340px;width:90%;box-shadow:0 12px 40px rgba(0,0,0,.12);text-align:center}
h1{font-size:20px;margin:0 0 6px}p{color:#8e8e93;font-size:14px;margin:0 0 20px}
input{width:100%;box-sizing:border-box;padding:13px 14px;border:1px solid #d1d1d6;border-radius:12px;font-size:16px;margin-bottom:12px}
button{width:100%;padding:13px;background:#007aff;color:#fff;border:0;border-radius:12px;font-size:16px;font-weight:600}
.e{color:#ff3b30;font-size:13px;margin-top:10px;min-height:16px}</style></head><body>
<div class="c"><h1>Nick Checker</h1><p>Введите пароль доступа</p>
<form onsubmit="go(event)"><input id="p" type="password" autofocus><button>Войти</button><div class="e" id="e"></div></form></div>
<script>async function go(ev){ev.preventDefault();const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('p').value})});if(r.ok)location.reload('/');else document.getElementById('e').textContent='Неверный пароль'}</script>
</body></html>"""


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if body.get("password") != auth.access_password():
        return JSONResponse({"error": "неверный пароль"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE, auth.make_token(), httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.get("/api/ping")
async def ping():
    return {"mode": "local", "engine": engine.available}


# --- проверки ---
@app.post("/api/check")
async def check_one(request: Request):
    body = await request.json()
    try:
        r = await cq.check_one(LOCAL_USER, body.get("username", ""))
    except Exception as e:
        r = {"username": body.get("username", ""), "status": "error", "error": checker._short_reason(e), "owner": None}
    return r


@app.post("/api/parse")
async def parse(request: Request):
    """Список из текста (вставка) или файла (txt/csv). Лимит 5000 (спека §4)."""
    body = await request.json()
    text = body.get("text", "")
    items = generators.parse_list(text)
    return {"items": items, "count": len(items)}


@app.post("/api/jobs")
async def create_job(request: Request):
    body = await request.json()
    mode = body.get("mode", "list")
    delay = float(body.get("delay", os.environ.get("CHECK_DELAY", "1.5") or 1.5))
    limit = int(body.get("limit") or 5000)
    if mode == "list":
        items = generators.parse_list(body.get("text", ""))[:limit]
        label = f"список · {len(items)} ников"
        total = len(items)
    elif mode == "pattern":
        items = generators.pattern_candidates(body.get("pattern", ""), limit=limit,
                                              random_order=bool(body.get("random")))
        label = f"шаблон {body.get('pattern', '')}"
        total = None
    elif mode == "random":
        items = generators.random_candidates(int(body.get("length", 6)), body.get("style", "letters"), limit)
        label = f"генератор · длина {body.get('length', 6)}"
        total = min(limit, 10 ** 9)
    elif mode == "dictionary":
        items = generators.dictionary_candidates(generators.parse_list(body.get("text", "")), limit)
        label = "словарь"
        total = None
    else:
        return JSONResponse({"error": "неизвестный режим"}, status_code=400)
    job = await cq.submit(LOCAL_USER, items, label, delay, total)
    return job.snapshot()


@app.post("/api/job/control")
async def job_control(request: Request):
    body = await request.json()
    ok = cq.control(body.get("action", ""))
    return {"ok": ok}


@app.get("/api/state")
async def state():
    if not cq.job:
        return {"status": "idle"}
    return cq.job.snapshot()


@app.get("/api/events")
async def events():
    q = cq.subscribe()

    async def stream():
        try:
            snap = cq.job.snapshot() if cq.job else {"status": "idle"}
            yield f"data: {json.dumps({'type': 'job', 'job': snap})}\n\n"
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            cq.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --- история и избранное ---
@app.get("/api/history")
async def history(search: str = "", status: str = "", sort: str = "new"):
    return db.history(LOCAL_USER, search.lower(), status, sort)


@app.post("/api/favorite")
async def favorite(request: Request):
    body = await request.json()
    added = db.toggle_favorite(LOCAL_USER, body.get("username", ""))
    return {"added": added}


@app.get("/api/favorites")
async def favorites():
    return db.favorites(LOCAL_USER)


@app.get("/api/export")
async def export(status: str = "", only_free: bool = False):
    rows = db.history(LOCAL_USER, "", status, "new", limit=100000)
    if only_free:
        rows = [r for r in rows if r["status"] == "free"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["username", "status", "owner_type", "owner_name", "checked_at", "error"])
    for r in rows:
        w.writerow([r["username"], r["status"], r["owner_type"] or "", r["owner_name"] or "",
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["checked_at"])), r["error"] or ""])
    fname = "free" if only_free else ("history" + ("_" + status if status else ""))
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.csv"'})


# --- статика (после входа) ---
@app.get("/")
async def index():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/app.js")
async def appjs():
    with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as f:
        return Response(f.read(), media_type="application/javascript")


@app.get("/style.css")
async def stylecss():
    with open(os.path.join(STATIC, "style.css"), encoding="utf-8") as f:
        return Response(f.read(), media_type="text/css")
