"""App_Remote — удалённый доступ к вычислительным ресурсам (MVP 0.1).

Запуск:
    python app.py              # первый запуск: выбор роли в браузере
    python app.py --role host  # принудительно роль хоста
    python app.py --role client
    python app.py --reset      # сбросить выбор роли
"""
import argparse
import asyncio
import sys
import webbrowser

from aiohttp import web

from core import state

SETUP_PORT = 8599


def run_setup():
    """Мини-сервер выбора роли: сохраняет выбор и продолжает запуск."""
    chosen = {}

    async def page(request):
        return web.FileResponse(f"{state.WEB_DIR}/setup.html")

    async def choose(request):
        d = await request.json()
        role = d.get("role")
        if role not in ("host", "client"):
            raise web.HTTPBadRequest()
        chosen["role"] = role
        cfg = state.load_config()
        cfg["role"] = role
        state.save_config(cfg)

        def _stop():
            raise web.GracefulExit()
        asyncio.get_running_loop().call_later(0.4, _stop)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_static("/static/", state.WEB_DIR)
    app.router.add_post("/api/choose", choose)
    print(f"[App_Remote] Первый запуск: выберите роль в браузере -> http://127.0.0.1:{SETUP_PORT}")
    webbrowser.open(f"http://127.0.0.1:{SETUP_PORT}")
    web.run_app(app, host="127.0.0.1", port=SETUP_PORT, print=None)
    return chosen.get("role")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["host", "client"])
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = state.load_config()
    if args.reset:
        cfg["role"] = None
        state.save_config(cfg)
        print("[App_Remote] Роль сброшена.")
    if args.role:
        cfg["role"] = args.role
        state.save_config(cfg)

    role = cfg.get("role")
    if not role:
        role = run_setup()
        if not role:
            sys.exit(0)
        cfg = state.load_config()

    if role == "host":
        from core import host_server
        url = f"http://127.0.0.1:{cfg['host_port']}"
        print(f"[App_Remote] Роль: ХОСТ. Панель владельца: {url}")
        print(f"[App_Remote] Клиенты в LAN найдут этот хост автоматически (UDP {cfg['discovery_port']}).")
        print("[App_Remote] ВНИМАНИЕ: разрешите доступ в брандмауэре Windows при первом запуске.")
        if not args.no_browser:
            webbrowser.open(url)
        host_server.run(cfg)
    else:
        from core import client_app
        url = f"http://127.0.0.1:{cfg['client_port']}"
        print(f"[App_Remote] Роль: КЛИЕНТ. Панель: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        client_app.run(cfg)


if __name__ == "__main__":
    main()
