"""Клиентское приложение: локальная панель + обнаружение хостов в LAN.

Панель доступна только на 127.0.0.1. Страница-вьювер подключается к хосту
напрямую по WebSocket (LAN Direct) — трафик не ходит через третьи узлы.
"""
import asyncio
import ipaddress
import json
import socket
import time

from aiohttp import web

from . import state

_hosts = {}  # "ip:port" -> {beacon..., "last_seen": ts}


async def _discovery_listener(port):
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):  # macOS/Linux: разрешить повторную привязку
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    bound = False
    for _ in range(10):
        try:
            sock.bind(("", port))
            bound = True
            break
        except OSError:
            await asyncio.sleep(1)
    if not bound:
        print(f"[App_Remote] Не удалось открыть UDP-порт {port} для обнаружения "
              f"(занят или заблокирован брандмауэром). Добавляйте хосты вручную по IP.")
        return
    print(f"[App_Remote] LAN-обнаружение слушает UDP {port}")
    sock.setblocking(False)
    while True:
        try:
            data, addr = await loop.sock_recvfrom(sock, 4096)
        except OSError:
            await asyncio.sleep(1)
            continue
        try:
            d = json.loads(data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if d.get("app") != "App_Remote" or d.get("role") != "host":
            continue
        try:
            port = int(d.get("port"))
            if not 1 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            continue
        clean = {
            "app": "App_Remote", "role": "host", "ip": addr[0], "port": port,
            "name": str(d.get("name") or addr[0])[:64],
            "cpu": str(d.get("cpu") or "")[:200],
            "gpu": str(d.get("gpu") or "")[:200],
            "cores": d.get("cores"), "threads": d.get("threads"),
            "ram_gb": d.get("ram_gb"), "sessions": d.get("sessions"),
            "accepting": bool(d.get("accepting", True)), "last_seen": time.time(),
        }
        _hosts[f"{addr[0]}:{port}"] = clean


async def page_client(request):
    return web.FileResponse(f"{state.WEB_DIR}/client.html")


async def page_viewer(request):
    return web.FileResponse(f"{state.WEB_DIR}/viewer.html")


async def api_hosts(request):
    now = time.time()
    out = []
    for key, h in list(_hosts.items()):
        if not h.get("manual") and now - h["last_seen"] > 15:
            _hosts.pop(key, None)
            continue
        out.append({**h, "age_s": 0 if h.get("manual") else round(now - h["last_seen"], 1)})
    return web.json_response(out)


async def api_add_host(request):
    """Ручное добавление хоста по ip:port (для сетей без broadcast)."""
    d = await request.json()
    try:
        ip = str(ipaddress.ip_address(str(d["ip"]).strip()))
        if ":" in ip:
            raise ValueError("IPv6 пока не поддерживается веб-клиентом")
        port = int(d.get("port", 8532))
        if not 1 <= port <= 65535:
            raise ValueError("Недопустимый порт")
    except (KeyError, TypeError, ValueError) as e:
        raise web.HTTPBadRequest(text=str(e) or "Введите корректный IPv4-адрес и порт")
    _hosts[f"{ip}:{port}"] = {"app": "App_Remote", "role": "host", "name": d.get("name", ip),
                              "ip": ip, "port": port, "manual": True,
                              "accepting": True, "last_seen": time.time()}
    return web.json_response({"ok": True})


def build_app(config):
    app = web.Application()
    app.router.add_get("/", page_client)
    app.router.add_get("/viewer", page_viewer)
    app.router.add_static("/static/", state.WEB_DIR)
    app.router.add_get("/api/hosts", api_hosts)
    app.router.add_post("/api/hosts/add", api_add_host)

    async def on_start(app):
        asyncio.create_task(_discovery_listener(config.get("discovery_port", 8533)))
    app.on_startup.append(on_start)
    return app


def run(config):
    web.run_app(build_app(config), host="127.0.0.1", port=config["client_port"], print=None)
