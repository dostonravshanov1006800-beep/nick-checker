"""SQLite: история проверок, кэш 10 минут, избранное (спека §7)."""
import os
import sqlite3
import time
import threading

from . import rules

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "checks.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

CACHE_TTL = 600  # 10 минут


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=10000")
            _conn.execute("""CREATE TABLE IF NOT EXISTS checks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 0,
                username TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_type TEXT, owner_name TEXT,
                error TEXT,
                checked_at INTEGER NOT NULL)""")
            _conn.execute("""CREATE TABLE IF NOT EXISTS hunt(
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                batch INTEGER NOT NULL DEFAULT 0,
                found INTEGER NOT NULL DEFAULT 0,
                started_at INTEGER NOT NULL DEFAULT 0)""")
            _conn.execute("""CREATE TABLE IF NOT EXISTS favorites(
                user_id INTEGER NOT NULL DEFAULT 0,
                username TEXT NOT NULL,
                added_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, username))""")
            _conn.commit()
    return _conn


def checkpoint():
    """Потокобезопасный сброс WAL (вызывается из любого потока)."""
    try:
        with _lock:
            c = sqlite3.connect(DB_PATH, timeout=10)
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
    except Exception:
        pass


def save_check(user_id: int, r: dict):
    with _lock:
        conn().execute(
            "INSERT INTO checks(user_id, username, status, owner_type, owner_name, error, checked_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, r["username"], r["status"], (r.get("owner") or {}).get("type"),
             (r.get("owner") or {}).get("name"), r.get("error"), int(time.time())))
        conn().commit()


def cached(username: str, user_id: int = 0):
    """Последняя проверка моложе CACHE_TTL, либо None."""
    with _lock:
        row = conn().execute(
            "SELECT *, ? AS cached FROM checks WHERE username=? AND checked_at>? ORDER BY id DESC LIMIT 1",
            (user_id, username, int(time.time()) - CACHE_TTL)).fetchone()
    return dict(row) if row else None


def history(user_id: int = 0, search: str = "", status: str = "", sort: str = "new", limit: int = 2000):
    q = "SELECT * FROM checks WHERE user_id=?"
    args: list = [user_id]
    if search:
        q += " AND username LIKE ?"
        args.append(f"%{search.lower()}%")
    if status:
        q += " AND status=?"
        args.append(status)
    q += " ORDER BY " + ("checked_at DESC" if sort != "len" else "LENGTH(username) ASC, id DESC") + " LIMIT ?"
    args.append(limit)
    with _lock:
        rows = conn().execute(q, args).fetchall()
    return [dict(r) for r in rows]


def add_favorite(user_id: int, username: str) -> bool:
    """Добавить в избранное (без toggle): True если добавлено, False если уже был."""
    u = rules.normalize(username)
    if not u:
        return False
    with conn() as _conn:
        cur = _conn.execute("SELECT 1 FROM favorites WHERE user_id=? AND username=?", (user_id, u)).fetchone()
        if cur:
            return False
        _conn.execute("INSERT INTO favorites(user_id, username, added_at) VALUES(?,?,?)", (user_id, u, int(time.time())))
    return True


def toggle_favorite(user_id: int, username: str) -> bool:
    u = username.lower()
    with _lock:
        cur = conn().execute("SELECT 1 FROM favorites WHERE user_id=? AND username=?", (user_id, u)).fetchone()
        if cur:
            conn().execute("DELETE FROM favorites WHERE user_id=? AND username=?", (user_id, u))
            conn().commit()
            return False
        conn().execute("INSERT INTO favorites(user_id, username, added_at) VALUES(?,?,?)", (user_id, u, int(time.time())))
        conn().commit()
        return True


def favorites(user_id: int) -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT * FROM favorites WHERE user_id=? ORDER BY added_at DESC", (user_id,)).fetchall()
    return [dict(r) for r in rows]


# ---------- фоновая охота ----------
def hunt_all() -> list[dict]:
    with _lock:
        rows = conn().execute("SELECT * FROM hunt WHERE enabled=1").fetchall()
    return [dict(r) for r in rows]


def hunt_get(user_id: int) -> dict | None:
    with _lock:
        row = conn().execute("SELECT * FROM hunt WHERE user_id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def hunt_set(user_id: int, enabled: bool, batch: int = 0, found: int = 0):
    with _lock:
        conn().execute(
            "INSERT INTO hunt(user_id, enabled, batch, found, started_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET enabled=?, batch=?, found=?",
            (user_id, int(enabled), batch, found, int(time.time()), int(enabled), batch, found))
        conn().commit()


def hunt_add_found(user_id: int, n: int = 1):
    with _lock:
        conn().execute("UPDATE hunt SET found=found+? WHERE user_id=?", (n, user_id))
        conn().commit()


def hunt_bump_batch(user_id: int, batch: int):
    """Обновить номер партии ТОЛЬКО если охота всё ещё включена (антигонка со stop)."""
    with _lock:
        conn().execute("UPDATE hunt SET batch=? WHERE user_id=? AND enabled=1", (batch, user_id))
        conn().commit()
