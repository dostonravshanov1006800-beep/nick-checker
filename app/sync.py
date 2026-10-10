"""Синхронизация состояния в ветку data (GitHub — бесплатный стор для Pages).

Паттерн, проверенный на предыдущих проектах: git-клон в /tmp, коммит, push -f.
SQLite бэкап + JSON-состояния для мини-аппа.
"""
import json
import os
import subprocess
import time

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
URL = f"https://x-access-token:{TOKEN}@github.com/{REPO}.git"
OUT = "/tmp/nc_data"


def _git(*args, cwd=OUT):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _init():
    os.makedirs(OUT, exist_ok=True)
    if not os.path.isdir(os.path.join(OUT, ".git")):
        _git("init", "-q", "-b", "data")
        _git("remote", "add", "origin", URL)
        if _git("config", "user.name").returncode != 0:
            _git("config", "user.name", "checker")
            _git("config", "user.email", "checker@users.noreply.github.com")


def push_payload(files: dict, root: str = ""):
    """files: относительный путь -> байты/строка. Атомарный коммит в data."""
    _init()
    for path, content in files.items():
        full = os.path.join(OUT, root, path) if root else os.path.join(OUT, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(full, mode) as f:
            f.write(content)
    r = _git("add", "-A")
    st = _git("status", "--porcelain")
    if not st.stdout.strip():
        return True  # нет изменений
    _git("commit", "-q", "-m", "live")
    for _ in range(3):
        pr = _git("push", "-q", "-f", "origin", "data")
        if pr.returncode == 0:
            return True
        time.sleep(2)
    return False


def restore(db_path: str) -> bool:
    """Забрать БД из data-ветки, если она там есть и не пустая."""
    try:
        _init()
        _git("fetch", "-q", "--depth", "1", "origin", "data")
        blob = subprocess.run(["git", "show", "FETCH_HEAD:db/checks.db"], cwd=OUT, capture_output=True)
        if blob.returncode != 0 or not blob.stdout:
            return False
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        tmp = db_path + ".restored"
        with open(tmp, "wb") as f:
            f.write(blob.stdout)
        import sqlite3
        try:
            n = sqlite3.connect(tmp).execute("select count(*) from checks").fetchone()[0]
        except Exception:
            return False
        if os.path.exists(db_path):
            try:
                cur = sqlite3.connect(db_path).execute("select count(*) from checks").fetchone()[0]
            except Exception:
                cur = -1
            if cur >= n:
                os.remove(tmp)
                return False  # локальная свежее
        os.replace(tmp, db_path)
        return True
    except Exception:
        return False
