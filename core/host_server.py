"""Сервер хоста: панель владельца, API, WebSocket-стриминг, LAN-beacon.

MVP-ограничения (честно):
- Видео = MJPEG (JPEG-кадры по WebSocket). H.264/HEVC/AV1 через аппаратные
  энкодеры — в дорожной карте (см. ARCHITECTURE.md).
- Все удалённые сессии в MVP видят ОДИН рабочий стол хоста (demo shared
  desktop). Полная изоляция (VM/отдельные сеансы ОС) — этап 2, требует
  KVM/Hyper-V; архитектура и API уже рассчитаны на неё (у каждой сессии
  свой профиль, права, журнал).
- Трафик не шифруется TLS в MVP — использовать в доверенной LAN.
"""
import asyncio
import concurrent.futures
import io
import ipaddress
import json
import socket
import time
import uuid

import psutil
from aiohttp import web, WSMsgType

from . import auth, capability, hwinfo, state

try:
    import mss
    from PIL import Image
    CAPTURE_OK = True
except Exception:
    CAPTURE_OK = False

try:
    from pynput.mouse import Controller as MouseController, Button
    from pynput.keyboard import Controller as KeyController, Key
    INPUT_OK = True
except Exception:
    INPUT_OK = False

PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------- захват экрана

class ScreenCapture:
    """Отдельный поток захвата: хранит последний сырой кадр."""

    def __init__(self):
        self.frame = None          # (bytes BGRA, width, height, ts)
        self.running = False
        self.max_fps = 30
        self._thread = None

    def start(self):
        if not CAPTURE_OK or self.running:
            return
        import threading
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def _loop(self):
        with mss.mss() as sct:
            mon = sct.monitors[1]
            while self.running:
                t0 = time.perf_counter()
                try:
                    img = sct.grab(mon)
                    self.frame = (img.bgra, img.width, img.height, time.time())
                except Exception:
                    time.sleep(0.5)
                    continue
                dt = time.perf_counter() - t0
                delay = max(0.0, 1.0 / max(self.max_fps, 1) - dt)
                time.sleep(delay)


def encode_jpeg(frame, quality, scale):
    raw, w, h, ts = frame
    img = Image.frombytes("RGB", (w, h), raw, "raw", "BGRX")
    if scale < 0.999:
        img = img.resize((max(2, int(w * scale)) // 2 * 2, max(2, int(h * scale)) // 2 * 2))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=int(quality))
    return buf.getvalue(), img.width, img.height, ts


# ---------------------------------------------------------------- ввод

KEY_MAP = {}
if INPUT_OK:
    KEY_MAP = {
        "Enter": Key.enter, "Backspace": Key.backspace, "Tab": Key.tab,
        "Escape": Key.esc, "Delete": Key.delete, "Insert": Key.insert,
        "Home": Key.home, "End": Key.end, "PageUp": Key.page_up, "PageDown": Key.page_down,
        "ArrowUp": Key.up, "ArrowDown": Key.down, "ArrowLeft": Key.left, "ArrowRight": Key.right,
        "Shift": Key.shift, "Control": Key.ctrl, "Alt": Key.alt, "Meta": Key.cmd,
        "CapsLock": Key.caps_lock, " ": Key.space,
        **{f"F{i}": getattr(Key, f"f{i}") for i in range(1, 13)},
    }


class InputInjector:
    def __init__(self):
        self.mouse = MouseController() if INPUT_OK else None
        self.kb = KeyController() if INPUT_OK else None
        self.screen_wh = None

    def apply(self, ev):
        if not INPUT_OK:
            return
        t = ev.get("t")
        try:
            if t == "mm" and self.screen_wh:
                self.mouse.position = (ev["x"] * self.screen_wh[0], ev["y"] * self.screen_wh[1])
            elif t == "mb":
                btn = {0: Button.left, 1: Button.middle, 2: Button.right}.get(ev.get("b", 0), Button.left)
                (self.mouse.press if ev.get("d") else self.mouse.release)(btn)
            elif t == "wh":
                self.mouse.scroll(0, -ev.get("dy", 0) / 100)
            elif t == "kb":
                key = ev.get("k", "")
                k = KEY_MAP.get(key, key if len(key) == 1 else None)
                if k is not None:
                    (self.kb.press if ev.get("d") else self.kb.release)(k)
        except Exception:
            pass


def clipboard_get():
    if hwinfo.IS_WIN:
        return hwinfo._ps("Get-Clipboard -Raw")
    return ""


def clipboard_set(text):
    if hwinfo.IS_WIN:
        import subprocess
        p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, creationflags=0x08000000)
        p.communicate(text.encode("utf-16-le"))


# ---------------------------------------------------------------- сессии

class Session:
    def __init__(self, ws, username, user, ip, route):
        self.sid = uuid.uuid4().hex[:10]
        self.ws = ws
        self.username = username
        self.user = user
        self.ip = ip
        self.route = route
        self.connected_at = time.time()
        prof = user.get("profile", "office")
        base = capability.STREAM_PROFILES.get(prof, capability.STREAM_PROFILES["office"])
        self.profile = prof
        self.fps = min(base["fps"], user.get("max_fps", 60))
        self.quality = base["quality"]
        self.scale = base["scale"]
        self.degrade = 0           # 0..4 — ступень деградации
        self.priority = user.get("priority", "normal")
        self.frames = 0
        self.bytes = 0
        self.last_bytes_ts = time.time()
        self.bitrate_mbps = 0.0
        self.warnings = []
        self.kick_reason = None

    def effective(self):
        """Параметры с учётом ступени деградации: битрейт → FPS → разрешение."""
        q, f, s = self.quality, self.fps, self.scale
        if self.degrade >= 1:
            q = max(30, q - 20)
        if self.degrade >= 2:
            f = max(10, f // 2)
        if self.degrade >= 3:
            s = min(s, 0.5)
        return f, q, s

    def info(self):
        f, q, s = self.effective()
        return {
            "sid": self.sid, "username": self.username, "ip": self.ip,
            "route": self.route, "profile": self.profile,
            "priority": self.priority,
            "connected_at": time.strftime("%H:%M:%S", time.localtime(self.connected_at)),
            "fps_target": f, "quality": q, "scale": s,
            "degrade": self.degrade,
            "bitrate_mbps": round(self.bitrate_mbps, 1),
            "frames": self.frames,
        }


class HostServer:
    def __init__(self, config):
        self.config = config
        self.capture = ScreenCapture()
        self.injector = InputInjector()
        self.sessions = {}
        self.static_info = None
        self.bench = None
        self.app = self._build_app()

    # ------------------------------------------------------------ HTTP app

    def _build_app(self):
        @web.middleware
        async def cors(request, handler):
            if request.method == "OPTIONS":
                resp = web.Response()
            else:
                try:
                    resp = await handler(request)
                except web.HTTPException as e:
                    resp = e
            resp.headers["Access-Control-Allow-Origin"] = "*"
            resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            return resp

        app = web.Application(middlewares=[cors])
        r = app.router
        r.add_get("/", self.page_panel)
        r.add_static("/static/", state.WEB_DIR)
        r.add_get("/api/info", self.api_info)
        r.add_post("/api/login", self.api_login)
        r.add_post("/api/redeem", self.api_redeem)
        r.add_post("/api/setup_owner", self.api_setup_owner)
        r.add_get("/api/report", self.api_report)
        r.add_get("/api/load", self.api_load)
        r.add_get("/api/users", self.api_users)
        r.add_post("/api/users/create", self.api_user_create)
        r.add_post("/api/users/update", self.api_user_update)
        r.add_post("/api/users/password", self.api_user_password)
        r.add_post("/api/users/delete", self.api_user_delete)
        r.add_get("/api/invites", self.api_invites)
        r.add_post("/api/invites/create", self.api_invite_create)
        r.add_post("/api/invites/revoke", self.api_invite_revoke)
        r.add_get("/api/sessions", self.api_sessions)
        r.add_post("/api/sessions/kick", self.api_kick)
        r.add_post("/api/settings", self.api_settings)
        r.add_get("/api/audit", self.api_audit)
        r.add_get("/api/history", self.api_history)
        r.add_get("/ws/panel", self.ws_panel)
        r.add_get("/ws/stream", self.ws_stream)
        app.on_startup.append(self._on_start)
        return app

    async def _on_start(self, app):
        loop = asyncio.get_running_loop()
        self.static_info = await loop.run_in_executor(_executor, hwinfo.get_static_info)
        self.bench = await loop.run_in_executor(_executor, hwinfo.quick_benchmark)
        if CAPTURE_OK:
            self.capture.start()
            for _ in range(50):
                if self.capture.frame:
                    break
                await asyncio.sleep(0.1)
            if self.capture.frame:
                self.injector.screen_wh = (self.capture.frame[1], self.capture.frame[2])
        asyncio.create_task(self._beacon_loop())
        asyncio.create_task(self._governor_loop())
        state.audit("host.start", "system", {"port": self.config["host_port"]})

    # ------------------------------------------------------------ утилиты

    def _is_local(self, request):
        peer = request.remote or ""
        return peer in ("127.0.0.1", "::1")

    def _admin(self, request):
        """Локальная консоль владельца или токен owner/admin."""
        if self._is_local(request):
            return "owner(local)"
        token = (request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                 or request.query.get("token", ""))
        username, u = auth.check_token(token)
        if u and u.get("role") in ("owner", "admin"):
            return username
        return None

    def _route_for(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_loopback:
                return "LAN Direct"
            for name, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == socket.AF_INET and a.netmask:
                        try:
                            net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
                            if addr in net:
                                return "LAN Direct"
                        except ValueError:
                            continue
            return "Internet Direct"
        except ValueError:
            return "Internet Direct"

    def host_name(self):
        return self.config.get("host_name") or self.static_info.get("hostname", "Host")

    # ------------------------------------------------------------ страницы

    async def page_panel(self, request):
        return web.FileResponse(f"{state.WEB_DIR}/host.html")

    # ------------------------------------------------------------ API

    async def api_info(self, request):
        s = self.static_info or {}
        return web.json_response({
            "app": "App_Remote", "version": "0.1.0", "role": "host",
            "name": self.host_name(),
            "os": s.get("os"), "cpu": s.get("cpu"),
            "cores": s.get("cores"), "threads": s.get("threads"),
            "ram_gb": s.get("ram_gb"),
            "gpus": s.get("gpus"),
            "encoders": s.get("encoders"),
            "accepting": self.config.get("accepting", True),
            "sessions": len(self.sessions),
            "max_sessions": self.config.get("max_sessions", 4),
            "needs_owner_setup": not auth.has_users(),
        })

    async def api_setup_owner(self, request):
        """Первичное создание учётной записи владельца — только локально."""
        if not self._is_local(request):
            raise web.HTTPForbidden(text="only local")
        if auth.has_users():
            raise web.HTTPBadRequest(text="owner уже создан")
        d = await request.json()
        auth.create_user(d["username"], d["password"], role="owner",
                         profile="dev", priority="critical", actor="setup")
        return web.json_response({"ok": True})

    async def api_login(self, request):
        d = await request.json()
        u = auth.verify(d.get("username", ""), d.get("password", ""))
        if not u:
            state.audit("login.fail", d.get("username", "?"), {"ip": request.remote})
            raise web.HTTPUnauthorized(text="Неверные учётные данные, блокировка или истёкший доступ")
        token = auth.issue_token(d["username"])
        state.audit("login.ok", d["username"], {"ip": request.remote})
        return web.json_response({"token": token, "role": u["role"],
                                  "profile": u.get("profile", "office"),
                                  "permissions": {
                                      "input": u.get("allow_input", True),
                                      "clipboard": u.get("allow_clipboard", False),
                                      "files": u.get("allow_files", False)}})

    async def api_redeem(self, request):
        d = await request.json()
        try:
            auth.redeem_invite(d["code"], d["username"], d["password"])
        except (ValueError, KeyError) as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True})

    async def api_report(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        load = hwinfo.get_load()
        rep = capability.build_report(self.static_info, self.bench, load, self.config)
        cap = capability.capacity_plan(self.static_info, self.bench, load, self.config)
        return web.json_response({"static": self.static_info, "bench": self.bench,
                                  "load": load, "report": rep, "capacity": cap,
                                  "config": self.config})

    async def api_load(self, request):
        return web.json_response(hwinfo.get_load())

    async def api_users(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        return web.json_response(auth.list_users())

    async def api_user_create(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        try:
            auth.create_user(d["username"], d["password"],
                             role=d.get("role", "user"), profile=d.get("profile", "office"),
                             priority=d.get("priority", "normal"),
                             allow_input=d.get("allow_input", True),
                             allow_clipboard=d.get("allow_clipboard", False),
                             allow_files=d.get("allow_files", False),
                             max_fps=int(d.get("max_fps", 60)), actor=actor)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True})

    async def api_user_update(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        try:
            auth.update_user(d["username"], d.get("fields", {}), actor=actor)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        if d.get("fields", {}).get("blocked"):
            auth.revoke_user_tokens(d["username"])
            for s in list(self.sessions.values()):
                if s.username == d["username"]:
                    await self._kick(s, "Пользователь заблокирован")
        return web.json_response({"ok": True})

    async def api_user_password(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        try:
            auth.set_password(d["username"], d.get("password", ""), actor=actor)
        except (ValueError, KeyError) as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True})

    async def api_user_delete(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        auth.delete_user(d["username"], actor=actor)
        for s in list(self.sessions.values()):
            if s.username == d["username"]:
                await self._kick(s, "Доступ отозван")
        return web.json_response({"ok": True})

    async def api_invites(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        return web.json_response(auth.list_invites())

    async def api_invite_create(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        code = auth.create_invite(role=d.get("role", "guest"), profile=d.get("profile", "office"),
                                  ttl_hours=float(d.get("ttl_hours", 24)),
                                  priority=d.get("priority", "low"),
                                  session_hours=d.get("session_hours"), actor=actor)
        return web.json_response({"code": code})

    async def api_invite_revoke(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        auth.revoke_invite(d["code"], actor=actor)
        return web.json_response({"ok": True})

    async def api_sessions(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        return web.json_response([s.info() for s in self.sessions.values()])

    async def api_kick(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        s = self.sessions.get(d.get("sid"))
        if s:
            state.audit("session.kick", actor, {"sid": s.sid, "username": s.username})
            await self._kick(s, d.get("reason", "Сессия завершена администратором"))
        return web.json_response({"ok": True})

    async def api_settings(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        for k in ("accepting", "work_only_mode", "owner_gaming_mode",
                  "owner_reserve_percent", "max_sessions", "host_name"):
            if k in d:
                self.config[k] = d[k]
        state.save_config(self.config)
        state.audit("settings.update", actor, d)
        return web.json_response({"ok": True, "config": self.config})

    async def api_audit(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        return web.json_response(state.audit_tail())

    async def api_history(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        return web.json_response(state.session_history())

    # ------------------------------------------------------------ WS: панель

    async def ws_panel(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        try:
            while not ws.closed:
                await ws.send_json({
                    "type": "tick",
                    "load": hwinfo.get_load(),
                    "sessions": [s.info() for s in self.sessions.values()],
                    "config": self.config,
                    "capture_ok": CAPTURE_OK, "input_ok": INPUT_OK,
                })
                try:
                    await asyncio.wait_for(ws.receive(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            return ws

    # ------------------------------------------------------------ WS: стрим

    async def ws_stream(self, request):
        token = request.query.get("token", "")
        username, user = auth.check_token(token)
        if not user:
            raise web.HTTPUnauthorized()
        if not self.config.get("accepting", True) and user.get("role") not in ("owner", "admin"):
            raise web.HTTPForbidden(text="Хост не принимает новые подключения")
        guests = [s for s in self.sessions.values()]
        if len(guests) >= self.config.get("max_sessions", 4):
            raise web.HTTPForbidden(text="Достигнут лимит одновременных сессий")
        if not CAPTURE_OK:
            raise web.HTTPServiceUnavailable(text="Захват экрана недоступен (нет mss/Pillow)")

        ws = web.WebSocketResponse(max_msg_size=8 * 2**20)
        await ws.prepare(request)
        sess = Session(ws, username, user, request.remote, self._route_for(request.remote))
        self.sessions[sess.sid] = sess
        state.log_session_event("connect", {"sid": sess.sid, "username": username,
                                            "ip": sess.ip, "route": sess.route})
        await ws.send_json({"type": "hello", "sid": sess.sid,
                            "host_name": self.host_name(),
                            "route": sess.route,
                            "codec": "MJPEG (MVP; H.264/HEVC в дорожной карте)",
                            "profile": sess.profile,
                            "fps": sess.fps, "quality": sess.quality, "scale": sess.scale,
                            "permissions": {
                                "input": user.get("allow_input", True) and not self.config.get("work_only_mode"),
                                "clipboard": user.get("allow_clipboard", False),
                                "files": user.get("allow_files", False)},
                            "isolation_note": "MVP: все удалённые сессии видят общий рабочий стол хоста. "
                                              "Изоляция через VM — этап 2."})
        sender = asyncio.create_task(self._frame_sender(sess))
        stats = asyncio.create_task(self._stats_sender(sess))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    d = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t == "ping":
                    await ws.send_json({"type": "pong", "t": d.get("t")})
                elif t == "config":
                    sess.fps = min(int(d.get("fps", sess.fps)), user.get("max_fps", 60))
                    sess.quality = max(20, min(95, int(d.get("quality", sess.quality))))
                    sess.scale = max(0.25, min(1.0, float(d.get("scale", sess.scale))))
                    if d.get("profile") in capability.STREAM_PROFILES:
                        sess.profile = d["profile"]
                elif t == "input":
                    if user.get("allow_input", True) and not self.config.get("work_only_mode"):
                        for ev in d.get("events", []):
                            self.injector.apply(ev)
                elif t == "clipboard_get" and user.get("allow_clipboard"):
                    loop = asyncio.get_running_loop()
                    text = await loop.run_in_executor(_executor, clipboard_get)
                    await ws.send_json({"type": "clipboard", "text": text or ""})
                elif t == "clipboard_set" and user.get("allow_clipboard"):
                    await asyncio.get_running_loop().run_in_executor(
                        _executor, clipboard_set, d.get("text", ""))
        finally:
            sender.cancel()
            stats.cancel()
            self.sessions.pop(sess.sid, None)
            state.log_session_event("disconnect", {
                "sid": sess.sid, "username": username, "ip": sess.ip,
                "frames": sess.frames, "mb_sent": round(sess.bytes / 2**20, 1),
                "duration_s": int(time.time() - sess.connected_at),
                "reason": sess.kick_reason or "client"})
        return ws

    async def _frame_sender(self, sess):
        loop = asyncio.get_running_loop()
        last_ts = 0.0
        while not sess.ws.closed:
            fps, quality, scale = sess.effective()
            frame = self.capture.frame
            if frame is None or frame[3] <= last_ts:
                await asyncio.sleep(0.005)
                continue
            t0 = time.perf_counter()
            try:
                data, w, h, ts = await loop.run_in_executor(
                    _executor, encode_jpeg, frame, quality, scale)
            except Exception:
                await asyncio.sleep(0.1)
                continue
            last_ts = frame[3]
            try:
                await sess.ws.send_bytes(data)
            except (ConnectionError, RuntimeError):
                break
            sess.frames += 1
            sess.bytes += len(data)
            now = time.time()
            if now - sess.last_bytes_ts >= 1.0:
                sess.bitrate_mbps = sess.bytes * 8 / 1e6 / (now - sess.last_bytes_ts)
                sess.bytes = 0
                sess.last_bytes_ts = now
            spent = time.perf_counter() - t0
            await asyncio.sleep(max(0.0, 1.0 / max(fps, 1) - spent))

    async def _stats_sender(self, sess):
        while not sess.ws.closed:
            load = hwinfo.get_load()
            fps, quality, scale = sess.effective()
            warn = list(sess.warnings)
            if sess.degrade > 0:
                warn.append(f"Хост перегружен: применена ступень деградации {sess.degrade} "
                            f"(битрейт → FPS → разрешение)")
            try:
                await sess.ws.send_json({
                    "type": "stats", "host": load,
                    "session": sess.info(),
                    "codec": "MJPEG", "route": sess.route,
                    "fps_target": fps, "quality": quality, "scale": scale,
                    "warnings": warn,
                })
            except (ConnectionError, RuntimeError):
                break
            await asyncio.sleep(2.0)

    async def _kick(self, sess, reason):
        sess.kick_reason = reason
        try:
            await sess.ws.send_json({"type": "kick", "reason": reason})
            await sess.ws.close()
        except (ConnectionError, RuntimeError):
            pass
        self.sessions.pop(sess.sid, None)

    # ------------------------------------------------------------ регулятор нагрузки

    async def _governor_loop(self):
        """Предсказуемая деградация: битрейт → FPS → разрешение → пауза.
        Резерв владельца: если CPU выше (100 - reserve)%, гости деградируют
        по возрастанию приоритета."""
        while True:
            await asyncio.sleep(3.0)
            load = hwinfo.get_load()
            reserve = self.config.get("owner_reserve_percent", 25)
            if self.config.get("owner_gaming_mode"):
                reserve = max(reserve, 50)
            threshold = 100 - reserve
            ordered = sorted(self.sessions.values(),
                             key=lambda s: PRIORITY_ORDER.get(s.priority, 1))
            if load["cpu_percent"] > threshold or load["ram_percent"] > 92:
                for s in ordered:  # деградируем самых низкоприоритетных первыми
                    if s.degrade < 4:
                        s.degrade += 1
                        if s.degrade == 4:
                            s.warnings = ["Критическая перегрузка: сессия будет "
                                          "приостановлена при сохранении нагрузки"]
                        break
            elif load["cpu_percent"] < threshold - 15:
                for s in reversed(ordered):  # восстанавливаем самых приоритетных первыми
                    if s.degrade > 0:
                        s.degrade -= 1
                        s.warnings = []
                        break

    # ------------------------------------------------------------ LAN beacon

    def _broadcast_targets(self):
        """Broadcast-адреса ВСЕХ активных IPv4-интерфейсов + глобальный.

        На Windows с несколькими адаптерами (Wi-Fi + Ethernet + VPN вроде
        Radmin/Hamachi 26.x/25.x) отправка только на 255.255.255.255 часто
        уходит лишь через один интерфейс (нередко через VPN), и клиент в
        реальном Wi-Fi хост не видит. Направленный broadcast на каждую
        подсеть (например 192.168.1.255) маршрутизируется в нужный интерфейс.
        VPN-подсети (25.x/26.x) пропускаем, чтобы не светить хост в них.
        """
        port = self.config.get("discovery_port", 8533)
        directed = set()
        try:
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if a.family != socket.AF_INET or not a.netmask:
                        continue
                    try:
                        net = ipaddress.ip_network(f"{a.address}/{a.netmask}", strict=False)
                    except ValueError:
                        continue
                    if net.is_loopback:
                        continue
                    first = str(a.address).split(".")[0]
                    if first in ("25", "26"):  # типичные диапазоны Radmin VPN / Hamachi
                        continue
                    if net.broadcast_address.is_global or net.is_private:
                        directed.add((str(net.broadcast_address), port))
        except Exception:
            pass
        # Глобальный broadcast — только как fallback: он часто уходит через
        # VPN-адаптер и порождает дубль хоста. Если нашли реальные подсети —
        # шлём только на них.
        if directed:
            return list(directed)
        return [("255.255.255.255", port)]

    async def _beacon_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        first_pass = True
        while True:
            s = self.static_info or {}
            payload = json.dumps({
                "app": "App_Remote", "v": 1, "role": "host",
                "name": self.host_name(),
                "port": self.config["host_port"],
                "cpu": s.get("cpu"), "cores": s.get("cores"), "threads": s.get("threads"),
                "ram_gb": s.get("ram_gb"),
                "gpu": (s.get("gpus") or [{}])[0].get("name"),
                "accepting": self.config.get("accepting", True),
                "sessions": len(self.sessions),
            }).encode()
            targets = self._broadcast_targets()
            if first_pass:
                print(f"[App_Remote] LAN-обнаружение: рассылка на {[t[0] for t in targets]}")
                first_pass = False
            for tgt in targets:
                try:
                    await loop.run_in_executor(None, sock.sendto, payload, tgt)
                except OSError:
                    pass
            await asyncio.sleep(2.0)


def run(config):
    server = HostServer(config)
    web.run_app(server.app, host="0.0.0.0", port=config["host_port"], print=None)
