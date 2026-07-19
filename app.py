"""App_Remote — удалённый доступ к вычислительным ресурсам (MVP 0.1).

Запуск:
    python app.py                       # первый запуск: выбор роли в браузере
    python app.py --role host           # принудительно роль хоста
    python app.py --role host --node-type vm --host-name "Игровая VM"
    python app.py --role client
    python app.py --reset               # сбросить выбор роли
    python app.py --list-users          # показать пользователей хоста
    python app.py --reset-password ИМЯ  # сменить пароль пользователю (локально)
"""
import argparse
import asyncio
import getpass
import platform
import secrets
import sys
import webbrowser

from aiohttp import web

from core import state

SETUP_PORT = 8599


def cmd_list_users():
    from core import auth
    users = auth.list_users()
    if not users:
        print("[App_Remote] Пользователей нет (хост ещё не настроен).")
        return
    print(f"[App_Remote] Пользователи ({len(users)}):")
    for u in users:
        flag = " [ЗАБЛОКИРОВАН]" if u["blocked"] else ""
        print(f"  - {u['username']:<20} роль={u['role']:<8} профиль={u['profile']}{flag}")


def cmd_reset_password(username, new_password=None):
    """Смена пароля без остановки сервера и без сброса остальных данных.
    Запущенный хост подхватит новый пароль сразу (читает users.json при входе)."""
    from core import auth
    if not auth.get_user(username):
        print(f"[App_Remote] Нет пользователя «{username}».")
        cmd_list_users()
        sys.exit(1)
    generated = False
    if not new_password:
        try:
            new_password = getpass.getpass("Новый пароль (Enter — сгенерировать): ").strip()
        except (EOFError, KeyboardInterrupt):
            new_password = ""
        if not new_password:
            new_password = secrets.token_urlsafe(9)
            generated = True
    try:
        auth.set_password(username, new_password, actor="cli:reset-password")
    except ValueError as e:
        print(f"[App_Remote] Ошибка: {e}")
        sys.exit(1)
    print(f"[App_Remote] Пароль для «{username}» изменён. "
          "Активные сессии не тронуты; прежние токены входа отозваны.")
    if generated:
        print(f"[App_Remote] Сгенерированный пароль: {new_password}")


def run_setup():
    """Мини-сервер выбора роли: сохраняет выбор и продолжает запуск."""
    chosen = {}

    async def page(request):
        return web.FileResponse(f"{state.WEB_DIR}/setup.html")

    async def setup_info(request):
        from core import hwinfo
        cfg = state.load_config()
        return web.json_response({
            "hostname": platform.node() or "Компьютер",
            "node_type": cfg.get("node_type", "auto"),
            "host_name": cfg.get("host_name"),
            "parent_host": cfg.get("parent_host"),
            "detected": hwinfo.get_machine_environment(),
        })

    async def choose(request):
        d = await request.json()
        role = d.get("role")
        if role not in ("host", "client"):
            raise web.HTTPBadRequest()
        cfg = state.load_config()
        cfg["role"] = role
        if role == "host":
            node_type = str(d.get("node_type") or "auto")
            if node_type not in ("auto", "physical", "vm"):
                raise web.HTTPBadRequest(text="Неизвестный тип среды")
            host_name = str(d.get("host_name") or "").strip()
            parent_host = str(d.get("parent_host") or "").strip()
            for value, label in ((host_name, "Имя среды"),
                                 (parent_host, "Имя физического хоста")):
                if len(value) > 64 or any(ord(ch) < 32 for ch in value):
                    raise web.HTTPBadRequest(text=f"{label} должно быть короче 65 символов")
            cfg["node_type"] = node_type
            cfg["host_name"] = host_name or None
            cfg["parent_host"] = parent_host or None
        state.save_config(cfg)
        chosen["role"] = role

        def _stop():
            raise web.GracefulExit()
        asyncio.get_running_loop().call_later(0.4, _stop)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_static("/static/", state.WEB_DIR)
    app.router.add_get("/api/setup-info", setup_info)
    app.router.add_post("/api/choose", choose)
    print(f"[App_Remote] Первый запуск: выберите роль в браузере -> http://127.0.0.1:{SETUP_PORT}")
    webbrowser.open(f"http://127.0.0.1:{SETUP_PORT}")
    web.run_app(app, host="127.0.0.1", port=SETUP_PORT, print=None)
    return chosen.get("role")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["host", "client"])
    ap.add_argument("--node-type", choices=["auto", "physical", "vm"],
                    help="тип публикуемого узла: авто, физический хост или VM")
    ap.add_argument("--host-name", help="имя узла, видимое клиентам")
    ap.add_argument("--parent-host", help="имя физического хоста, на котором работает VM")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--list-users", action="store_true", help="показать пользователей и выйти")
    ap.add_argument("--reset-password", metavar="ИМЯ", help="сменить пароль пользователю и выйти")
    ap.add_argument("--password", metavar="ПАРОЛЬ", help="новый пароль (иначе спросит/сгенерирует)")
    args = ap.parse_args()

    if args.list_users:
        cmd_list_users()
        return
    if args.reset_password:
        cmd_reset_password(args.reset_password, args.password)
        return

    cfg = state.load_config()
    if args.reset:
        cfg["role"] = None
        state.save_config(cfg)
        print("[App_Remote] Роль сброшена.")
    if args.role:
        cfg["role"] = args.role
        state.save_config(cfg)
    identity_changed = False
    if args.node_type:
        cfg["node_type"] = args.node_type
        identity_changed = True
    if args.host_name is not None:
        cfg["host_name"] = args.host_name.strip() or None
        identity_changed = True
    if args.parent_host is not None:
        cfg["parent_host"] = args.parent_host.strip() or None
        identity_changed = True
    if identity_changed:
        state.save_config(cfg)

    first_server = True
    while True:
        cfg = state.load_config()
        role = cfg.get("role")
        if not role:
            role = run_setup()
            if not role:
                return
            cfg = state.load_config()

        if role == "host":
            from core import host_server
            url = f"http://127.0.0.1:{cfg['host_port']}"
            print(f"[App_Remote] Роль: ХОСТ. Панель владельца: {url}")
            print(f"[App_Remote] Клиенты в LAN найдут этот хост автоматически (UDP {cfg['discovery_port']}).")
            print("[App_Remote] ВНИМАНИЕ: разрешите доступ в брандмауэре Windows при первом запуске.")
            if first_server and not args.no_browser:
                webbrowser.open(url)
            host_server.run(cfg)
        else:
            from core import client_app
            url = f"http://127.0.0.1:{cfg['client_port']}"
            print(f"[App_Remote] Роль: КЛИЕНТ. Панель: {url}")
            if first_server and not args.no_browser:
                webbrowser.open(url)
            client_app.run(cfg)

        first_server = False
        if state.load_config().get("role") == role:
            break
        print("[App_Remote] Роль изменена из интерфейса, перезапускаю локальную панель...")


if __name__ == "__main__":
    main()
