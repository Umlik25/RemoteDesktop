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
    _BILINEAR = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
except Exception:
    CAPTURE_OK = False

# Быстрый путь захвата: DXGI Desktop Duplication (dxcam) — 60+ FPS и
# аппаратное определение «кадр не изменился». Fallback — mss (GDI, ~30 FPS).
try:
    import dxcam
    DXCAM_OK = hwinfo.IS_WIN
except Exception:
    DXCAM_OK = False

# Быстрое кодирование: OpenCV (libjpeg-turbo, SIMD) кодирует JPEG в 3–5 раз
# быстрее Pillow — на 1440p это разница между ~20 и 60+ FPS. Fallback — Pillow.
try:
    import numpy as np
    import cv2
    CV2_OK = True
except Exception:
    CV2_OK = False

# GetCursorPos: mss не захватывает курсор, поэтому его позицию читаем отдельно
# и рисуем в кадр на стороне хоста.
_user32 = None
if hwinfo.IS_WIN:
    try:
        import ctypes

        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        _user32 = ctypes.windll.user32
    except Exception:
        _user32 = None

# Тип курсора хоста (стрелка/текст/рука/resize/…): читаем реальный HCURSOR через
# GetCursorInfo и сопоставляем со стандартными системными курсорами. Клиент затем
# показывает курсор нужной формы сам — курсор НЕ впечатывается в кадр (это убирает
# перекодирование при каждом движении мыши и делает курсор «живым» и адаптивным).
_CURSOR_TYPE_MAP = {}
_GetCursorInfo = None
_CURSORINFO = None
if _user32 is not None:
    try:
        class _CURSORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("flags", ctypes.c_uint),
                        ("hCursor", ctypes.c_void_p), ("ptScreenPos", _POINT)]

        _user32.LoadCursorW.restype = ctypes.c_void_p
        _user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        # ID стандартных курсоров Windows -> имя CSS-курсора
        _STD_CURSORS = {
            32512: "default", 32513: "text", 32514: "wait", 32515: "crosshair",
            32516: "default", 32642: "nwse-resize", 32643: "nesw-resize",
            32644: "ew-resize", 32645: "ns-resize", 32646: "move",
            32648: "not-allowed", 32649: "pointer", 32650: "progress", 32651: "help",
        }
        for _cid, _css in _STD_CURSORS.items():
            _h = _user32.LoadCursorW(None, _cid)
            if _h:
                _CURSOR_TYPE_MAP[int(_h)] = _css
        _GetCursorInfo = _user32.GetCursorInfo
    except Exception:
        _GetCursorInfo = None

# Точная инъекция мыши через SendInput (как в проф. remote-решениях): абсолютное
# позиционирование в нормированных координатах 0..65535 не зависит от DPI-мас-
# штабирования, корректно работает с боковыми кнопками и горизонтальным колесом,
# и надёжнее SetCursorPos. При любой осечке — откат на pynput.
_SENDINPUT_OK = False
if _user32 is not None:
    try:
        class _MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                        ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("mi", _MOUSEINPUT)]

        class _INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_uint32), ("u", _INPUTUNION)]

        _user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
        _user32.SendInput.restype = ctypes.c_uint
        _MEF = {"move": 0x0001, "abs": 0x8000, "ldown": 0x0002, "lup": 0x0004,
                "rdown": 0x0008, "rup": 0x0010, "mdown": 0x0020, "mup": 0x0040,
                "xdown": 0x0080, "xup": 0x0100, "wheel": 0x0800, "hwheel": 0x1000}
        _SENDINPUT_OK = True

        def _send_mouse(flags, dx=0, dy=0, data=0):
            inp = _INPUT()
            inp.type = 0  # INPUT_MOUSE
            inp.u.mi = _MOUSEINPUT(dx, dy, data & 0xffffffff, flags, 0, 0)
            return _user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
    except Exception:
        _SENDINPUT_OK = False

try:
    from pynput.mouse import Controller as MouseController, Button
    from pynput.keyboard import Controller as KeyController, Key, KeyCode
    INPUT_OK = True
except Exception:
    INPUT_OK = False

PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------- захват экрана

class ScreenCapture:
    """Отдельный поток захвата: хранит последний сырой кадр.

    seq растёт только когда содержимое экрана реально изменилось
    (memcmp предыдущего кадра) — статичный экран не кодируется и не шлётся,
    что убирает лишнюю нагрузку CPU и трафик."""

    def __init__(self):
        self.frame = None          # (bytes BGRA, width, height, ts, seq)
        self.running = False
        self.max_fps = 30
        self.mon_offset = (0, 0)   # left/top захватываемого монитора
        self.backend = None        # "dxgi" | "gdi"
        self.on_frame = None       # колбэк «появился новый кадр» (из потока захвата)
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
        if DXCAM_OK and self._loop_dxcam():
            return
        self._loop_mss()

    def _loop_dxcam(self):
        """DXGI Desktop Duplication: быстрый захват, grab() сам возвращает None,
        если экран не менялся. Возврат False -> откат на mss."""
        try:
            cam = dxcam.create(output_color="BGRA")
            if cam is None:
                return False
        except Exception:
            return False
        print("[App_Remote] Захват экрана: DXGI Desktop Duplication (быстрый)")
        self.backend = "dxgi"
        self.mon_offset = (0, 0)   # dxcam захватывает основной монитор с (0,0)
        seq = 0
        errors = 0
        try:
            while self.running:
                t0 = time.perf_counter()
                try:
                    f = cam.grab()
                    errors = 0
                except Exception:
                    errors += 1
                    if errors > 50:          # дупликатор умер (смена режима и т.п.)
                        print("[App_Remote] DXGI-захват сбоит — переключаюсь на GDI/mss")
                        return False
                    time.sleep(0.05)
                    continue
                if f is not None:
                    seq += 1
                    h, w = f.shape[:2]
                    self.frame = (f.tobytes(), w, h, time.time(), seq)
                    if self.on_frame:
                        self.on_frame()
                dt = time.perf_counter() - t0
                time.sleep(max(0.001, 1.0 / max(self.max_fps, 1) - dt))
        finally:
            try:
                cam.release()
            except Exception:
                pass
        return True

    def _loop_mss(self):
        prev = None
        seq = 0
        self.backend = "gdi"
        print("[App_Remote] Захват экрана: GDI/mss (запасной путь)")
        with mss.mss() as sct:
            mon = sct.monitors[1]
            self.mon_offset = (mon.get("left", 0), mon.get("top", 0))
            while self.running:
                t0 = time.perf_counter()
                try:
                    img = sct.grab(mon)
                    raw = img.bgra
                    if prev is None or raw != prev:   # memcmp: быстрый, с ранним выходом
                        seq += 1
                        self.frame = (raw, img.width, img.height, time.time(), seq)
                        prev = raw
                        if self.on_frame:
                            self.on_frame()
                except Exception:
                    time.sleep(0.5)
                    continue
                dt = time.perf_counter() - t0
                delay = max(0.0, 1.0 / max(self.max_fps, 1) - dt)
                time.sleep(delay)


def encode_jpeg(frame, quality, scale):
    raw, w, h, ts, seq = frame
    if CV2_OK:
        try:
            arr = np.frombuffer(raw, np.uint8).reshape(h, w, 4)
            if scale < 0.999:
                nw = max(2, int(w * scale)) // 2 * 2
                nh = max(2, int(h * scale)) // 2 * 2
                arr = cv2.resize(arr, (nw, nh), interpolation=cv2.INTER_AREA)
            bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
            if ok:
                return buf.tobytes(), bgr.shape[1], bgr.shape[0], ts
        except Exception:
            pass
    img = Image.frombytes("RGB", (w, h), raw, "raw", "BGRX")
    if scale < 0.999:
        img = img.resize((max(2, int(w * scale)) // 2 * 2,
                          max(2, int(h * scale)) // 2 * 2), _BILINEAR)
    buf = io.BytesIO()
    # subsampling=2 (4:2:0) — заметно меньше и быстрее при том же видимом качестве
    img.save(buf, "JPEG", quality=int(quality), subsampling=2)
    return buf.getvalue(), img.width, img.height, ts


# ---------------------------------------------------------------- ввод

KEY_MAP = {}
BTN_MAP = {}
_VK_PUNCT = {}
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
    for _name, _attr in (("ContextMenu", "menu"), ("AltGraph", "alt_gr"),
                         ("NumLock", "num_lock"), ("ScrollLock", "scroll_lock"),
                         ("Pause", "pause"), ("PrintScreen", "print_screen")):
        _k = getattr(Key, _attr, None)
        if _k is not None:
            KEY_MAP[_name] = _k
    BTN_MAP = {0: Button.left, 1: Button.middle, 2: Button.right}
    for _b, _attr in ((3, "x1"), (4, "x2")):   # боковые кнопки (назад/вперёд)
        _btn = getattr(Button, _attr, None)
        if _btn is not None:
            BTN_MAP[_b] = _btn
    # Виртуальные коды пунктуации US-раскладки: шорткаты (Ctrl+/, Ctrl+.)
    # работают независимо от текущей раскладки хоста.
    _VK_PUNCT = {".": 0xBE, ",": 0xBC, "/": 0xBF, ";": 0xBA, "'": 0xDE,
                 "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0, "-": 0xBD, "=": 0xBB}


def _char_key(ch):
    """Символ -> клавиша. Латиница/цифры/пунктуация идут виртуальными кодами
    (VK), которые не зависят от раскладки хоста: Ctrl+C работает, даже если на
    хосте включён русский. Остальное — как символ (Unicode-фолбэк pynput)."""
    if hwinfo.IS_WIN:
        if ch.isascii() and ch.isalnum():
            return KeyCode.from_vk(ord(ch.upper()))
        vk = _VK_PUNCT.get(ch)
        if vk is not None:
            return KeyCode.from_vk(vk)
    return ch


class InputInjector:
    def __init__(self):
        self.mouse = MouseController() if INPUT_OK else None
        self.kb = KeyController() if INPUT_OK else None
        self.screen_wh = None

    def _mouse_move(self, nx, ny):
        """nx,ny в 0..1. SendInput (абсолют, 0..65535) — если доступен; иначе pynput."""
        if _SENDINPUT_OK and _send_mouse(_MEF["move"] | _MEF["abs"],
                                         int(nx * 65535), int(ny * 65535)):
            return
        if self.screen_wh:
            x = int(nx * (self.screen_wh[0] - 1))
            y = int(ny * (self.screen_wh[1] - 1))
            self.mouse.position = (x, y)

    def _mouse_button(self, b, down):
        if _SENDINPUT_OK:
            pairs = {0: ("ldown", "lup"), 1: ("mdown", "mup"), 2: ("rdown", "rup")}
            if b in pairs:
                if _send_mouse(_MEF[pairs[b][0 if down else 1]]):
                    return
            elif b in (3, 4):  # боковые кнопки: XBUTTON1/2 в mouseData
                if _send_mouse(_MEF["xdown" if down else "xup"], data=(b - 2)):
                    return
        btn = BTN_MAP.get(b)
        if btn is not None:
            (self.mouse.press if down else self.mouse.release)(btn)

    def _mouse_wheel(self, dx, dy):
        # dx,dy — «щелчки» (клиент шлёт кратно 100). WHEEL_DELTA = 120 на щелчок.
        if _SENDINPUT_OK:
            done = True
            if dy:
                done &= bool(_send_mouse(_MEF["wheel"], data=(dy * 120) & 0xffffffff))
            if dx:
                done &= bool(_send_mouse(_MEF["hwheel"], data=(dx * 120) & 0xffffffff))
            if done:
                return
        if dx or dy:
            self.mouse.scroll(dx, dy)

    def apply(self, ev):
        if not INPUT_OK:
            return
        t = ev.get("t")
        try:
            if t == "mm":
                self._mouse_move(max(0.0, min(1.0, float(ev["x"]))),
                                 max(0.0, min(1.0, float(ev["y"]))))
            elif t == "mb":
                self._mouse_button(ev.get("b", 0), bool(ev.get("d")))
            elif t == "wh":
                self._mouse_wheel(int(float(ev.get("dx", 0)) / 100),
                                  int(-float(ev.get("dy", 0)) / 100))
            elif t == "kb":
                key = ev.get("k", "")
                k = KEY_MAP.get(key)
                if k is None and len(key) == 1:
                    k = _char_key(key)
                if k is not None:
                    (self.kb.press if ev.get("d") else self.kb.release)(k)
            elif t == "txt":
                # Текст печатается КАК ЕСТЬ (Unicode-инъекция): точка, кириллица,
                # любой символ — независимо от раскладок клиента и хоста.
                s = str(ev.get("s", ""))[:32]
                if s:
                    self.kb.type(s)
            elif t == "lang":
                # Win+Space — переключение языка ввода на хосте
                self.kb.press(Key.cmd)
                self.kb.press(Key.space)
                self.kb.release(Key.space)
                self.kb.release(Key.cmd)
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
        self.degrade = 0           # 0..4 — ступень деградации по нагрузке хоста
        self.net_degrade = 0       # 0..3 — деградация из-за пропускной способности сети
        self.priority = user.get("priority", "normal")
        self.frames = 0
        self.bytes = 0
        self.last_bytes_ts = time.time()
        self.bitrate_mbps = 0.0
        self.warnings = []
        self.kick_reason = None

    def effective(self):
        """Параметры с учётом деградации (нагрузка хоста + сеть):
        битрейт → FPS → разрешение."""
        q, f, s = self.quality, self.fps, self.scale
        lvl = min(4, self.degrade + self.net_degrade)
        if lvl >= 1:
            q = max(30, q - 20)
        if lvl >= 2:
            f = max(10, f // 2)
        if lvl >= 3:
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
            "degrade": self.degrade, "net_degrade": self.net_degrade,
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
        self._enc_jobs = {}        # (seq, q, scale) -> future с кодированным кадром
        self._load_cache = (0.0, None)
        self._load_task = None
        self._loop = None
        self._frame_evt = None     # событие «есть новый кадр» для отправителей
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
        r.add_post("/api/reset/create", self.api_reset_create)
        r.add_post("/api/reset/redeem", self.api_reset_redeem)
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
        self._loop = loop
        self._frame_evt = asyncio.Event()
        self.static_info = await loop.run_in_executor(_executor, hwinfo.get_static_info)
        self.bench = await loop.run_in_executor(_executor, hwinfo.quick_benchmark)
        if CAPTURE_OK:
            self.capture.on_frame = self._frame_notify_threadsafe
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

    def _input_permission(self, user):
        if not user or user.get("blocked"):
            return False, "user_denied"
        if not INPUT_OK:
            return False, "backend_unavailable"
        if not user.get("allow_input", True):
            return False, "user_denied"
        return True, None

    def _permission_payload(self, user):
        input_allowed, input_reason = self._input_permission(user)
        return {
            "input": input_allowed,
            "input_reason": input_reason,
            "work_only_mode": self.config.get("work_only_mode", False),
            "clipboard": bool(user and user.get("allow_clipboard", False)),
            "files": bool(user and user.get("allow_files", False)),
        }

    def _frame_notify_threadsafe(self):
        """Из потока захвата: разбудить отправителей кадров в event loop."""
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._notify_frame)
            except RuntimeError:
                pass

    def _notify_frame(self):
        ev = self._frame_evt
        if ev is not None:
            self._frame_evt = asyncio.Event()
            ev.set()

    async def _get_load(self):
        """hwinfo.get_load() без блокировки event loop и не чаще раза в ~1.5 с.

        Прямой вызов запускает nvidia-smi (100–300 мс) — раньше это делалось
        синхронно в трёх местах и регулярно замораживало стрим."""
        ts, val = self._load_cache
        if val is not None and time.monotonic() - ts < 1.5:
            return val
        if self._load_task is None:
            loop = asyncio.get_running_loop()
            self._load_task = asyncio.ensure_future(
                loop.run_in_executor(_executor, hwinfo.get_load))
        task = self._load_task
        try:
            val = await asyncio.shield(task)
        except Exception:
            return self._load_cache[1] or {}
        if self._load_task is task:
            self._load_cache = (time.monotonic(), val)
            self._load_task = None
        return val

    def _cursor_info(self):
        """Позиция + ТИП курсора хоста (в координатах кадра) или None.

        Тип (стрелка/текст/рука/resize/…) читаем через GetCursorInfo и отдаём
        клиенту — он рисует курсор нужной формы у себя. Так курсор адаптивный,
        двигается мгновенно и не заставляет перекодировать кадр."""
        frame = self.capture.frame
        if not frame:
            return None
        x = y = None
        ctype = "default"
        showing = True
        if _GetCursorInfo is not None:
            try:
                ci = _CURSORINFO()
                ci.cbSize = ctypes.sizeof(_CURSORINFO)
                if _GetCursorInfo(ctypes.byref(ci)):
                    showing = bool(ci.flags & 0x1)
                    x, y = ci.ptScreenPos.x, ci.ptScreenPos.y
                    ctype = _CURSOR_TYPE_MAP.get(int(ci.hCursor or 0), "default")
            except Exception:
                pass
        if x is None and _user32 is not None:
            try:
                pt = _POINT()
                if _user32.GetCursorPos(ctypes.byref(pt)):
                    x, y = pt.x, pt.y
            except Exception:
                pass
        if x is None and INPUT_OK and self.injector.mouse:
            try:
                p = self.injector.mouse.position
                x, y = int(p[0]), int(p[1])
            except Exception:
                pass
        if x is None:
            return None
        ox, oy = self.capture.mon_offset
        fx, fy = x - ox, y - oy
        on = 0 <= fx < frame[1] and 0 <= fy < frame[2]
        fx = max(0, min(frame[1] - 1, fx))
        fy = max(0, min(frame[2] - 1, fy))
        return {"x": fx, "y": fy, "type": ctype, "visible": bool(showing and on)}

    def _fps_cap(self, user):
        """Потолок FPS: владелец/админ управляют своим же хостом — им отдаём
        весь диапазон (до 240). Остальным — заданный админом лимит max_fps."""
        if user and user.get("role") in ("owner", "admin"):
            return 240
        return max(1, int((user or {}).get("max_fps", 60) or 60))

    def _stream_params(self, sess):
        """Эффективные FPS/качество/масштаб с учётом режима «только работа»."""
        fps, q, s = sess.effective()
        if self.config.get("work_only_mode") and sess.user.get("role") not in ("owner", "admin"):
            fps, q = min(fps, 30), min(q, 60)
        return fps, q, s

    def _refresh_capture_fps(self):
        targets = [self._stream_params(s)[0] for s in self.sessions.values()]
        self.capture.max_fps = max(targets, default=30)

    async def _encoded(self, frame, quality, scale):
        """Кодирование с общим кэшем: один и тот же кадр с одинаковыми
        параметрами кодируется один раз на все сессии."""
        key = (frame[4], int(quality), round(scale, 2))
        fut = self._enc_jobs.get(key)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = asyncio.ensure_future(
                loop.run_in_executor(_executor, encode_jpeg, frame, quality, scale))
            # держим только задания текущего кадра
            self._enc_jobs = {k: v for k, v in self._enc_jobs.items() if k[0] >= frame[4]}
            self._enc_jobs[key] = fut
        return await asyncio.shield(fut)

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
            "input_ok": INPUT_OK,
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
                                  "permissions": self._permission_payload(u)})

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
        load = await self._get_load()
        rep = capability.build_report(self.static_info, self.bench, load, self.config)
        cap = capability.capacity_plan(self.static_info, self.bench, load, self.config)
        return web.json_response({"static": self.static_info, "bench": self.bench,
                                  "load": load, "report": rep, "capacity": cap,
                                  "config": self.config})

    async def api_load(self, request):
        return web.json_response(await self._get_load())

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

    async def api_reset_create(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        d = await request.json()
        try:
            code = auth.create_reset_code(d["username"],
                                          ttl_hours=float(d.get("ttl_hours", 1)), actor=actor)
        except (ValueError, KeyError) as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"code": code})

    async def api_reset_redeem(self, request):
        """Публичный: пользователь вводит одноразовый код и новый пароль."""
        d = await request.json()
        try:
            username = auth.redeem_reset_code(str(d.get("code", "")).strip(),
                                              d.get("password", ""))
        except (ValueError, KeyError) as e:
            state.audit("reset_code.fail", "?", {"ip": request.remote})
            await asyncio.sleep(1.0)   # антиперебор
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True, "username": username})

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
                                  allow_input=d.get("allow_input", True),
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
                    "load": await self._get_load(),
                    "sessions": [s.info() for s in self.sessions.values()],
                    "config": self.config,
                    "capture_ok": CAPTURE_OK, "input_ok": INPUT_OK,
                })
                try:
                    await asyncio.wait_for(ws.receive(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            pass
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
        scr = [self.capture.frame[1], self.capture.frame[2]] if self.capture.frame else None
        await ws.send_json({"type": "hello", "sid": sess.sid,
                            "host_name": self.host_name(),
                            "route": sess.route,
                            "codec": "MJPEG (MVP; H.264/HEVC в дорожной карте)",
                            "screen": scr,
                            "profile": sess.profile,
                            "fps": sess.fps, "quality": sess.quality, "scale": sess.scale,
                            "permissions": self._permission_payload(user),
                            "isolation_note": "MVP: все удалённые сессии видят общий рабочий стол хоста. "
                                              "Изоляция через VM — этап 2."})
        self._refresh_capture_fps()
        sender = asyncio.create_task(self._frame_sender(sess))
        stats = asyncio.create_task(self._stats_sender(sess))
        cursor = asyncio.create_task(self._cursor_sender(sess))
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
                    sess.fps = min(int(d.get("fps", sess.fps)), self._fps_cap(user))
                    sess.quality = max(20, min(95, int(d.get("quality", sess.quality))))
                    sess.scale = max(0.25, min(1.0, float(d.get("scale", sess.scale))))
                    if d.get("profile") in capability.STREAM_PROFILES:
                        sess.profile = d["profile"]
                    sess.net_degrade = 0   # пользователь сменил настройки — начинаем заново
                    self._refresh_capture_fps()
                elif t == "input":
                    current_user = auth.get_user(username) or user
                    if self._input_permission(current_user)[0]:
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
            cursor.cancel()
            self.sessions.pop(sess.sid, None)
            self._refresh_capture_fps()
            state.log_session_event("disconnect", {
                "sid": sess.sid, "username": username, "ip": sess.ip,
                "frames": sess.frames, "mb_sent": round(sess.bytes / 2**20, 1),
                "duration_s": int(time.time() - sess.connected_at),
                "reason": sess.kick_reason or "client"})
        return ws

    async def _frame_sender(self, sess):
        """Отправка кадров: пропускает неизменённые, держит целевой FPS и
        адаптируется к каналу (если сеть не успевает — ступени деградации:
        битрейт → FPS → разрешение, с восстановлением)."""
        last_seq = -1
        last_params = None
        last_pick = 0.0            # monotonic-время выбора последнего кадра
        send_ewma = 0.0
        last_adapt = time.monotonic()
        while not sess.ws.closed:
            ev = self._frame_evt
            fps, quality, scale = self._stream_params(sess)
            budget = 1.0 / max(fps, 1)
            frame = self.capture.frame
            now = time.monotonic()
            params = (int(quality), round(scale, 2))
            # изменение = новый кадр экрана / смена настроек; курсор в кадр не
            # впечатывается (его рисует клиент), поэтому темп задают только
            # реальные изменения экрана и выбранный FPS. Раз в 2 с — keepalive.
            changed = frame is not None and (
                frame[4] != last_seq or params != last_params or now - last_pick >= 2.0)
            if not changed or now < last_pick + budget * 0.75:
                # Ждём СОБЫТИЕ нового кадра, а не таймер: системный таймер
                # Windows тикает ~15.6 мс и таймерный пейсинг режет FPS вдвое;
                # call_soon_threadsafe будит цикл мгновенно.
                if ev is None:
                    await asyncio.sleep(0.05)
                else:
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=0.05)
                    except asyncio.TimeoutError:
                        pass
                continue
            last_pick = now
            try:
                data, w, h, ts = await self._encoded(frame, quality, scale)
            except Exception:
                await asyncio.sleep(0.1)
                continue
            last_seq, last_params = frame[4], params
            t0 = time.perf_counter()
            try:
                await sess.ws.send_bytes(data)
            except (ConnectionError, RuntimeError):
                break
            send_dt = time.perf_counter() - t0
            send_ewma = send_dt if not send_ewma else send_ewma * 0.7 + send_dt * 0.3
            now2 = time.monotonic()
            if send_ewma > budget * 1.4 and sess.net_degrade < 3 and now2 - last_adapt > 2.0:
                sess.net_degrade += 1
                last_adapt = now2
            elif send_ewma < budget * 0.4 and sess.net_degrade > 0 and now2 - last_adapt > 6.0:
                sess.net_degrade -= 1
                last_adapt = now2
            sess.frames += 1
            sess.bytes += len(data)
            nowt = time.time()
            if nowt - sess.last_bytes_ts >= 1.0:
                sess.bitrate_mbps = sess.bytes * 8 / 1e6 / (nowt - sess.last_bytes_ts)
                sess.bytes = 0
                sess.last_bytes_ts = nowt

    async def _stats_sender(self, sess):
        while not sess.ws.closed:
            load = await self._get_load()
            fps, quality, scale = self._stream_params(sess)
            user = auth.get_user(sess.username) or sess.user
            warn = list(sess.warnings)
            if sess.degrade > 0:
                warn.append(f"Хост перегружен: применена ступень деградации {sess.degrade} "
                            f"(битрейт → FPS → разрешение)")
            if sess.net_degrade > 0:
                warn.append(f"Канал не успевает за потоком (ступень {sess.net_degrade}): "
                            f"качество временно снижено. Помогут меньший FPS/масштаб "
                            f"или кабельное подключение.")
            try:
                await sess.ws.send_json({
                    "type": "stats", "host": load,
                    "session": sess.info(),
                    "codec": "MJPEG", "capture": self.capture.backend,
                    "route": sess.route,
                    "fps_target": fps, "quality": quality, "scale": scale,
                    "permissions": self._permission_payload(user),
                    "warnings": warn,
                })
            except (ConnectionError, RuntimeError):
                break
            await asyncio.sleep(1.0)

    async def _cursor_sender(self, sess):
        """Отдельный лёгкий канал позиции и типа курсора (~30 Гц, только при
        изменении). Крошечный JSON — клиент рисует курсор нужной формы сам."""
        last = None
        while not sess.ws.closed:
            info = self._cursor_info()
            if info != last:
                last = info
                try:
                    await sess.ws.send_json({"type": "cursor",
                                             "cursor": info or {"visible": False}})
                except (ConnectionError, RuntimeError):
                    break
            await asyncio.sleep(1 / 30)

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
            self._refresh_capture_fps()
            load = await self._get_load()
            reserve = self.config.get("owner_reserve_percent", 25)
            if self.config.get("owner_gaming_mode"):
                reserve = max(reserve, 50)
            threshold = 100 - reserve
            ordered = sorted(self.sessions.values(),
                             key=lambda s: PRIORITY_ORDER.get(s.priority, 1))
            if load.get("cpu_percent", 0) > threshold or load.get("ram_percent", 0) > 92:
                for s in ordered:  # деградируем самых низкоприоритетных первыми
                    if s.degrade < 4:
                        s.degrade += 1
                        if s.degrade == 4:
                            s.warnings = ["Критическая перегрузка: сессия будет "
                                          "приостановлена при сохранении нагрузки"]
                        break
            elif load.get("cpu_percent", 0) < threshold - 15:
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
    if hwinfo.IS_WIN:
        # Таймер Windows по умолчанию тикает ~15.6 мс — asyncio.sleep(мелкие
        # паузы пейсинга) округляется вверх и режет FPS вдвое. 1 мс — как у
        # всех стриминговых/игровых приложений.
        try:
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
    server = HostServer(config)
    web.run_app(server.app, host="0.0.0.0", port=config["host_port"], print=None)
