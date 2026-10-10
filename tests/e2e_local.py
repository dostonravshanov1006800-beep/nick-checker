"""E2E локального режима: реальный браузер, реальный сервер FastAPI,
движок подменён фейковым клиентом ТОЛЬКО в этом тесте (спека §11)."""
import asyncio
import os
import subprocess
import sys
import time
import urllib.request

REPO = "/app/conversations/6ac9c315fdd87fb9d063cd11/username-checker"
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"

LAUNCHER = r'''
import os, sys
sys.path.insert(0, "REPOX")
os.environ["ACCESS_PASSWORD"] = "test123"
import asyncio, types
from telethon import errors
from app import main as M

def user(first="Дуров", username="durov"):
    u = types.SimpleNamespace(first_name=first, last_name="", username=username, bot=False)
    return types.SimpleNamespace(users=[u], chats=[])

class FakeClient:
    async def __call__(self, req):
        name = getattr(req, "username", "")
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        from telethon.tl.functions.account import CheckUsernameRequest
        if isinstance(req, ResolveUsernameRequest):
            if name == "durov":
                return user()
            if name == "mybotname":
                b = types.SimpleNamespace(first_name="Bot", last_name="", username="mybotname", bot=True)
                return types.SimpleNamespace(users=[b], chats=[])
            raise errors.UsernameNotOccupiedError(request=None)
        if isinstance(req, CheckUsernameRequest):
            if name == "freenick":
                return True
            if name.startswith("bad"):
                raise errors.UsernameInvalidError(request=None)
            return False
        raise RuntimeError("unexpected")

async def fake_connect():
    return FakeClient()

M._connect_engine = fake_connect

import uvicorn
uvicorn.run(M.app, host="127.0.0.1", port=PORTX, log_level="warning")
'''.replace("REPOX", REPO).replace("PORTX", str(PORT))


async def run():
    from playwright.async_api import async_playwright
    results = []

    def check(name, ok, extra=""):
        results.append((name, ok))
        print(("PASS " if ok else "FAIL ") + name, extra)

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        ctx = await browser.new_context(viewport={"width": 360, "height": 740})
        page = await ctx.new_page()

        # 1. Без пароля — страница входа
        await page.goto(BASE + "/")
        await page.wait_for_timeout(300)
        check("вход: показан запрос пароля", await page.locator("input[type=password]").count() == 1)

        # 2. Неверный пароль
        await page.fill("input[type=password]", "wrong")
        await page.click("button:has-text('Войти')")
        await page.wait_for_timeout(400)
        check("вход: неверный пароль отклонён", "Неверный пароль" in await page.locator("#e").inner_text())

        # 3. Верный пароль
        await page.fill("input[type=password]", "test123")
        await page.click("button:has-text('Войти')")
        await page.wait_for_timeout(900)
        check("вход: после пароля открывается UI", "Nick" in (await page.locator(".hero h1").inner_text()))

        # 4. Одиночная проверка занятого
        await page.fill("#in-one", "@durov")
        await page.click("#btn-one")
        await page.wait_for_timeout(1500)
        body = await page.locator("#feed").inner_text()
        check("проверка: durov = занят, владелец виден", "занят" in body and "Дуров" in body)

        # 5. Свободный и недоступный
        for nick, want in [("freenick", "свободен"), ("bad_boy_x", "недоступен")]:
            await page.fill("#in-one", nick)
            await page.click("#btn-one")
            await page.wait_for_timeout(900)
        body = await page.locator("#feed").inner_text()
        check("проверка: freenick свободен", "freenick" in body and "свободен" in body)
        check("проверка: bad* недоступен", "недоступен" in body)

        # 6. Невалидный без запроса
        await page.fill("#in-one", "ab")
        await page.click("#btn-one")
        await page.wait_for_timeout(600)
        body = await page.locator("#feed").inner_text()
        check("проверка: невалидный — понятная причина", "короткий" in body)

        # 7. Очередь по списку с SSE
        await page.click("#mode-seg button[data-m=list]")
        await page.fill("#in-list", "durov, freenick, bad_boy_x, qwe_rty_z")
        await page.fill("#in-delay-list", "0.3")
        await page.click("#btn-list")
        await page.wait_for_timeout(2500)
        check("очередь: прогресс заполнен", "/" in (await page.locator("#cnt-done").inner_text()))
        check("очередь: живая лента показывает результаты", (await page.locator("#feed .res").count()) >= 3)

        # 8. Пауза/стоп доступны, история обновилась
        await page.click("#tabbar button[data-tab=history]")
        await page.wait_for_timeout(700)
        hist = await page.locator("#hist-feed").inner_text()
        check("история: записи есть", "durov" in hist and "freenick" in hist)

        # 9. Фильтр по статусу
        await page.click("#hist-chips .chip[data-s=free]")
        await page.wait_for_timeout(700)
        hist = await page.locator("#hist-feed").inner_text()
        check("история: фильтр только свободные", "freenick" in hist and "durov" not in hist)

        # 10. Избранное
        await page.click("#tabbar button[data-tab=fav]")
        await page.wait_for_timeout(300)
        check("избранное: подсказка при пустом списке", "пусто" in (await page.locator("#fav-feed").inner_text()).lower())

        # 11. Экспорт CSV (только свободные)
        await page.click("#tabbar button[data-tab=history]")
        await page.wait_for_timeout(400)
        async with page.expect_download() as dl:
            await page.click("#exp-free")
        d = await dl.value
        path = await d.path()
        content = open(path, encoding="utf-8-sig").read()
        check("экспорт: CSV только свободные", "freenick" in content and "durov" not in content)

        # 12. Мобильная вёрстка без горизонтального скролла
        scroll_w = await page.evaluate("document.documentElement.scrollWidth")
        check("вёрстка: нет горизонтального скролла на 360px", scroll_w <= 360, f"scrollWidth={scroll_w}")

        await browser.close()

    fails = [n for n, ok in results if not ok]
    print(f"\nИтого: {len(results) - len(fails)}/{len(results)} PASS")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    proc = subprocess.Popen([sys.executable, "-c", LAUNCHER], cwd=REPO,
                            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(BASE + "/api/ping", timeout=2)
                break
            except Exception:
                pass
        asyncio.run(run())
    finally:
        proc.terminate()
