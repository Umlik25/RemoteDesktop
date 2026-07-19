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
import collections
import concurrent.futures
import hashlib
import io
import ipaddress
import json
import math
import os
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import psutil
from aiohttp import web, WSMsgType

from . import auth, capability, display, hwinfo, state, video_encoder, virtual_display

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

# numpy — для дельта-кадров (поиск изменённой области экрана).
try:
    import numpy as np
    NP_OK = True
except Exception:
    NP_OK = False

# Быстрое кодирование: OpenCV (libjpeg-turbo, SIMD) кодирует JPEG в 3–5 раз
# быстрее Pillow — на 1440p это разница между ~20 и 60+ FPS. Fallback — Pillow.
try:
    import cv2
    CV2_OK = NP_OK
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
        _MEF = {"move": 0x0001, "abs": 0x8000, "virtualdesk": 0x4000,
                "ldown": 0x0002, "lup": 0x0004,
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
LOGIN_ATTEMPT_LIMIT = 8
LOGIN_ATTEMPT_WINDOW = 60
MAX_UPLOAD_MB = 256
SMOOTH_DESKTOP_MIN_FPS = 50
# MJPEG кодируется процессором. Около 2.5 Мп оставляют достаточно времени для
# DXGI-захвата и JPEG при 60 FPS; 4K в этом протоколе стабильно не помещается в
# 16.7-мс дедлайн даже в быстрой локальной сети.
SMOOTH_DESKTOP_MAX_PIXELS = 2_500_000

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


def _recent_rate(timestamps, window=1.0):
    """Число событий в секунду за короткое скользящее окно."""
    now = time.monotonic()
    cutoff = now - window
    try:
        recent = sum(ts >= cutoff for ts in list(timestamps))
    except RuntimeError:
        return 0.0
    return recent / window


# ---------------------------------------------------------------- захват экрана

def _bbox_pad(x0, y0, x1, y1, w, h):
    """Расширить область на 8 px и выровнять по чётным координатам (JPEG 4:2:0)."""
    x0 = max(0, x0 - 8) // 2 * 2
    y0 = max(0, y0 - 8) // 2 * 2
    x1 = min(w, ((x1 + 8 + 1) // 2) * 2)
    y1 = min(h, ((y1 + 8 + 1) // 2) * 2)
    return (int(x0), int(y0), int(x1), int(y1))


def _diff_bbox(a, b):
    """Прямоугольник изменений между кадрами (H, W, 4).

    False — кадры идентичны; None — считать изменённым целиком (ошибка/resize).
    Это ядро дельта-кадров: передаётся только изменившаяся область экрана."""
    try:
        if a.shape != b.shape:
            return None
        h, w = b.shape[:2]
        # На видео/играх почти весь экран меняется. Быстрая редкая сетка
        # позволяет сразу выбрать full-frame, не выполняя два полных прохода
        # по многомегабайтному BGRA-кадру.
        sample = (a[::32, ::32] != b[::32, ::32]).any(axis=2)
        if sample.size and sample.mean() > 0.35:
            return None
        if CV2_OK:
            # NumPy создавал несколько временных массивов размером с кадр.
            # На 4K это 30+ МБ на каждый проход и до 20 мс только на bbox.
            # OpenCV выполняет точное сравнение и поиск границ внутри C/SIMD.
            diff = cv2.absdiff(a, b)
            # boundingRect принимает одноканальную матрицу. Представляем BGRA
            # как строку байтов и переводим найденные X обратно в пиксели. Это
            # убирает ещё одну 4K-маску и два полных прохода inRange/invert.
            byte_x, y0, byte_w, bh = cv2.boundingRect(diff.reshape(h, w * 4))
            x0 = byte_x // 4
            x1 = (byte_x + byte_w + 3) // 4
            bw = x1 - x0
            if bw <= 0 or bh <= 0:
                return False
            return _bbox_pad(x0, y0, x0 + bw, y0 + bh, w, h)
        rows = (a.reshape(h, -1) != b.reshape(h, -1)).any(axis=1)
        idx = np.flatnonzero(rows)
        if idx.size == 0:
            return False
        y0, y1 = int(idx[0]), int(idx[-1]) + 1
        cols = (a[y0:y1] != b[y0:y1]).any(axis=(0, 2))
        ci = np.flatnonzero(cols)
        x0, x1 = int(ci[0]), int(ci[-1]) + 1
        return _bbox_pad(x0, y0, x1, y1, w, h)
    except Exception:
        return None


class ScreenCapture:
    """Отдельный поток захвата: хранит последний сырой кадр + область изменений.

    seq растёт только когда содержимое экрана реально изменилось; для каждого
    кадра запоминается bbox изменений (кольцо bbox_ring) — отправители шлют
    клиентам только изменённые области (дельта-кадры)."""

    def __init__(self):
        self.frame = None          # (bytes BGRA, width, height, ts, seq)
        self.running = False
        self.max_fps = 30
        self.mon_offset = (0, 0)   # left/top захватываемого монитора
        self.monitor_rect = None    # {x,y,width,height} выбранного Windows output
        self.output_id = None
        self.device_idx = 0
        self.output_idx = 0
        self.backend = None        # "dxgi" | "gdi"
        self.on_frame = None       # колбэк «появился новый кадр» (из потока захвата)
        self.bbox_ring = collections.deque(maxlen=600)   # (seq, bbox|None)
        self.capture_times = collections.deque(maxlen=600)
        self.process_ms_ewma = 0.0
        self._thread = None
        self._restart = threading.Event()
        self._seq = 0

    def start(self):
        if not CAPTURE_OK or self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._restart.set()

    def restart(self):
        """Пересоздать DXGI/GDI после смены display mode."""
        self.bbox_ring.clear()
        self._restart.set()

    def select_output(self, output):
        """Point every capture backend at the same enumerated Windows output."""
        output = output or {}
        current = output.get("current") or {}
        new_state = (
            output.get("id"), int(output.get("device_index", 0) or 0),
            int(output.get("output_index", output.get("capture_index", 0)) or 0),
            int(current.get("x", 0) or 0), int(current.get("y", 0) or 0),
            int(current.get("width", 0) or 0), int(current.get("height", 0) or 0),
        )
        old_state = (
            self.output_id, self.device_idx, self.output_idx,
            *((self.monitor_rect or {}).get(key, 0)
              for key in ("x", "y", "width", "height")),
        )
        self.output_id, self.device_idx, self.output_idx = new_state[:3]
        self.monitor_rect = dict(zip(("x", "y", "width", "height"), new_state[3:]))
        self.mon_offset = (new_state[3], new_state[4])
        if new_state != old_state:
            self.frame = None
            self.restart()
        return new_state != old_state

    def bbox_since(self, since_seq, upto_seq):
        """Объединённая область изменений за (since_seq, upto_seq].
        None — данных не хватает (или был полный кадр) → слать полный кадр."""
        if upto_seq <= since_seq:
            return None
        try:
            ring = list(self.bbox_ring)   # снапшот: поток захвата дописывает
        except RuntimeError:
            return None
        boxes = {}
        for s, b in reversed(ring):
            if s <= since_seq:
                break
            boxes[s] = b
        x0 = y0 = 1 << 30
        x1 = y1 = -1
        for s in range(since_seq + 1, upto_seq + 1):
            b = boxes.get(s, "missing")
            if b == "missing" or b is None:
                return None
            x0 = min(x0, b[0]); y0 = min(y0, b[1])
            x1 = max(x1, b[2]); y1 = max(y1, b[3])
        return (x0, y0, x1, y1) if x1 > x0 else None

    def _publish(self, arr, w, h, bbox):
        self._seq += 1
        self.bbox_ring.append((self._seq, bbox))
        # DXcam уже возвращает отдельный numpy-кадр. Не превращаем каждый
        # 1440p BGRA frame в ещё одну копию на 14+ МБ перед кодированием.
        self.frame = (arr, w, h, time.time(), self._seq)
        if self.on_frame:
            self.on_frame()

    def performance(self):
        return {
            "fps": round(_recent_rate(self.capture_times), 1),
            "process_ms": round(self.process_ms_ewma, 2),
        }

    def _record_capture(self, process_started):
        self.capture_times.append(time.monotonic())
        sample = (time.perf_counter() - process_started) * 1000
        self.process_ms_ewma = (sample if not self.process_ms_ewma
                                else self.process_ms_ewma * 0.8 + sample * 0.2)

    def _loop(self):
        while self.running:
            self._restart.clear()
            if DXCAM_OK and self._loop_dxcam():
                continue
            if self.running:
                self._loop_mss()

    def _loop_dxcam(self):
        """DXGI Desktop Duplication: быстрый захват, grab() сам возвращает None,
        если экран не менялся. Возврат False -> откат на mss."""
        try:
            cam = dxcam.create(device_idx=self.device_idx, output_idx=self.output_idx,
                               output_color="BGRA")
            if cam is None:
                return False
        except Exception:
            return False
        print(f"[App_Remote] Захват экрана: DXGI device {self.device_idx}, "
              f"output {self.output_idx} (быстрый)")
        self.backend = "dxgi"
        rect = self.monitor_rect or {}
        self.mon_offset = (int(rect.get("x", 0)), int(rect.get("y", 0)))
        errors = 0
        prev = None
        try:
            target = max(1, min(240, int(self.max_fps)))
            # Внутренний поток DXcam использует high-resolution pacing и
            # кольцевой буфер. grab() при активном start() читает только свежий
            # кадр из этого буфера, не заставляя отправителя догонять историю.
            try:
                cam.start(target_fps=target, video_mode=False)
            except Exception:
                return False
            while self.running and not self._restart.is_set():
                requested = max(1, min(240, int(self.max_fps)))
                if requested != target:
                    try:
                        cam.stop()
                        cam.start(target_fps=requested, video_mode=False)
                    except Exception:
                        return False
                    target = requested
                    prev = None
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
                    process_started = time.perf_counter()
                    bbox = _diff_bbox(prev, f) if (prev is not None and NP_OK) else None
                    prev = f
                    self._record_capture(process_started)
                    if bbox is not False:    # False = кадры идентичны, пропускаем
                        h, w = f.shape[:2]
                        self._publish(f, w, h, bbox)
                # Потребитель только опрашивает latest-frame; точный FPS задаёт
                # поток DXcam. 1–2 мс здесь ограничивают CPU без длинной очереди.
                time.sleep(min(0.002, 0.25 / target))
        finally:
            try:
                cam.stop()
            except Exception:
                pass
            try:
                cam.release()
            except Exception:
                pass
        return True

    def _loop_mss(self):
        prev = None
        self.backend = "gdi"
        print("[App_Remote] Захват экрана: GDI/mss (запасной путь)")
        with mss.mss() as sct:
            monitors = list(sct.monitors[1:])
            rect = self.monitor_rect or {}
            mon = next((item for item in monitors
                        if int(item.get("left", 0)) == int(rect.get("x", 0))
                        and int(item.get("top", 0)) == int(rect.get("y", 0))
                        and int(item.get("width", 0)) == int(rect.get("width", 0))
                        and int(item.get("height", 0)) == int(rect.get("height", 0))), None)
            if mon is None and monitors:
                mon = monitors[min(max(0, self.output_idx), len(monitors) - 1)]
            if mon is None:
                return
            self.mon_offset = (mon.get("left", 0), mon.get("top", 0))
            next_tick = time.perf_counter()
            while self.running and not self._restart.is_set():
                t0 = time.perf_counter()
                try:
                    process_started = time.perf_counter()
                    img = sct.grab(mon)
                    raw = img.bgra
                    if prev is None or raw != prev:   # memcmp: быстрый, с ранним выходом
                        bbox = None
                        if prev is not None and NP_OK:
                            a = np.frombuffer(prev, np.uint8).reshape(img.height, img.width, 4)
                            b = np.frombuffer(raw, np.uint8).reshape(img.height, img.width, 4)
                            bbox = _diff_bbox(a, b)
                        prev = raw
                        if bbox is not False:
                            self._publish(raw, img.width, img.height, bbox)
                    self._record_capture(process_started)
                except Exception:
                    time.sleep(0.5)
                    continue
                period = 1.0 / max(self.max_fps, 1)
                next_tick += period
                now = time.perf_counter()
                if next_tick < now - period:
                    next_tick = now
                time.sleep(max(0.0, next_tick - now))


def encode_jpeg(frame, quality, scale, region=None):
    """JPEG кадра или его области.

    region=(x0,y0,x1,y1) в координатах кадра — кодируется только эта область
    (дельта-кадр). Возвращает (data, sx, sy, fw, fh): смещение области и полные
    размеры потока в МАСШТАБИРОВАННЫХ координатах."""
    raw, w, h, ts, seq = frame
    if scale < 0.999:
        fw = max(2, int(w * scale)) // 2 * 2
        fh = max(2, int(h * scale)) // 2 * 2
    else:
        fw, fh = w, h
    if region is None:
        x0, y0, x1, y1 = 0, 0, w, h
        sx = sy = 0
        sw, sh = fw, fh
    else:
        x0, y0, x1, y1 = region
        sx = int(x0 * fw / w) // 2 * 2
        sy = int(y0 * fh / h) // 2 * 2
        ex = min(fw, ((-(-(x1 * fw) // w)) + 1) // 2 * 2)   # ceil, чётное
        ey = min(fh, ((-(-(y1 * fh) // h)) + 1) // 2 * 2)
        sw, sh = max(2, int(ex - sx)), max(2, int(ey - sy))
    if CV2_OK:
        try:
            source = raw if isinstance(raw, np.ndarray) else np.frombuffer(raw, np.uint8).reshape(h, w, 4)
            arr = source[y0:y1, x0:x1]
            if scale < 0.999:
                arr = cv2.resize(arr, (sw, sh), interpolation=cv2.INTER_AREA)
            # cvtColor умеет читать ROI со stride. Предварительный
            # ascontiguousarray копировал каждую дельта-область второй раз.
            bgr = cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)
            ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
            if ok:
                return buf.tobytes(), sx, sy, fw, fh
        except Exception:
            pass
    raw_bytes = raw.tobytes() if hasattr(raw, "tobytes") else raw
    img = Image.frombytes("RGB", (w, h), raw_bytes, "raw", "BGRX")
    if region is not None:
        img = img.crop((x0, y0, x1, y1))
    if scale < 0.999:
        img = img.resize((sw, sh), _BILINEAR)
    buf = io.BytesIO()
    # subsampling=2 (4:2:0) — заметно меньше и быстрее при том же видимом качестве
    img.save(buf, "JPEG", quality=int(quality), subsampling=2)
    return buf.getvalue(), sx, sy, fw, fh


# ---------------------------------------------------------------- ввод

KEY_MAP = {}
BTN_MAP = {}
_VK_PUNCT = {}
if INPUT_OK:
    for _name, _attr in (
            ("Enter", "enter"), ("Backspace", "backspace"), ("Tab", "tab"),
            ("Escape", "esc"), ("Delete", "delete"), ("Insert", "insert"),
            ("Home", "home"), ("End", "end"), ("PageUp", "page_up"),
            ("PageDown", "page_down"), ("ArrowUp", "up"), ("ArrowDown", "down"),
            ("ArrowLeft", "left"), ("ArrowRight", "right"), ("Shift", "shift"),
            ("Control", "ctrl"), ("Alt", "alt"), ("Meta", "cmd"),
            ("CapsLock", "caps_lock"), (" ", "space"),
            ("ContextMenu", "menu"), ("AltGraph", "alt_gr"),
                         ("NumLock", "num_lock"), ("ScrollLock", "scroll_lock"),
                         ("Pause", "pause"), ("PrintScreen", "print_screen")):
        _k = getattr(Key, _attr, None)
        if _k is not None:
            KEY_MAP[_name] = _k
    for _i in range(1, 13):
        _k = getattr(Key, f"f{_i}", None)
        if _k is not None:
            KEY_MAP[f"F{_i}"] = _k
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
        self.screen_origin = (0, 0)

    def set_screen(self, width, height, origin=(0, 0)):
        self.screen_wh = (max(1, int(width)), max(1, int(height)))
        self.screen_origin = (int(origin[0]), int(origin[1]))

    def _absolute_target(self, nx, ny):
        width, height = self.screen_wh or (1, 1)
        origin = getattr(self, "screen_origin", (0, 0))
        return (origin[0] + int(nx * max(0, width - 1)),
                origin[1] + int(ny * max(0, height - 1)))

    def _send_absolute(self, x, y):
        if not _SENDINPUT_OK:
            return False
        if _user32 is not None:
            try:
                left = int(_user32.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
                top = int(_user32.GetSystemMetrics(77))    # SM_YVIRTUALSCREEN
                width = max(1, int(_user32.GetSystemMetrics(78)))
                height = max(1, int(_user32.GetSystemMetrics(79)))
                dx = round((int(x) - left) * 65535 / max(1, width - 1))
                dy = round((int(y) - top) * 65535 / max(1, height - 1))
                flags = _MEF["move"] | _MEF["abs"] | _MEF["virtualdesk"]
                return bool(_send_mouse(flags, max(0, min(65535, dx)),
                                        max(0, min(65535, dy))))
            except Exception:
                pass
        return False

    def _mouse_move(self, nx, ny):
        """Map normalized coordinates into the selected monitor's desktop rect."""
        x, y = self._absolute_target(nx, ny)
        if self._send_absolute(x, y):
            return
        if self.screen_wh and self.mouse:
            self.mouse.position = (x, y)

    def _mouse_move_relative(self, dx, dy):
        """Перемещение 1:1 для игр, без повторного ускорения Windows."""
        dx = max(-4096, min(4096, int(dx)))
        dy = max(-4096, min(4096, int(dy)))
        if not dx and not dy:
            return
        # Относительный MOUSEEVENTF_MOVE повторно проходит через системную
        # акселерацию Windows. Абсолютная цель current + delta сохраняет
        # полученную от Pointer Lock игровую дельту ровно 1:1.
        if _SENDINPUT_OK and self.screen_wh and self.mouse:
            try:
                x, y = self.mouse.position
                w, h = self.screen_wh
                ox, oy = getattr(self, "screen_origin", (0, 0))
                target_x = max(ox, min(ox + w - 1, int(x) + dx))
                target_y = max(oy, min(oy + h - 1, int(y) + dy))
                if self._send_absolute(target_x, target_y):
                    return
            except Exception:
                pass
        if self.mouse:
            x, y = self.mouse.position
            target_x, target_y = int(x) + dx, int(y) + dy
            if self.screen_wh:
                width, height = self.screen_wh
                ox, oy = getattr(self, "screen_origin", (0, 0))
                target_x = max(ox, min(ox + width - 1, target_x))
                target_y = max(oy, min(oy + height - 1, target_y))
            self.mouse.position = (target_x, target_y)

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
            elif t == "mr":
                self._mouse_move_relative(ev.get("dx", 0), ev.get("dy", 0))
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
        self.adaptive = True       # сеть/кодировщик могут временно снижать поток
        self.degrade = 0           # 0..4 — ступень деградации по нагрузке хоста
        self.net_degrade = 0       # 0..3 — деградация из-за пропускной способности сети
        self.pipeline_degrade = 0  # 0..3 — захват/JPEG не укладываются в FPS
        self.force_full = True     # следующий кадр — полный (первый / по запросу клиента)
        self.priority = user.get("priority", "normal")
        self.frames = 0
        self.bytes = 0
        self.total_bytes = 0
        self.last_bytes_ts = time.time()
        self.bitrate_mbps = 0.0
        self.warnings = []
        self.kick_reason = None
        self.desktop_mode = "host"
        self.desktop_size = None
        self.desktop_target = None
        self.codec_requested = "auto"
        self.codec = "mjpeg"
        self.codec_generation = 0
        self.codec_reason = None
        self.client_h264_mse = False
        self.last_video_resync = 0.0
        self.send_id = 0
        self.last_ack_id = 0
        self.ack_latency_ms = 0.0
        self.client_queue = 0
        self.client_decode_ms = 0.0
        self.stale_drops = 0
        self.frame_bytes_ewma = 0.0
        self.required_mbps = 0.0
        self.link_mbps = 0
        self.sent_times = collections.deque(maxlen=240)
        self.frame_times = collections.deque(maxlen=480)
        self.encode_ms_ewma = 0.0
        self.send_ms_ewma = 0.0

    def effective(self):
        """Параметры с учётом нагрузки хоста, сети и кодировщика:
        битрейт → FPS → разрешение."""
        q, f, s = self.quality, self.fps, self.scale
        if self.degrade >= 1:
            q = max(30, q - 20)
        if self.degrade >= 2:
            f = max(10, f // 2)
        if self.degrade >= 3:
            s = min(s, 0.5)
        # При узком LAN сначала уменьшаем кадр, сохраняя плавность.
        auto_degrade = max(self.net_degrade, self.pipeline_degrade)
        if self.adaptive and auto_degrade == 1:
            q, s = max(35, q - 10), min(s, 0.75)
        elif self.adaptive and auto_degrade == 2:
            q, f, s = max(30, q - 15), max(15, int(f * 0.75)), min(s, 0.75)
        elif self.adaptive and auto_degrade >= 3:
            q, f, s = max(30, q - 20), max(10, f // 2), min(s, 0.5)
        return f, q, s

    def info(self, effective=None):
        f, q, s = effective or self.effective()
        return {
            "sid": self.sid, "username": self.username, "ip": self.ip,
            "route": self.route, "profile": self.profile,
            "priority": self.priority,
            "connected_at": time.strftime("%H:%M:%S", time.localtime(self.connected_at)),
            "fps_target": f, "quality": q, "scale": s,
            "requested": {"fps": self.fps, "quality": self.quality,
                          "scale": self.scale, "adaptive": self.adaptive},
            "degrade": self.degrade, "net_degrade": self.net_degrade,
            "pipeline_degrade": self.pipeline_degrade,
            "codec": self.codec,
            "codec_requested": self.codec_requested,
            "bitrate_mbps": round(self.bitrate_mbps, 1),
            "frames": self.frames,
            "ack_latency_ms": round(self.ack_latency_ms, 1),
            "client_queue": self.client_queue,
            "stale_drops": self.stale_drops,
            "required_mbps": round(self.required_mbps, 1),
            "link_mbps": self.link_mbps,
            "actual_fps": round(_recent_rate(self.frame_times), 1),
            "encode_ms": round(self.encode_ms_ewma, 2),
            "send_ms": round(self.send_ms_ewma, 2),
            "decode_ms": round(self.client_decode_ms, 2),
        }


class HostServer:
    def __init__(self, config):
        self.config = config
        self.capture = ScreenCapture()
        self.injector = InputInjector()
        self.display = display.DisplayManager(config.get("display_output", "auto"))
        self._sync_capture_output()
        initial_display = getattr(self.display, "_original", None) or {}
        self._display_refresh_hz = int(initial_display.get("refresh") or 0)
        self._display_owner = None
        self.sessions = {}
        self.static_info = None
        self.bench = None
        self._enc_jobs = {}        # (seq, q, scale) -> future с кодированным кадром
        self._load_cache = (0.0, None)
        self._load_task = None
        self.video_encoder = video_encoder.unavailable()
        self._loop = None
        self._frame_evt = None     # событие «есть новый кадр» для отправителей
        self._login_attempts = collections.defaultdict(collections.deque)
        self.app = self._build_app()

    # ------------------------------------------------------------ HTTP app

    def _build_app(self):
        @web.middleware
        async def cors(request, handler):
            origin = request.headers.get("Origin")
            if origin and not self._origin_allowed(request, origin):
                raise web.HTTPForbidden(text="Недоверенный источник запроса")
            try:
                if request.method == "OPTIONS":
                    resp = web.Response()
                else:
                    resp = await handler(request)
            except web.HTTPException as error:
                if origin:
                    error.headers["Access-Control-Allow-Origin"] = origin
                    error.headers["Vary"] = "Origin"
                raise
            if origin:
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Vary"] = "Origin"
                resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
                resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            return resp

        app = web.Application(middlewares=[cors], client_max_size=(MAX_UPLOAD_MB + 2) * 2**20)
        r = app.router
        r.add_get("/", self.page_panel)
        r.add_static("/static/", state.WEB_DIR)
        r.add_get("/api/info", self.api_info)
        r.add_post("/api/login", self.api_login)
        r.add_get("/api/admin/me", self.api_admin_me)
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
        r.add_get("/api/files", self.api_files)
        r.add_post("/api/files/upload", self.api_file_upload)
        r.add_get("/api/files/download/{name}", self.api_file_download)
        r.add_post("/api/files/delete", self.api_file_delete)
        r.add_post("/api/users/delete", self.api_user_delete)
        r.add_get("/api/invites", self.api_invites)
        r.add_post("/api/invites/create", self.api_invite_create)
        r.add_post("/api/invites/revoke", self.api_invite_revoke)
        r.add_get("/api/sessions", self.api_sessions)
        r.add_post("/api/sessions/kick", self.api_kick)
        r.add_post("/api/settings", self.api_settings)
        r.add_post("/api/display/install", self.api_display_install)
        r.add_post("/api/app/role", self.api_app_role)
        r.add_get("/api/audit", self.api_audit)
        r.add_get("/api/history", self.api_history)
        r.add_get("/ws/panel", self.ws_panel)
        r.add_get("/ws/stream", self.ws_stream)
        app.on_startup.append(self._on_start)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_start(self, app):
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._frame_evt = asyncio.Event()
        self.static_info = await loop.run_in_executor(_executor, hwinfo.get_static_info)
        self.bench = await loop.run_in_executor(_executor, hwinfo.quick_benchmark)
        self.video_encoder = await loop.run_in_executor(_executor, video_encoder.detect)
        if CAPTURE_OK:
            self.capture.on_frame = self._frame_notify_threadsafe
            self.capture.start()
            for _ in range(50):
                if self.capture.frame:
                    break
                await asyncio.sleep(0.1)
            if self.capture.frame:
                self.injector.set_screen(
                    self.capture.frame[1], self.capture.frame[2], self.capture.mon_offset)
        asyncio.create_task(self._beacon_loop())
        asyncio.create_task(self._governor_loop())
        state.audit("host.start", "system", {"port": self.config["host_port"]})

    async def _on_cleanup(self, app):
        capture = getattr(self, "capture", None)
        if capture and hasattr(capture, "stop"):
            capture.stop()
        if getattr(self, "_display_owner", None):
            manager = getattr(self, "display", None)
            if manager and manager.available:
                try:
                    await asyncio.get_running_loop().run_in_executor(_executor, manager.restore)
                except Exception:
                    pass
            self._display_owner = None

    # ------------------------------------------------------------ утилиты

    def _is_local(self, request):
        peer = request.remote or ""
        return peer in ("127.0.0.1", "::1")

    def _origin_allowed(self, request, origin):
        """Разрешает same-origin панель и локальный клиент App_Remote."""
        try:
            if origin.rstrip("/") == f"{request.scheme}://{request.host}".rstrip("/"):
                return True
            parsed = urlsplit(origin)
            return (parsed.scheme in ("http", "https")
                    and parsed.hostname in ("127.0.0.1", "localhost", "::1")
                    and parsed.port == int(self.config.get("client_port", 8600)))
        except (TypeError, ValueError):
            return False

    def _token(self, request):
        return (request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                or request.query.get("token", ""))

    def _admin_identity(self, request):
        """Возвращает (имя, запись) только для owner/admin с валидным токеном."""
        token = self._token(request)
        username, user = auth.check_token(token)
        if user and user.get("role") in ("owner", "admin"):
            return username, user
        return None, None

    def _admin(self, request):
        """Административная панель всегда требует токен, включая localhost."""
        username, _ = self._admin_identity(request)
        return username

    def _login_retry_after(self, remote):
        now = time.monotonic()
        attempts = self._login_attempts[remote]
        while attempts and now - attempts[0] >= LOGIN_ATTEMPT_WINDOW:
            attempts.popleft()
        if len(attempts) < LOGIN_ATTEMPT_LIMIT:
            return 0
        return max(1, int(LOGIN_ATTEMPT_WINDOW - (now - attempts[0])))

    def _login_failed(self, remote):
        self._login_attempts[remote].append(time.monotonic())

    def _login_succeeded(self, remote):
        self._login_attempts.pop(remote, None)

    def _can_manage_user(self, request, target_name, *, new_role=None, destructive=False):
        actor, actor_user = self._admin_identity(request)
        if not actor:
            raise web.HTTPForbidden(text="Требуется вход владельца или администратора")
        target = auth.get_user(target_name)
        if not target:
            raise web.HTTPBadRequest(text="Нет такого пользователя")
        if actor_user.get("role") != "owner" and target.get("role") in ("owner", "admin"):
            raise web.HTTPForbidden(text="Администратор не может изменять владельца или другого администратора")
        if target.get("role") == "owner" and new_role and new_role != "owner":
            raise web.HTTPForbidden(text="Роль владельца нельзя изменить через панель")
        if new_role == "owner":
            raise web.HTTPForbidden(text="Назначение владельца через панель запрещено")
        if actor_user.get("role") != "owner" and new_role == "admin":
            raise web.HTTPForbidden(text="Только владелец может назначать администраторов")
        if destructive and target.get("role") == "owner":
            raise web.HTTPForbidden(text="Учётную запись владельца нельзя удалить")
        if destructive and actor == target_name:
            raise web.HTTPBadRequest(text="Нельзя удалить или заблокировать собственную учётную запись")
        return actor, actor_user, target

    def _file_identity(self, request):
        username, user = auth.check_token(self._token(request))
        if not auth.user_is_active(user):
            raise web.HTTPUnauthorized(text="Сеанс истёк. Войдите снова.")
        if not user.get("allow_files", False):
            raise web.HTTPForbidden(text="Передача файлов не разрешена владельцем хоста")
        return username, user

    def _user_file_dir(self, username, user=None):
        storage_id = (user or auth.get_user(username) or {}).get("storage_id", "")
        if len(storage_id) == 24 and all(ch in "0123456789abcdef" for ch in storage_id):
            user_key = storage_id
        else:
            user_key = hashlib.sha256(username.encode("utf-8")).hexdigest()[:24]
        root = Path(state.DATA_DIR) / "files" / user_key
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        return root

    def _safe_filename(self, value):
        name = str(value or "").strip()
        forbidden = '<>:"/\\|?*'
        if (not name or len(name) > 180 or name.startswith(".")
                or name.endswith((".", " ")) or any(ch in forbidden or ord(ch) < 32 for ch in name)):
            raise web.HTTPBadRequest(text="Недопустимое имя файла")
        return name

    def _file_usage(self, root):
        total = 0
        for path in root.iterdir():
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

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
        display_allowed = bool(user and (user.get("role") in ("owner", "admin")
                                        or user.get("allow_display", False)))
        return {
            "input": input_allowed,
            "input_reason": input_reason,
            "work_only_mode": self.config.get("work_only_mode", False),
            "clipboard": bool(user and user.get("allow_clipboard", False)),
            "files": bool(user and user.get("allow_files", False)),
            "display": bool(display_allowed and getattr(self, "display", None)
                            and self.display.available),
        }

    def _network_state(self, sess=None):
        link = capability.lan_link_mbps(self.static_info or {})
        # На 100-Мбит Ethernet реальный полезный MJPEG/WebSocket-поток ниже
        # номинала. Запас не даёт TCP-очереди превратиться в видимую задержку.
        budget = link * (0.55 if link and link <= 100 else 0.7)
        if sess is not None:
            sess.link_mbps = link
        return {"link_mbps": link, "safe_stream_mbps": round(budget, 1),
                "limited": bool(link and link <= 100)}

    @staticmethod
    def _network_pressure_level(required_mbps, budget_mbps):
        """Ступень, нужная для текущего превышения доступного битрейта."""
        if not budget_mbps or required_mbps <= budget_mbps * 1.05:
            return 0
        ratio = required_mbps / budget_mbps
        if ratio >= 2.2:
            return 3
        if ratio >= 1.45:
            return 2
        return 1

    @staticmethod
    def _network_lag_pressure(lag_bad, send_seconds, frame_budget_seconds,
                              required_mbps, budget_mbps):
        """Не принимать задержку браузера за сетевой затор без подтверждения."""
        send_busy = send_seconds > frame_budget_seconds * 1.4
        lag_has_network_evidence = bool(
            lag_bad and (send_seconds > frame_budget_seconds * 0.6
                         or (budget_mbps and
                             required_mbps > budget_mbps * 0.7)))
        return send_busy or lag_has_network_evidence

    @staticmethod
    def _pipeline_pressure_level(encode_ms, frame_budget_ms):
        """Ступень, нужная когда JPEG не укладывается в дедлайн кадра."""
        if not frame_budget_ms or encode_ms <= frame_budget_ms * 0.72:
            return 0
        ratio = encode_ms / frame_budget_ms
        if ratio >= 1.6:
            return 3
        if ratio >= 1.0:
            return 2
        return 1

    def _frame_notify_threadsafe(self):
        """Из потока захвата: разбудить отправителей кадров в event loop."""
        frame = self.capture.frame
        if frame:
            self.injector.set_screen(frame[1], frame[2], self.capture.mon_offset)
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(self._notify_frame)
            except RuntimeError:
                pass

    def _sync_capture_output(self, output=None):
        manager = getattr(self, "display", None)
        if output is None and manager:
            try:
                output = manager.selected_output()
            except Exception:
                output = None
        capture = getattr(self, "capture", None)
        if capture and hasattr(capture, "select_output"):
            capture.select_output(output)
        current = (output or {}).get("current") or {}
        injector = getattr(self, "injector", None)
        if injector and current:
            injector.set_screen(
                current.get("width", 1), current.get("height", 1),
                (current.get("x", 0), current.get("y", 0)))
        return output

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

    def _stream_limits(self, user):
        return {
            "min_fps": 1, "max_fps": self._fps_cap(user),
            "min_quality": 20, "max_quality": 95,
            "min_scale": 0.25, "max_scale": 1.0,
        }

    def _display_state(self, sess=None, user=None):
        manager = getattr(self, "display", None)
        available = bool(manager and manager.available)
        allowed = bool(user and (user.get("role") in ("owner", "admin")
                                 or user.get("allow_display", False)))
        try:
            current = manager.current() if available else None
            modes = manager.modes() if available else []
            outputs = manager.outputs() if available and hasattr(manager, "outputs") else []
            selected_output = (manager.selected_output()
                               if available and hasattr(manager, "selected_output") else None)
            if current and current.get("refresh"):
                self._display_refresh_hz = int(current["refresh"])
        except Exception:
            available, current, modes, outputs, selected_output = False, None, [], [], None
        return {
            "available": available,
            "allowed": available and allowed,
            "mode": sess.desktop_mode if sess else "host",
            "current": current,
            "selected": sess.desktop_size if sess else None,
            "target": sess.desktop_target if sess else None,
            "modes": modes,
            "shared": True,
            "output": selected_output,
            "outputs": outputs,
            "independent_from_physical": bool(
                selected_output and selected_output.get("virtual")),
            "virtual_display": virtual_display.state_payload(outputs),
        }

    async def _apply_display_output_config(self, sess, data, user):
        requested = data.get("display_output")
        if not requested:
            return []
        manager = getattr(self, "display", None)
        allowed = bool(user and (user.get("role") in ("owner", "admin")
                                 or user.get("allow_display", False)))
        if not manager or not manager.available:
            return ["Выбор экрана доступен только на Windows-хосте"]
        if not allowed:
            return ["Владелец хоста не разрешил этой учётной записи менять дисплей"]
        try:
            before = manager.selected_output() if hasattr(manager, "selected_output") else None
            outputs = manager.outputs() if hasattr(manager, "outputs") else []
        except Exception:
            before, outputs = None, []
        lowered = str(requested).lower()
        if lowered == "virtual":
            target = next((item for item in outputs if item.get("virtual")), None)
        elif lowered == "physical":
            target = next((item for item in outputs
                           if item.get("primary") and not item.get("virtual")), None)
            target = target or next((item for item in outputs if not item.get("virtual")), None)
        elif lowered == "auto":
            target = next((item for item in outputs if item.get("virtual")), None)
            target = target or next((item for item in outputs if item.get("primary")), None)
        else:
            target = next((item for item in outputs if item.get("id") == requested), None)
        changing = bool(target and before and target.get("id") != before.get("id"))
        if changing and len(self.sessions) > 1:
            return ["Экран трансляции нельзя переключать при нескольких активных сессиях"]
        display_owner = getattr(self, "_display_owner", None)
        if changing and display_owner not in (None, sess.sid):
            return ["Разрешением текущего экрана управляет другая сессия"]
        if changing and display_owner == sess.sid:
            try:
                await asyncio.get_running_loop().run_in_executor(_executor, manager.restore)
            except Exception:
                pass
            self._display_owner = None
            sess.desktop_mode = "host"
            sess.desktop_size = None
            sess.desktop_target = None
        try:
            ok, selected, error = await asyncio.get_running_loop().run_in_executor(
                _executor, manager.select_output, requested)
        except Exception as exc:
            ok, selected, error = False, None, f"Ошибка Windows Display API: {exc}"
        if not ok or not selected:
            return [error or "Windows не смогла выбрать экран трансляции"]
        if not before or selected.get("id") != before.get("id"):
            self.config["display_output"] = str(requested)
            state.save_config(self.config)
            self._sync_capture_output(selected)
            current = selected.get("current") or {}
            self._display_refresh_hz = int(current.get("refresh") or 0)
            self._reset_stream_adaptation()
            for active in self.sessions.values():
                active.codec_generation += 1
                active.force_full = True
            state.audit("display.output", sess.username, {
                "id": selected.get("id"), "virtual": selected.get("virtual", False)})
        return []

    async def _apply_display_config(self, sess, data, user):
        reasons = await self._apply_display_output_config(sess, data, user)
        requested = data.get("desktop_mode")
        if requested not in ("host", "client", "viewport"):
            return self._display_state(sess, user), reasons
        manager = getattr(self, "display", None)
        allowed = bool(user and (user.get("role") in ("owner", "admin")
                                 or user.get("allow_display", False)))
        if not manager or not manager.available:
            reasons.append("Системное разрешение можно менять только на Windows-хосте")
            return self._display_state(sess, user), reasons
        if not allowed:
            reasons.append("Владелец хоста не разрешил этой учётной записи менять дисплей")
            return self._display_state(sess, user), reasons
        if requested == "host":
            if getattr(self, "_display_owner", None) == sess.sid:
                try:
                    restored = await asyncio.get_running_loop().run_in_executor(
                        _executor, manager.restore)
                except Exception:
                    restored = False
                if restored:
                    self._display_owner = None
                    sess.desktop_mode = "host"
                    sess.desktop_size = None
                    sess.desktop_target = None
                    self._sync_capture_output()
                    self.capture.restart()
                    original = getattr(manager, "_original", None) or {}
                    self._display_refresh_hz = int(original.get("refresh") or 0)
                    self._reset_stream_adaptation()
                else:
                    reasons.append("Не удалось восстановить исходное разрешение хоста")
            return self._display_state(sess, user), reasons
        display_owner = getattr(self, "_display_owner", None)
        if display_owner not in (None, sess.sid):
            reasons.append("Разрешением общего дисплея уже управляет другая сессия")
            return self._display_state(sess, user), reasons
        if display_owner is None and len(self.sessions) > 1:
            reasons.append("Системное разрешение нельзя менять при нескольких активных сессиях")
            return self._display_state(sess, user), reasons
        raw_width, raw_height = self._client_display_size(data, requested)
        if raw_width <= 0 or raw_height <= 0:
            reasons.append("Клиент не сообщил размер экрана или окна")
            return self._display_state(sess, user), reasons
        width = int(max(800, min(8192, raw_width)))
        height = int(max(600, min(8192, raw_height)))
        requested_fps = int(round(self._stream_number(data.get("fps"), sess.fps)))
        # NVENC получает D3D11-кадр напрямую и нормально работает с 4K.
        # Ограничение до ~2.5 Мп нужно только запасному CPU-MJPEG пути.
        hardware_video = self._h264_eligible(sess, data)
        pixel_budget = (SMOOTH_DESKTOP_MAX_PIXELS
                        if requested_fps >= SMOOTH_DESKTOP_MIN_FPS
                        and not hardware_video else None)
        try:
            candidate = display.best_mode(
                manager.modes(), width, height, max_pixels=pixel_budget)
        except Exception:
            candidate = None
        selected = sess.desktop_size or {}
        target = {"width": width, "height": height,
                  "source": "window" if requested == "viewport" else "screen",
                  "max_pixels": pixel_budget}
        if (display_owner == sess.sid and candidate
                and selected.get("width") == candidate.get("width")
                and selected.get("height") == candidate.get("height")):
            sess.desktop_mode = requested
            sess.desktop_target = target
            return self._display_state(sess, user), reasons
        try:
            ok, chosen, error = await asyncio.get_running_loop().run_in_executor(
                _executor, manager.set_best, width, height, pixel_budget)
        except Exception as exc:
            ok, chosen, error = False, None, f"Ошибка Windows Display API: {exc}"
        if not ok:
            reasons.append(error or "Windows не применила выбранное разрешение")
            return self._display_state(sess, user), reasons
        self._display_owner = sess.sid
        sess.desktop_mode = requested
        sess.desktop_size = chosen
        sess.desktop_target = target
        self._display_refresh_hz = int(chosen.get("refresh") or 0)
        self._sync_capture_output()
        self.capture.restart()
        self._reset_stream_adaptation()
        if (pixel_budget and width * height > pixel_budget
                and chosen["width"] * chosen["height"] <= pixel_budget):
            reasons.append(
                f"Для стабильных {requested_fps} FPS системное разрешение "
                f"ограничено до {chosen['width']}×{chosen['height']}; "
                "4K в текущем MJPEG-режиме перегружает захват и JPEG")
        if chosen["width"] != width or chosen["height"] != height:
            reasons.append(
                f"Применён ближайший поддерживаемый режим {chosen['width']}×{chosen['height']} "
                f"вместо {width}×{height}")
        return self._display_state(sess, user), reasons

    def _client_display_size(self, data, requested):
        """Размер физического экрана или текущего viewport с legacy-fallback."""
        key = "viewport" if requested == "viewport" else "screen"
        client_display = data.get("client_display")
        candidate = client_display.get(key) if isinstance(client_display, dict) else None
        if isinstance(candidate, dict):
            width = self._stream_number(candidate.get("width"), 0)
            height = self._stream_number(candidate.get("height"), 0)
            if width > 0 and height > 0:
                return width, height
        return (self._stream_number(data.get("client_width"), 0),
                self._stream_number(data.get("client_height"), 0))

    async def _release_display(self, sess):
        if getattr(self, "_display_owner", None) != sess.sid:
            return
        manager = getattr(self, "display", None)
        if manager and manager.available:
            try:
                await asyncio.get_running_loop().run_in_executor(_executor, manager.restore)
                self._sync_capture_output()
                self.capture.restart()
                original = getattr(manager, "_original", None) or {}
                self._display_refresh_hz = int(original.get("refresh") or 0)
                self._reset_stream_adaptation()
            except Exception:
                pass
        self._display_owner = None

    async def _fit_display_for_mjpeg(self, sess):
        """После сбоя NVENC не оставлять запасной JPEG-конвейер в 4K."""
        selected = sess.desktop_size or {}
        target = sess.desktop_target or {}
        if (getattr(self, "_display_owner", None) != sess.sid
                or selected.get("width", 0) * selected.get("height", 0)
                <= SMOOTH_DESKTOP_MAX_PIXELS):
            return None
        width, height = target.get("width"), target.get("height")
        if not width or not height:
            return None
        try:
            ok, chosen, _ = await asyncio.get_running_loop().run_in_executor(
                _executor, self.display.set_best, width, height,
                SMOOTH_DESKTOP_MAX_PIXELS)
        except Exception:
            return None
        if not ok or not chosen:
            return None
        sess.desktop_size = chosen
        sess.desktop_target = {**target, "max_pixels": SMOOTH_DESKTOP_MAX_PIXELS}
        self._display_refresh_hz = int(chosen.get("refresh") or 0)
        self._sync_capture_output()
        self.capture.restart()
        self._reset_stream_adaptation()
        return chosen

    @staticmethod
    def _stream_number(value, fallback):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        return number if math.isfinite(number) else fallback

    def _apply_frame_ack(self, sess, data):
        """Обратная связь браузера: очередь декодера и end-to-end lag кадра."""
        try:
            ack_id = max(0, int(data.get("id", 0)))
            queue = max(0, min(120, int(data.get("queue", 0))))
            decode_ms = max(0.0, min(10_000.0, float(data.get("decode_ms", 0))))
        except (TypeError, ValueError):
            return
        if ack_id < sess.last_ack_id:
            return
        sent_at = None
        while sess.sent_times and sess.sent_times[0][0] <= ack_id:
            frame_id, frame_sent_at = sess.sent_times.popleft()
            if frame_id == ack_id:
                sent_at = frame_sent_at
        if sent_at is not None:
            sample = max(0.0, (time.monotonic() - sent_at) * 1000)
            sess.ack_latency_ms = (sample if not sess.ack_latency_ms
                                   else sess.ack_latency_ms * 0.75 + sample * 0.25)
        sess.last_ack_id = ack_id
        sess.client_queue = queue
        sess.client_decode_ms = decode_ms

    @staticmethod
    def _apply_video_ack(sess, data):
        try:
            queue = max(0, min(120, int(data.get("queue", 0))))
            decode_ms = max(0.0, min(10_000.0, float(data.get("decode_ms", 0))))
            frame_count = max(1, min(12, int(data.get("frames", 1))))
        except (TypeError, ValueError, OverflowError):
            return
        now = time.monotonic()
        sess.client_queue = queue
        sess.client_decode_ms = decode_ms
        sess.frame_times.extend([now] * frame_count)
        sess.frames += frame_count

    def _stream_state(self, sess, user=None, reasons=None):
        """Выбранные и фактически применённые параметры для ответа клиенту."""
        user = user or sess.user
        fps, quality, scale = self._stream_params(sess)
        why = list(reasons or [])
        if self.config.get("work_only_mode") and user.get("role") not in ("owner", "admin"):
            if sess.fps > 30 or sess.quality > 60:
                why.append("Режим «только работа» ограничивает поток до 30 FPS и Q60")
        if sess.degrade > 0:
            why.append(f"Нагрузка хоста временно снизила поток (ступень {sess.degrade})")
        if sess.adaptive and sess.net_degrade > 0:
            why.append(f"Автоадаптация сети временно снизила поток (ступень {sess.net_degrade})")
        if sess.adaptive and sess.pipeline_degrade > 0:
            why.append(
                f"Захват или JPEG не укладываются в целевой FPS "
                f"(ступень {sess.pipeline_degrade})")
        refresh = int(getattr(self, "_display_refresh_hz", 0) or 0)
        if refresh >= 24 and sess.fps > refresh:
            why.append(
                f"Дисплей хоста работает на {refresh} Гц, поэтому поток "
                f"ограничен до {refresh} FPS")
        if sess.codec_reason:
            why.append(sess.codec_reason)
        # Не дублируем одинаковые причины между валидацией и текущим состоянием.
        why = list(dict.fromkeys(why))
        return {
            "selected": {
                "fps": sess.fps, "quality": sess.quality,
                "scale": sess.scale, "profile": sess.profile,
                "adaptive": sess.adaptive,
            },
            "applied": {"fps": fps, "quality": quality, "scale": scale},
            "codec": {"selected": sess.codec_requested,
                      "applied": sess.codec},
            "limits": self._stream_limits(user),
            "reasons": why,
        }

    def _public_video_capabilities(self):
        capabilities = dict(getattr(self, "video_encoder", {}) or {})
        capabilities.pop("executable", None)
        return capabilities

    def _h264_eligible(self, sess, data):
        requested = data.get("video_codec", sess.codec_requested)
        client = data.get("video_codecs")
        client_h264 = (bool(client.get("h264_mse"))
                       if isinstance(client, dict) else sess.client_h264_mse)
        scale = self._stream_number(data.get("scale"), sess.scale)
        capabilities = getattr(self, "video_encoder", {}) or {}
        return bool(requested != "mjpeg" and client_h264
                    and capabilities.get("available") and scale >= 0.999)

    def _select_stream_codec(self, sess):
        if sess.codec_requested == "mjpeg":
            return "mjpeg", None
        capabilities = getattr(self, "video_encoder", {}) or {}
        if not sess.client_h264_mse:
            reason = "Браузер не поддерживает H.264 Media Source Extensions"
        elif not capabilities.get("available"):
            reason = capabilities.get("reason") or "Аппаратный H.264 недоступен"
        elif sess.scale < 0.999:
            reason = "Уменьшенный масштаб использует MJPEG; H.264 GPU передаёт родное разрешение"
        else:
            return "h264", None
        return "mjpeg", reason if sess.codec_requested == "h264" else None

    def _apply_stream_config(self, sess, data, user):
        """Безопасно применяет настройки потока и возвращает подтверждение.

        Старые клиенты продолжают работать: отсутствующие поля не меняют
        состояние. Новые получают selected/applied и видят все ограничения.
        """
        limits = self._stream_limits(user)
        requested_fps = int(round(self._stream_number(data.get("fps"), sess.fps)))
        requested_quality = int(round(self._stream_number(
            data.get("quality"), sess.quality)))
        requested_scale = self._stream_number(data.get("scale"), sess.scale)
        fps = max(limits["min_fps"], min(limits["max_fps"], requested_fps))
        quality = max(limits["min_quality"],
                      min(limits["max_quality"], requested_quality))
        scale = max(limits["min_scale"], min(limits["max_scale"], requested_scale))
        reasons = []
        if requested_fps > limits["max_fps"]:
            reasons.append(f"Для этой учётной записи разрешено не больше {limits['max_fps']} FPS")
        elif requested_fps < limits["min_fps"]:
            reasons.append(f"Минимальное значение — {limits['min_fps']} FPS")
        if requested_quality != quality:
            reasons.append(f"Качество ограничено диапазоном Q{limits['min_quality']}–Q{limits['max_quality']}")
        if requested_scale != scale:
            reasons.append("Масштаб ограничен диапазоном 25–100%")

        old_effective = self._stream_params(sess)
        old_selected = (sess.fps, sess.quality, sess.scale, sess.adaptive,
                        sess.profile, sess.codec_requested, sess.codec)
        profile = data.get("profile")
        if profile in capability.STREAM_PROFILES:
            sess.profile = profile
        adaptive = data.get("adaptive", sess.adaptive)
        if isinstance(adaptive, bool):
            sess.adaptive = adaptive
        codec_requested = data.get("video_codec", sess.codec_requested)
        if codec_requested in ("auto", "h264", "mjpeg"):
            sess.codec_requested = codec_requested
        client_codecs = data.get("video_codecs")
        if isinstance(client_codecs, dict):
            sess.client_h264_mse = bool(client_codecs.get("h264_mse"))

        sess.fps, sess.quality, sess.scale = fps, quality, scale
        sess.codec, codec_reason = self._select_stream_codec(sess)
        sess.codec_reason = codec_reason
        sess.user = user
        sess.net_degrade = 0
        sess.pipeline_degrade = 0
        sess.frame_bytes_ewma = 0.0
        sess.required_mbps = 0.0
        sess.encode_ms_ewma = 0.0
        new_effective = self._stream_params(sess)
        image_params_changed = (int(old_effective[1]) != int(new_effective[1])
                                or abs(old_effective[2] - new_effective[2]) > 1e-6)
        changed = old_selected != (sess.fps, sess.quality, sess.scale,
                                   sess.adaptive, sess.profile,
                                   sess.codec_requested, sess.codec)
        if changed:
            sess.codec_generation += 1
        if image_params_changed:
            sess.force_full = True
        self._refresh_capture_fps()
        if changed or old_effective != new_effective:
            self._notify_frame()
        state_payload = self._stream_state(sess, user, reasons)
        return {"type": "config_applied", "request_id": data.get("request_id"),
                **state_payload}

    def _stream_params(self, sess):
        """Эффективные FPS/качество/масштаб с учётом режима «только работа»."""
        fps, q, s = sess.effective()
        refresh = int(getattr(self, "_display_refresh_hz", 0) or 0)
        if refresh >= 24:
            fps = min(fps, refresh)
        if self.config.get("work_only_mode") and sess.user.get("role") not in ("owner", "admin"):
            fps, q = min(fps, 30), min(q, 60)
        return fps, q, s

    def _reset_stream_adaptation(self):
        """Сбросить замеры, ставшие неверными после смены display mode."""
        for session in self.sessions.values():
            session.net_degrade = 0
            session.pipeline_degrade = 0
            session.frame_bytes_ewma = 0.0
            session.required_mbps = 0.0
            session.encode_ms_ewma = 0.0
            session.send_ms_ewma = 0.0
            session.force_full = True
            if session.codec == "h264":
                session.codec_generation += 1
        self._refresh_capture_fps()
        self._notify_frame()

    def _refresh_capture_fps(self):
        targets = [self._stream_params(s)[0] for s in self.sessions.values()
                   if s.codec != "h264"]
        self.capture.max_fps = max(targets, default=(5 if self.sessions else 30))

    async def _encoded(self, frame, quality, scale, region=None):
        """Кодирование с общим кэшем: один и тот же кадр/область с одинаковыми
        параметрами кодируется один раз на все сессии."""
        key = (frame[4], int(quality), round(scale, 6), region)
        fut = self._enc_jobs.get(key)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = asyncio.ensure_future(
                loop.run_in_executor(_executor, encode_jpeg, frame, quality, scale, region))
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

    @staticmethod
    def _tune_stream_socket(request):
        """Не копить мелкие control/input пакеты в TCP Nagle-буфере."""
        try:
            sock = request.transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # AF41 обычно попадает в WMM Video и меньше ждёт в Wi-Fi очереди.
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x88)
        except (AttributeError, OSError):
            pass

    def host_name(self):
        return self.config.get("host_name") or self.static_info.get("hostname", "Host")

    def node_identity(self):
        environment = (self.static_info or {}).get("environment") or {}
        detected = environment.get("kind") if environment.get("kind") in ("physical", "vm") else "physical"
        configured = self.config.get("node_type", "auto")
        if configured not in ("auto", "physical", "vm"):
            configured = "auto"
        resolved = detected if configured == "auto" else configured
        return {
            "type": resolved,
            "configured": configured,
            "detected": detected,
            "hypervisor": environment.get("hypervisor") if resolved == "vm" else None,
            "parent_host": (self.config.get("parent_host") or None)
                           if resolved == "vm" else None,
        }

    # ------------------------------------------------------------ страницы

    async def page_panel(self, request):
        return web.FileResponse(f"{state.WEB_DIR}/host.html")

    # ------------------------------------------------------------ API

    async def api_info(self, request):
        s = self.static_info or {}
        return web.json_response({
            "app": "App_Remote", "version": "0.1.0", "role": "host",
            "name": self.host_name(),
            "node": self.node_identity(),
            "os": s.get("os"), "cpu": s.get("cpu"),
            "cores": s.get("cores"), "threads": s.get("threads"),
            "ram_gb": s.get("ram_gb"),
            "gpus": s.get("gpus"),
            "encoders": s.get("encoders"),
            "video": self._public_video_capabilities(),
            "display": self._display_state(),
            "network": self._network_state(),
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
        try:
            auth.create_user(d["username"], d["password"], role="owner",
                             profile="dev", priority="critical", actor="setup")
        except (ValueError, KeyError) as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True})

    async def api_login(self, request):
        d = await request.json()
        remote = request.remote or "unknown"
        retry_after = self._login_retry_after(remote)
        if retry_after:
            raise web.HTTPTooManyRequests(
                text=f"Слишком много попыток. Повторите через {retry_after} сек.",
                headers={"Retry-After": str(retry_after)})
        username = str(d.get("username", "")).strip()
        u = auth.verify(username, d.get("password", ""))
        if not u:
            self._login_failed(remote)
            state.audit("login.fail", username or "?", {"ip": request.remote})
            raise web.HTTPUnauthorized(text="Неверные учётные данные, блокировка или истёкший доступ")
        self._login_succeeded(remote)
        token = auth.issue_token(username)
        state.audit("login.ok", username, {"ip": request.remote})
        return web.json_response({"token": token, "role": u["role"],
                                  "username": username,
                                  "profile": u.get("profile", "office"),
                                  "permissions": self._permission_payload(u)})

    async def api_admin_me(self, request):
        username, user = self._admin_identity(request)
        if not user:
            raise web.HTTPUnauthorized(text="Сеанс панели истёк. Войдите снова.")
        return web.json_response({"username": username, "role": user.get("role")})

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
                                  "config": self.config,
                                  "video": self._public_video_capabilities()})

    async def api_load(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        return web.json_response(await self._get_load())

    async def api_users(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        return web.json_response(auth.list_users())

    async def api_user_create(self, request):
        actor, actor_user = self._admin_identity(request)
        if not actor_user:
            raise web.HTTPForbidden()
        d = await request.json()
        role = d.get("role", "user")
        if role == "owner":
            raise web.HTTPForbidden(text="Назначение владельца через панель запрещено")
        if role == "admin" and actor_user.get("role") != "owner":
            raise web.HTTPForbidden(text="Только владелец может назначать администраторов")
        try:
            auth.create_user(d["username"], d["password"],
                             role=role, profile=d.get("profile", "office"),
                             priority=d.get("priority", "normal"),
                             allow_input=d.get("allow_input", True),
                             allow_clipboard=d.get("allow_clipboard", False),
                             allow_files=d.get("allow_files", False),
                             allow_display=d.get("allow_display", False),
                             max_fps=int(d.get("max_fps", 60)),
                             disk_quota_mb=int(d.get("disk_quota_mb", 2048)), actor=actor)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True})

    async def api_user_update(self, request):
        d = await request.json()
        username = str(d.get("username", "")).strip()
        fields = d.get("fields", {})
        destructive = bool(fields.get("blocked"))
        actor, _, _ = self._can_manage_user(
            request, username, new_role=fields.get("role"), destructive=destructive)
        try:
            auth.update_user(username, fields, actor=actor)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        current_user = auth.get_user(username)
        for session in list(self.sessions.values()):
            if session.username != username:
                continue
            session.user = current_user
            session.priority = current_user.get("priority", session.priority)
            if fields.get("blocked"):
                await self._kick(session, "Пользователь заблокирован")
        if fields.get("blocked"):
            auth.revoke_user_tokens(username)
        return web.json_response({"ok": True})

    async def api_user_password(self, request):
        d = await request.json()
        username = str(d.get("username", "")).strip()
        actor, _, _ = self._can_manage_user(request, username)
        try:
            auth.set_password(username, d.get("password", ""), actor=actor)
        except (ValueError, KeyError) as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"ok": True})

    async def api_reset_create(self, request):
        d = await request.json()
        username = str(d.get("username", "")).strip()
        actor, _, _ = self._can_manage_user(request, username)
        try:
            code = auth.create_reset_code(username,
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

    async def api_files(self, request):
        username, user = self._file_identity(request)
        root = self._user_file_dir(username, user)
        files = []
        for path in root.iterdir():
            try:
                if not path.is_file() or path.is_symlink() or path.name.startswith(".upload-"):
                    continue
                stat = path.stat()
                files.append({"name": path.name, "size": stat.st_size,
                              "modified": int(stat.st_mtime)})
            except OSError:
                continue
        files.sort(key=lambda item: item["modified"], reverse=True)
        used = sum(item["size"] for item in files)
        quota = int(user.get("disk_quota_mb", 2048)) * 2**20
        return web.json_response({"files": files, "used": used, "quota": quota,
                                  "max_upload": MAX_UPLOAD_MB * 2**20})

    async def api_file_upload(self, request):
        username, user = self._file_identity(request)
        try:
            reader = await request.multipart()
            field = await reader.next()
        except (AssertionError, ValueError):
            raise web.HTTPBadRequest(text="Ожидался файл")
        if field is None or field.name != "file" or not field.filename:
            raise web.HTTPBadRequest(text="Файл не выбран")

        name = self._safe_filename(field.filename)
        root = self._user_file_dir(username, user)
        destination = root / name
        if destination.is_symlink():
            raise web.HTTPBadRequest(text="Недопустимый путь файла")
        existing_size = destination.stat().st_size if destination.exists() else 0
        quota = int(user.get("disk_quota_mb", 2048)) * 2**20
        baseline = max(0, self._file_usage(root) - existing_size)
        max_size = min(MAX_UPLOAD_MB * 2**20, quota - baseline)
        if max_size < 0:
            raise web.HTTPRequestEntityTooLarge(max_size=quota, actual_size=baseline)

        temp_path = root / f".upload-{uuid.uuid4().hex}"
        size = 0
        try:
            with open(temp_path, "xb") as file:
                while True:
                    chunk = await field.read_chunk(size=64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_size:
                        raise web.HTTPRequestEntityTooLarge(max_size=max_size,
                                                            actual_size=size)
                    file.write(chunk)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            os.replace(temp_path, destination)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        state.audit("file.upload", username, {"name": name, "size": size})
        return web.json_response({"ok": True, "name": name, "size": size})

    async def api_file_download(self, request):
        username, user = self._file_identity(request)
        name = self._safe_filename(request.match_info.get("name"))
        path = self._user_file_dir(username, user) / name
        if not path.is_file() or path.is_symlink():
            raise web.HTTPNotFound(text="Файл не найден")
        state.audit("file.download", username, {"name": name, "size": path.stat().st_size})
        return web.FileResponse(
            path, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    async def api_file_delete(self, request):
        username, user = self._file_identity(request)
        data = await request.json()
        name = self._safe_filename(data.get("name"))
        path = self._user_file_dir(username, user) / name
        if not path.is_file() or path.is_symlink():
            raise web.HTTPNotFound(text="Файл не найден")
        size = path.stat().st_size
        path.unlink()
        state.audit("file.delete", username, {"name": name, "size": size})
        return web.json_response({"ok": True})

    async def api_user_delete(self, request):
        d = await request.json()
        username = str(d.get("username", "")).strip()
        actor, _, _ = self._can_manage_user(request, username, destructive=True)
        auth.delete_user(username, actor=actor)
        for s in list(self.sessions.values()):
            if s.username == username:
                await self._kick(s, "Доступ отозван")
        return web.json_response({"ok": True})

    async def api_invites(self, request):
        if not self._admin(request):
            raise web.HTTPForbidden()
        return web.json_response(auth.list_invites())

    async def api_invite_create(self, request):
        actor, actor_user = self._admin_identity(request)
        if not actor_user:
            raise web.HTTPForbidden()
        d = await request.json()
        role = d.get("role", "guest")
        if role not in ("guest", "user"):
            raise web.HTTPForbidden(text="Приглашение может создать только гостя или пользователя")
        try:
            code = auth.create_invite(role=role, profile=d.get("profile", "office"),
                                      ttl_hours=float(d.get("ttl_hours", 24)),
                                      priority=d.get("priority", "low"),
                                      allow_input=d.get("allow_input", True),
                                      allow_clipboard=d.get("allow_clipboard", False),
                                      allow_files=d.get("allow_files", False),
                                      allow_display=d.get("allow_display", False),
                                      session_hours=d.get("session_hours"),
                                      disk_quota_mb=d.get("disk_quota_mb", 512), actor=actor)
        except (TypeError, ValueError) as e:
            raise web.HTTPBadRequest(text=str(e))
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
        for key in ("accepting", "work_only_mode", "owner_gaming_mode"):
            if key in d:
                self.config[key] = bool(d[key])
        if "owner_reserve_percent" in d:
            self.config["owner_reserve_percent"] = max(0, min(80, int(d["owner_reserve_percent"])))
        if "max_sessions" in d:
            self.config["max_sessions"] = max(1, min(64, int(d["max_sessions"])))
        if "host_name" in d:
            name = str(d["host_name"] or "").strip()
            if len(name) > 64 or any(ord(ch) < 32 for ch in name):
                raise web.HTTPBadRequest(text="Имя хоста должно быть короче 65 символов")
            self.config["host_name"] = name or None
        if "node_type" in d:
            node_type = str(d["node_type"] or "auto")
            if node_type not in ("auto", "physical", "vm"):
                raise web.HTTPBadRequest(text="Неизвестный тип узла")
            self.config["node_type"] = node_type
        if "parent_host" in d:
            parent = str(d["parent_host"] or "").strip()
            if len(parent) > 64 or any(ord(ch) < 32 for ch in parent):
                raise web.HTTPBadRequest(text="Имя физического хоста должно быть короче 65 символов")
            self.config["parent_host"] = parent or None
        if "display_output" in d:
            requested_output = str(d["display_output"] or "auto")
            before = (self.display.selected_output()
                      if hasattr(self.display, "selected_output") else None)
            if self.sessions:
                outputs = self.display.outputs() if hasattr(self.display, "outputs") else []
                target = next((item for item in outputs
                               if item.get("id") == requested_output), None)
                if requested_output == "virtual":
                    target = next((item for item in outputs if item.get("virtual")), None)
                elif requested_output == "physical":
                    target = next((item for item in outputs if not item.get("virtual")), None)
                elif requested_output == "auto":
                    target = next((item for item in outputs if item.get("virtual")), None)
                    target = target or next((item for item in outputs if item.get("primary")), None)
                if target and before and target.get("id") != before.get("id"):
                    raise web.HTTPConflict(text="Отключите активные сессии перед сменой экрана")
            ok, selected, error = await asyncio.get_running_loop().run_in_executor(
                _executor, self.display.select_output, requested_output)
            if not ok:
                raise web.HTTPBadRequest(text=error or "Не удалось выбрать экран")
            self.config["display_output"] = requested_output
            self._sync_capture_output(selected)
            current = (selected or {}).get("current") or {}
            self._display_refresh_hz = int(current.get("refresh") or 0)
        state.save_config(self.config)
        state.audit("settings.update", actor, d)
        return web.json_response({"ok": True, "config": self.config})

    async def api_display_install(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        if not self._is_local(request):
            raise web.HTTPForbidden(
                text="Установку драйвера нужно подтвердить на самом Windows-хосте")
        ok, error = await asyncio.get_running_loop().run_in_executor(
            _executor, virtual_display.launch_installer)
        if not ok:
            raise web.HTTPBadRequest(text=error)
        state.audit("display.driver.install", actor)
        return web.json_response({"ok": True})

    async def api_app_role(self, request):
        actor = self._admin(request)
        if not actor:
            raise web.HTTPForbidden()
        if not self._is_local(request):
            raise web.HTTPForbidden(text="Роль можно менять только с самого компьютера")
        d = await request.json()
        role = d.get("role")
        if role not in ("host", "client"):
            raise web.HTTPBadRequest(text="Неизвестная роль")
        self.config["role"] = role
        state.save_config(self.config)
        state.audit("app.role", actor, {"role": role})

        def stop_app():
            raise web.GracefulExit()

        asyncio.get_running_loop().call_later(0.4, stop_app)
        return web.json_response({"ok": True, "role": role,
                                  "port": (self.config["host_port"] if role == "host"
                                           else self.config["client_port"])})

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
                if not self._admin(request):
                    await ws.close(code=4003, message=b"admin session expired")
                    break
                await ws.send_json({
                    "type": "tick",
                    "load": await self._get_load(),
                    "sessions": [s.info(self._stream_params(s))
                                 for s in self.sessions.values()],
                    "config": self.config,
                    "capture_ok": CAPTURE_OK, "input_ok": INPUT_OK,
                    "video": self._public_video_capabilities(),
                    "display": self._display_state(),
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

        # JPEG уже сжат: permessage-deflate только тратит CPU и добавляет jitter.
        # Небольшой writer_limit быстрее включает backpressure вместо скрытой
        # очереди устаревших кадров в памяти процесса.
        ws = web.WebSocketResponse(max_msg_size=8 * 2**20, compress=False,
                                   writer_limit=64 * 1024)
        await ws.prepare(request)
        self._tune_stream_socket(request)
        sess = Session(ws, username, user, request.remote, self._route_for(request.remote))
        self.sessions[sess.sid] = sess
        state.log_session_event("connect", {"sid": sess.sid, "username": username,
                                            "ip": sess.ip, "route": sess.route})
        scr = [self.capture.frame[1], self.capture.frame[2]] if self.capture.frame else None
        stream_state = self._stream_state(sess, user)
        await ws.send_json({"type": "hello", "sid": sess.sid,
                            "host_name": self.host_name(),
                            "node": self.node_identity(),
                            "route": sess.route,
                            "codec": "H.264/NVENC с автоматическим MJPEG fallback",
                            "video": self._public_video_capabilities(),
                            "screen": scr,
                            "profile": sess.profile,
                            "fps": sess.fps, "quality": sess.quality, "scale": sess.scale,
                            "stream": stream_state,
                            "limits": stream_state["limits"],
                            "display": self._display_state(sess, user),
                            "network": self._network_state(sess),
                            "transport": {"direct": True, "low_latency": True},
                            "permissions": self._permission_payload(user),
                            "isolation_note": (
                                "Подключение идёт к отдельной виртуальной машине. "
                                "Сессии этого узла видят рабочий стол этой VM."
                                if self.node_identity()["type"] == "vm" else
                                "Все удалённые сессии этого узла видят общий рабочий стол физического хоста."
                            )})
        self._refresh_capture_fps()
        sender = asyncio.create_task(self._media_sender(sess))
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
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                current_user = auth.get_user(username)
                if not auth.user_is_active(current_user):
                    await self._kick(sess, "Доступ истёк или был отозван")
                    break
                if t == "ping":
                    await ws.send_json({"type": "pong", "t": d.get("t"),
                                        "wall": d.get("wall"),
                                        "server_wall": time.time() * 1000})
                elif t == "config":
                    display_state, display_reasons = await self._apply_display_config(
                        sess, d, current_user)
                    applied = self._apply_stream_config(sess, d, current_user)
                    applied["display"] = display_state
                    applied["reasons"] = list(dict.fromkeys(
                        applied.get("reasons", []) + display_reasons))
                    await ws.send_json(applied)
                elif t == "frame_ack":
                    self._apply_frame_ack(sess, d)
                elif t == "video_ack":
                    self._apply_video_ack(sess, d)
                elif t == "video_resync" and sess.codec == "h264":
                    now = time.monotonic()
                    if now - sess.last_video_resync >= 0.5:
                        sess.last_video_resync = now
                        sess.codec_generation += 1
                elif t == "refresh":
                    # клиент просит полный кадр (потерял дельта-цепочку)
                    sess.force_full = True
                    self._notify_frame()
                elif t == "input":
                    if self._input_permission(current_user)[0]:
                        for ev in d.get("events", [])[:256]:
                            self.injector.apply(ev)
                elif t == "clipboard_get" and current_user.get("allow_clipboard"):
                    loop = asyncio.get_running_loop()
                    text = await loop.run_in_executor(_executor, clipboard_get)
                    await ws.send_json({"type": "clipboard", "text": text or ""})
                elif t == "clipboard_set" and current_user.get("allow_clipboard"):
                    text = str(d.get("text", ""))[:1_000_000]
                    await asyncio.get_running_loop().run_in_executor(
                        _executor, clipboard_set, text)
        finally:
            sender.cancel()
            stats.cancel()
            cursor.cancel()
            await asyncio.gather(sender, stats, cursor, return_exceptions=True)
            await self._release_display(sess)
            self.sessions.pop(sess.sid, None)
            self._refresh_capture_fps()
            state.log_session_event("disconnect", {
                "sid": sess.sid, "username": username, "ip": sess.ip,
                "frames": sess.frames, "mb_sent": round(sess.total_bytes / 2**20, 1),
                "duration_s": int(time.time() - sess.connected_at),
                "reason": sess.kick_reason or "client"})
        return ws

    async def _media_sender(self, sess):
        """Переключает медиаконвейер без разрыва сессии."""
        announced = None
        while not sess.ws.closed:
            if sess.codec == "h264":
                reason = await self._h264_sender(sess)
                if reason and not sess.ws.closed:
                    sess.codec = "mjpeg"
                    sess.codec_reason = f"NVENC отключён: {reason}. Используется MJPEG"
                    sess.codec_generation += 1
                    sess.force_full = True
                    chosen = await self._fit_display_for_mjpeg(sess)
                    if chosen:
                        sess.codec_reason += (
                            f"; Windows переключена на {chosen['width']}×{chosen['height']} "
                            "для сохранения плавности")
                    self._refresh_capture_fps()
                announced = "h264"
                continue
            if announced != "mjpeg":
                try:
                    await sess.ws.send_json({
                        "type": "video_stream", "codec": "mjpeg",
                        "reason": sess.codec_reason})
                except (ConnectionError, RuntimeError):
                    break
                announced = "mjpeg"
            await self._frame_sender(sess)

    async def _h264_sender(self, sess):
        """Desktop Duplication -> NVENC -> fragmented MP4 -> WebSocket."""
        generation = sess.codec_generation
        fps, quality, _ = self._stream_params(sess)
        current = None
        try:
            current = self.display.current() if self.display.available else None
            output = (self.display.selected_output()
                      if self.display.available and hasattr(self.display, "selected_output") else None)
        except Exception:
            current, output = None, None
        frame = self.capture.frame
        width = int((current or {}).get("width") or (frame[1] if frame else 1920))
        height = int((current or {}).get("height") or (frame[2] if frame else 1080))
        network = self._network_state(sess)
        try:
            command = video_encoder.command(
                self.video_encoder, fps, quality, width, height,
                network.get("safe_stream_mbps", 0),
                output_idx=int((output or {}).get("output_index", 0) or 0),
                device_idx=int((output or {}).get("device_index", 0) or 0))
        except ValueError as exc:
            return str(exc)
        sess.required_mbps = video_encoder.target_bitrate_mbps(
            width, height, fps, quality, network.get("safe_stream_mbps", 0))
        creationflags = 0x08000000 if os.name == "nt" else 0
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, creationflags=creationflags)
        except (OSError, NotImplementedError) as exc:
            return f"не удалось запустить FFmpeg ({exc})"
        stderr_tail = collections.deque(maxlen=12)

        async def read_errors():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                stderr_tail.append(line.decode("utf-8", "replace").strip())

        error_task = asyncio.create_task(read_errors())
        try:
            await sess.ws.send_json({
                "type": "video_stream", "codec": "h264",
                "mime": video_encoder.H264_MIME,
                "width": width, "height": height, "fps": fps,
                "encoder": "NVENC"})
            sent_any = False
            while (not sess.ws.closed and sess.codec == "h264"
                   and sess.codec_generation == generation):
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(64 * 1024), timeout=5.0)
                except asyncio.TimeoutError:
                    return "FFmpeg не выдал видеоданные"
                if not chunk:
                    detail = next((line for line in reversed(stderr_tail) if line), "")
                    return detail[-280:] or f"FFmpeg завершился с кодом {proc.returncode}"
                sent_any = True
                started = time.perf_counter()
                await sess.ws.send_bytes(chunk)
                send_ms = (time.perf_counter() - started) * 1000
                sess.send_ms_ewma = (send_ms if not sess.send_ms_ewma
                                     else sess.send_ms_ewma * 0.75 + send_ms * 0.25)
                self._record_stream_bytes(sess, len(chunk))
            return None if sent_any else "NVENC не успел запустить поток"
        except (ConnectionError, RuntimeError):
            return None
        finally:
            if proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=1.5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    if proc.returncode is None:
                        proc.kill()
            error_task.cancel()
            await asyncio.gather(error_task, return_exceptions=True)

    def _record_stream_bytes(self, sess, count):
        sess.bytes += count
        sess.total_bytes += count
        now = time.time()
        if now - sess.last_bytes_ts >= 1.0:
            sess.bitrate_mbps = sess.bytes * 8 / 1e6 / (now - sess.last_bytes_ts)
            sess.bytes = 0
            sess.last_bytes_ts = now

    async def _frame_sender(self, sess):
        """Отправка кадров: ДЕЛЬТА-КАДРЫ (только изменённые области экрана) —
        это радикально снижает битрейт и задержку по Wi-Fi. Полный кадр идёт
        при подключении, смене параметров, запросе клиента и редко после дельт.
        Бинарный формат v2: 24-байтовый заголовок
        [A7, 2, kind, 0, x, y, fw, fh, id:u32, capture_ts:f64] + JPEG.
        Адаптация учитывает подтверждённую браузером очередь и задержку."""
        last_seq = -1
        last_params = None
        next_pick = 0.0            # следующий дедлайн кадра (с переносом остатка)
        last_fps = None
        last_full_t = 0.0
        dirty_since_full = False
        send_ewma = 0.0
        last_adapt = time.monotonic()
        stale_skips = 0
        pressure_since = None
        pipeline_pressure_since = None
        pipeline_good_since = None
        last_pipeline_adapt = time.monotonic()
        while not sess.ws.closed and sess.codec == "mjpeg":
            ev = self._frame_evt
            fps, quality, scale = self._stream_params(sess)
            budget = 1.0 / max(fps, 1)
            if fps != last_fps:
                next_pick = 0.0
                last_fps = fps
            frame = self.capture.frame
            now = time.monotonic()
            params = (int(quality), round(scale, 6))
            # WebSocket/TCP надёжен; частые full-frame создавали заметный пик
            # битрейта каждые 5 секунд. Редкий кадр нужен лишь для самопроверки.
            heal = dirty_since_full and now - last_full_t >= 30.0
            changed = frame is not None and (
                frame[4] != last_seq or params != last_params
                or sess.force_full or heal)
            if not changed or (next_pick and now < next_pick):
                # Ждём СОБЫТИЕ нового кадра, а не таймер: системный таймер
                # Windows тикает ~15.6 мс и таймерный пейсинг режет FPS вдвое.
                timeout = 0.25
                if changed and next_pick:
                    timeout = min(timeout, max(0.001, next_pick - now))
                if ev is None:
                    await asyncio.sleep(min(0.05, timeout))
                else:
                    try:
                        await asyncio.wait_for(ev.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        pass
                continue
            if not next_pick or now - next_pick > budget * 4:
                next_pick = now + budget
            else:
                next_pick += budget
                while next_pick <= now:
                    next_pick += budget
            region = None
            if (not sess.force_full and not heal
                    and params == last_params and last_seq >= 0):
                region = self.capture.bbox_since(last_seq, frame[4])
                if region is not None:
                    x0, y0, x1, y1 = region
                    # область почти во весь экран — полный кадр проще и не хуже
                    if (x1 - x0) * (y1 - y0) > 0.6 * frame[1] * frame[2]:
                        region = None
            try:
                encode_started = time.perf_counter()
                data, sx, sy, fw, fh = await self._encoded(frame, quality, scale, region)
            except Exception:
                await asyncio.sleep(0.1)
                continue
            encode_dt = time.perf_counter() - encode_started
            encode_ms = encode_dt * 1000
            sess.encode_ms_ewma = (encode_ms if not sess.encode_ms_ewma
                                   else sess.encode_ms_ewma * 0.75 + encode_ms * 0.25)
            now_encoded = time.monotonic()
            pipeline_ms = max(sess.encode_ms_ewma,
                              float(getattr(self.capture, "process_ms_ewma", 0.0)))
            pipeline_level = self._pipeline_pressure_level(
                pipeline_ms, budget * 1000)
            if sess.adaptive:
                if pipeline_level:
                    pipeline_pressure_since = pipeline_pressure_since or now_encoded
                    pipeline_good_since = None
                else:
                    pipeline_pressure_since = None
                    pipeline_good_since = pipeline_good_since or now_encoded
                if (pipeline_pressure_since
                        and now_encoded - pipeline_pressure_since >= 0.5
                        and now_encoded - last_pipeline_adapt > 1.0
                        and sess.pipeline_degrade < 3):
                    sess.pipeline_degrade = max(
                        sess.pipeline_degrade + 1, min(3, pipeline_level))
                    sess.encode_ms_ewma = 0.0
                    sess.force_full = True
                    pipeline_pressure_since = None
                    pipeline_good_since = None
                    last_pipeline_adapt = now_encoded
                    self._refresh_capture_fps()
                    self._notify_frame()
                elif (pipeline_good_since
                      and now_encoded - pipeline_good_since >= 8.0
                      and now_encoded - last_pipeline_adapt >= 8.0
                      and sess.pipeline_degrade > 0):
                    sess.pipeline_degrade -= 1
                    sess.encode_ms_ewma = 0.0
                    sess.force_full = True
                    pipeline_good_since = None
                    last_pipeline_adapt = now_encoded
                    self._refresh_capture_fps()
                    self._notify_frame()
            elif sess.pipeline_degrade:
                sess.pipeline_degrade = 0
                pipeline_pressure_since = None
                pipeline_good_since = None
                self._refresh_capture_fps()
            latest = self.capture.frame
            if (latest is not None and latest[4] != frame[4]
                    and encode_dt > budget * 0.65 and stale_skips < 1):
                # Не отправляем кадр, который устарел ещё во время JPEG-кодирования.
                stale_skips += 1
                sess.stale_drops += 1
                continue
            stale_skips = 0
            kind = 1 if region is not None else 0
            sess.send_id = (sess.send_id + 1) & 0xffffffff
            if sess.send_id == 0:
                sess.send_id = 1
            msg = struct.pack("<BBBBHHHHId", 0xA7, 2, kind, 0, sx, sy, fw, fh,
                              sess.send_id, float(frame[3])) + data
            last_seq, last_params = frame[4], params
            sess.force_full = False
            if kind == 0:
                last_full_t = now
                dirty_since_full = False
            else:
                dirty_since_full = True
            t0 = time.perf_counter()
            sent_at = time.monotonic()
            try:
                await sess.ws.send_bytes(msg)
            except (ConnectionError, RuntimeError):
                break
            send_dt = time.perf_counter() - t0
            send_ms = send_dt * 1000
            sess.send_ms_ewma = (send_ms if not sess.send_ms_ewma
                                 else sess.send_ms_ewma * 0.75 + send_ms * 0.25)
            sess.sent_times.append((sess.send_id, sent_at))
            send_ewma = send_dt if not send_ewma else send_ewma * 0.7 + send_dt * 0.3
            now2 = time.monotonic()
            sess.frame_times.append(now2)
            sess.frame_bytes_ewma = (len(msg) if not sess.frame_bytes_ewma
                                     else sess.frame_bytes_ewma * 0.8 + len(msg) * 0.2)
            sess.required_mbps = sess.frame_bytes_ewma * 8 * fps / 1e6
            network = self._network_state(sess)
            budget_mbps = network["safe_stream_mbps"]
            pressure_level = self._network_pressure_level(
                sess.required_mbps, budget_mbps)
            over_link = pressure_level > 0
            pressure_since = (pressure_since or now2) if over_link else None
            if sess.adaptive:
                ack_limit = max(80.0, budget * 5 * 1000)
                lag_bad = (sess.client_queue > 1
                           or (sess.last_ack_id and sess.ack_latency_ms > ack_limit))
                lag_good = (sess.client_queue == 0
                            and (not sess.last_ack_id
                                 or sess.ack_latency_ms < max(45.0, budget * 3 * 1000)))
                bandwidth_bad = bool(pressure_since and now2 - pressure_since >= 0.8)
                bandwidth_good = bool(not budget_mbps
                                      or sess.required_mbps < budget_mbps * 0.7)
                lag_is_network = self._network_lag_pressure(
                    lag_bad, send_ewma, budget, sess.required_mbps, budget_mbps)
                if bandwidth_bad and sess.net_degrade < 3 and now2 - last_adapt > 1.0:
                    # Q95/120 FPS на 100-Мбит порту может превышать безопасный
                    # бюджет в 3–5 раз. Прыгаем сразу к расчётной ступени, иначе
                    # пользователь несколько секунд видит TCP-фризы.
                    sess.net_degrade = max(
                        sess.net_degrade + 1, min(3, pressure_level))
                    sess.frame_bytes_ewma = 0.0
                    pressure_since = None
                    last_adapt = now2
                elif send_dt > max(0.25, budget * 4) and sess.net_degrade < 3 and now2 - last_adapt > 1.0:
                    sess.net_degrade += 1  # резкий затык канала — реагируем сразу
                    last_adapt = now2
                elif (lag_is_network and sess.net_degrade < 3
                      and now2 - last_adapt > 2.0):
                    sess.net_degrade += 1
                    last_adapt = now2
                elif (lag_good and bandwidth_good and send_ewma < budget * 0.4
                      and sess.net_degrade > 0 and now2 - last_adapt > 8.0):
                    sess.net_degrade -= 1
                    last_adapt = now2
            elif sess.net_degrade:
                sess.net_degrade = 0
            sess.frames += 1
            self._record_stream_bytes(sess, len(msg))

    async def _stats_sender(self, sess):
        while not sess.ws.closed:
            user = auth.get_user(sess.username)
            if not auth.user_is_active(user):
                await self._kick(sess, "Доступ истёк или был отозван")
                break
            load = await self._get_load()
            fps, quality, scale = self._stream_params(sess)
            sess.user = user
            sess.priority = user.get("priority", sess.priority)
            warn = list(sess.warnings)
            if sess.degrade > 0:
                warn.append(f"Хост перегружен: применена ступень деградации {sess.degrade} "
                            f"(битрейт → FPS → разрешение)")
            if sess.net_degrade > 0:
                warn.append(f"Канал не успевает за потоком (ступень {sess.net_degrade}): "
                            f"качество временно снижено. Помогут меньший FPS/масштаб "
                            f"или кабельное подключение.")
            if sess.pipeline_degrade > 0 and sess.codec == "mjpeg":
                warn.append(
                    f"Хост не успевает захватывать или кодировать выбранный поток "
                    f"(ступень {sess.pipeline_degrade}). Автоадаптация снизила "
                    f"параметры до устойчивого режима.")
            network = self._network_state(sess)
            capture_perf = self.capture.performance()
            media_fps = round(_recent_rate(sess.frame_times), 1)
            if network["limited"]:
                warn.append(
                    f"Ethernet хоста согласован только на {network['link_mbps']} Мбит/с. "
                    "Проверьте кабель Cat5e/Cat6, гигабитный порт и Auto Negotiation.")
            if sess.client_queue > 1:
                warn.append(f"Браузер не успевает декодировать поток: в очереди "
                            f"{sess.client_queue} кадр(а)")
            try:
                await sess.ws.send_json({
                    "type": "stats", "host": load,
                    "session": sess.info((fps, quality, scale)),
                    "codec": ("H.264 NVENC" if sess.codec == "h264" else "MJPEG"),
                    "video": self._public_video_capabilities(),
                    "capture": ("ddagrab" if sess.codec == "h264"
                                else self.capture.backend),
                    "capture_fps": (media_fps if sess.codec == "h264"
                                    else capture_perf["fps"]),
                    "capture_process_ms": (0 if sess.codec == "h264"
                                           else capture_perf["process_ms"]),
                    "route": sess.route,
                    "fps_target": fps, "quality": quality, "scale": scale,
                    "network": network,
                    "stream": self._stream_state(sess, user),
                    "display": self._display_state(sess, user),
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
            node = self.node_identity()
            payload = json.dumps({
                "app": "App_Remote", "v": 1, "role": "host",
                "name": self.host_name(),
                "node_type": node["type"],
                "node_detected": node["detected"],
                "hypervisor": node["hypervisor"],
                "parent_host": node["parent_host"],
                "port": self.config["host_port"],
                "cpu": s.get("cpu"), "cores": s.get("cores"), "threads": s.get("threads"),
                "ram_gb": s.get("ram_gb"),
                "gpu": (s.get("gpus") or [{}])[0].get("name"),
                "link_mbps": capability.lan_link_mbps(s),
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
