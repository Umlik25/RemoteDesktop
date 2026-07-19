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


def _node_fields(data):
    node_type = data.get("node_type", "physical")
    if node_type not in ("physical", "vm"):
        node_type = "physical"
    return {
        "node_type": node_type,
        "node_detected": (data.get("node_detected")
                          if data.get("node_detected") in ("physical", "vm") else None),
        "hypervisor": str(data.get("hypervisor") or "")[:64] or None,
        "parent_host": str(data.get("parent_host") or "")[:64] or None,
    }


def _manual_host(data):
    try:
        ip = str(ipaddress.ip_address(str(data["ip"]).strip()))
        if ":" in ip:
            raise ValueError("IPv6 пока не поддерживается веб-клиентом")
        port = int(data.get("port", 8532))
        if not 1 <= port <= 65535:
            raise ValueError("Недопустимый порт")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(str(error) or "Введите корректный IPv4-адрес и порт") from error
    node_type = data.get("node_type", "physical")
    if node_type not in ("physical", "vm"):
        raise ValueError("Неизвестный тип узла")
    name = str(data.get("name") or ip).strip()
    parent_host = str(data.get("parent_host") or "").strip()
    for value, label in ((name, "Название"), (parent_host, "Имя физического хоста")):
        if len(value) > 64 or any(ord(ch) < 32 for ch in value):
            raise ValueError(f"{label} должно быть короче 65 символов")
    return {
        "app": "App_Remote", "role": "host", "name": name or ip,
        "ip": ip, "port": port, "manual": True, "node_type": node_type,
        "parent_host": parent_host or None,
        "accepting": True, "last_seen": time.time(),
    }


def _save_manual_hosts():
    fields = ("name", "ip", "port", "node_type", "parent_host")
    saved = [{key: host.get(key) for key in fields}
             for host in _hosts.values() if host.get("manual")]
    state.save_client_hosts(saved)


def _restore_manual_hosts():
    for key, host in list(_hosts.items()):
        if host.get("manual"):
            _hosts.pop(key, None)
    for saved in state.load_client_hosts():
        try:
            host = _manual_host(saved)
        except ValueError:
            continue
        _hosts[f"{host['ip']}:{host['port']}"] = host


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
            "link_mbps": d.get("link_mbps"),
            "accepting": bool(d.get("accepting", True)), "last_seen": time.time(),
            **_node_fields(d),
        }
        key = f"{addr[0]}:{port}"
        existing = _hosts.get(key) or {}
        if existing.get("manual"):
            clean["manual"] = True
            clean["name"] = existing.get("name") or clean["name"]
            if "node_type" not in d:
                clean["node_type"] = existing.get("node_type", "physical")
            if not clean.get("parent_host"):
                clean["parent_host"] = existing.get("parent_host")
        _hosts[key] = clean


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
    out.sort(key=lambda item: (item.get("node_type") == "vm",
                               str(item.get("name") or "").lower()))
    return web.json_response(out)


async def api_add_host(request):
    """Ручное добавление хоста по ip:port (для сетей без broadcast)."""
    d = await request.json()
    try:
        host = _manual_host(d)
    except ValueError as error:
        raise web.HTTPBadRequest(text=str(error)) from error
    _hosts[f"{host['ip']}:{host['port']}"] = host
    _save_manual_hosts()
    return web.json_response({"ok": True})


async def api_update_host(request):
    d = await request.json()
    try:
        old_ip = str(ipaddress.ip_address(str(d["original_ip"]).strip()))
        old_port = int(d.get("original_port", 8532))
        host = _manual_host(d)
    except (KeyError, TypeError, ValueError) as error:
        raise web.HTTPBadRequest(text=str(error)) from error
    old_key = f"{old_ip}:{old_port}"
    if not _hosts.get(old_key, {}).get("manual"):
        raise web.HTTPNotFound(text="Сохранённая среда не найдена")
    new_key = f"{host['ip']}:{host['port']}"
    if new_key != old_key and new_key in _hosts:
        raise web.HTTPConflict(text="Среда с таким адресом уже существует")
    _hosts.pop(old_key, None)
    _hosts[new_key] = host
    _save_manual_hosts()
    return web.json_response({"ok": True})


async def api_remove_host(request):
    d = await request.json()
    try:
        ip = str(ipaddress.ip_address(str(d["ip"]).strip()))
        port = int(d.get("port", 8532))
    except (KeyError, TypeError, ValueError) as error:
        raise web.HTTPBadRequest(text="Некорректный адрес") from error
    key = f"{ip}:{port}"
    if not _hosts.get(key, {}).get("manual"):
        raise web.HTTPNotFound(text="Сохранённая среда не найдена")
    _hosts.pop(key, None)
    _save_manual_hosts()
    return web.json_response({"ok": True})


async def api_app_role(request):
    d = await request.json()
    role = d.get("role")
    if role not in ("host", "client"):
        raise web.HTTPBadRequest(text="Неизвестная роль")
    cfg = state.load_config()
    cfg["role"] = role
    state.save_config(cfg)

    def stop_app():
        raise web.GracefulExit()

    asyncio.get_running_loop().call_later(0.4, stop_app)
    return web.json_response({"ok": True, "role": role,
                              "port": cfg["host_port"] if role == "host" else cfg["client_port"]})


def build_app(config):
    app = web.Application()
    app.router.add_get("/", page_client)
    app.router.add_get("/viewer", page_viewer)
    app.router.add_static("/static/", state.WEB_DIR)
    app.router.add_get("/api/hosts", api_hosts)
    app.router.add_post("/api/hosts/add", api_add_host)
    app.router.add_post("/api/hosts/update", api_update_host)
    app.router.add_post("/api/hosts/remove", api_remove_host)
    app.router.add_post("/api/app/role", api_app_role)

    async def on_start(app):
        _restore_manual_hosts()
        asyncio.create_task(_discovery_listener(config.get("discovery_port", 8533)))
    app.on_startup.append(on_start)
    return app


def run(config):
    web.run_app(build_app(config), host="127.0.0.1", port=config["client_port"], print=None)
