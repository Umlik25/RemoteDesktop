"""Windows display enumeration, selection and mode management.

The streaming desktop can be a physical output or an Indirect Display Driver
(IDD) output.  Resolution changes are always applied to the selected output,
so a virtual display is independent from the monitor plugged into the host.
"""
import math
import platform
import re
import threading


IS_WIN = platform.system() == "Windows"

VIRTUAL_MARKERS = (
    "virtual display", "virtual monitor", "indirect display", "iddsample",
    "mttvdd", "mtt1337", "usbmmid", "parsec", "rustdesk", "sunshine",
)


def is_virtual_output(*values):
    text = " ".join(str(value or "") for value in values).lower()
    return any(marker in text for marker in VIRTUAL_MARKERS)


def best_mode(modes, target_width, target_height, max_pixels=None):
    """Return the closest mode by aspect ratio and then pixel count."""
    if not modes or target_width < 1 or target_height < 1:
        return None
    candidates = list(modes)
    if max_pixels:
        within_budget = [
            mode for mode in candidates
            if mode["width"] * mode["height"] <= int(max_pixels)
        ]
        if within_budget:
            candidates = within_budget
    target_aspect = target_width / target_height
    target_area = target_width * target_height

    def score(mode):
        aspect = mode["width"] / mode["height"]
        aspect_error = abs(math.log(aspect / target_aspect))
        area_error = abs(math.log((mode["width"] * mode["height"]) / target_area))
        return aspect_error * 8 + area_error, -mode.get("refresh", 0)

    return min(candidates, key=score)


if IS_WIN:
    import ctypes
    from ctypes import wintypes

    CCHDEVICENAME = 32
    CCHFORMNAME = 32
    ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
    DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
    DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
    DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008
    DM_BITSPERPEL = 0x00040000
    DM_PELSWIDTH = 0x00080000
    DM_PELSHEIGHT = 0x00100000
    DM_DISPLAYFREQUENCY = 0x00400000
    CDS_TEST = 0x00000002
    DISP_CHANGE_SUCCESSFUL = 0

    class _POINTL(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class _PrinterFields(ctypes.Structure):
        _fields_ = [
            ("dmOrientation", wintypes.SHORT), ("dmPaperSize", wintypes.SHORT),
            ("dmPaperLength", wintypes.SHORT), ("dmPaperWidth", wintypes.SHORT),
            ("dmScale", wintypes.SHORT), ("dmCopies", wintypes.SHORT),
            ("dmDefaultSource", wintypes.SHORT), ("dmPrintQuality", wintypes.SHORT),
        ]

    class _DisplayFields(ctypes.Structure):
        _fields_ = [
            ("dmPosition", _POINTL),
            ("dmDisplayOrientation", wintypes.DWORD),
            ("dmDisplayFixedOutput", wintypes.DWORD),
        ]

    class _FieldsUnion(ctypes.Union):
        _fields_ = [("printer", _PrinterFields), ("display", _DisplayFields)]

    class _FlagsUnion(ctypes.Union):
        _fields_ = [("dmDisplayFlags", wintypes.DWORD), ("dmNup", wintypes.DWORD)]

    class _DEVMODEW(ctypes.Structure):
        _anonymous_ = ("fields", "flags")
        _fields_ = [
            ("dmDeviceName", wintypes.WCHAR * CCHDEVICENAME),
            ("dmSpecVersion", wintypes.WORD), ("dmDriverVersion", wintypes.WORD),
            ("dmSize", wintypes.WORD), ("dmDriverExtra", wintypes.WORD),
            ("dmFields", wintypes.DWORD), ("fields", _FieldsUnion),
            ("dmColor", wintypes.SHORT), ("dmDuplex", wintypes.SHORT),
            ("dmYResolution", wintypes.SHORT), ("dmTTOption", wintypes.SHORT),
            ("dmCollate", wintypes.SHORT),
            ("dmFormName", wintypes.WCHAR * CCHFORMNAME),
            ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
            ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
            ("flags", _FlagsUnion), ("dmDisplayFrequency", wintypes.DWORD),
            ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
            ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
            ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
            ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD),
        ]

    class _DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("DeviceName", wintypes.WCHAR * 32),
            ("DeviceString", wintypes.WCHAR * 128),
            ("StateFlags", wintypes.DWORD),
            ("DeviceID", wintypes.WCHAR * 128),
            ("DeviceKey", wintypes.WCHAR * 128),
        ]

    _user32 = ctypes.windll.user32
    _user32.EnumDisplaySettingsW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DEVMODEW)]
    _user32.EnumDisplaySettingsW.restype = wintypes.BOOL
    _user32.EnumDisplayDevicesW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DISPLAY_DEVICEW),
        wintypes.DWORD]
    _user32.EnumDisplayDevicesW.restype = wintypes.BOOL
    _user32.ChangeDisplaySettingsExW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(_DEVMODEW), wintypes.HWND,
        wintypes.DWORD, wintypes.LPVOID]
    _user32.ChangeDisplaySettingsExW.restype = wintypes.LONG


class DisplayManager:
    def __init__(self, preferred="auto"):
        self._lock = threading.RLock()
        self._mode_cache = {}
        self._outputs = self._enumerate_outputs()
        self._selected_name = None
        self._originals = {}
        ok, _, _ = self.select_output(preferred)
        if not ok and str(preferred or "auto").lower() != "auto":
            self.select_output("auto")
        current = self.current()
        if self._selected_name and current:
            self._originals[self._selected_name] = current
        # Kept for compatibility with the existing session restore path.
        self._original = current

    @property
    def available(self):
        return IS_WIN and getattr(self, "_original", None) is not None

    def _devmode(self, index, device_name=None):
        if not IS_WIN:
            return None
        mode = _DEVMODEW()
        mode.dmSize = ctypes.sizeof(_DEVMODEW)
        name = device_name if device_name is not None else getattr(self, "_selected_name", None)
        if not _user32.EnumDisplaySettingsW(name, index, ctypes.byref(mode)):
            return None
        return mode

    @staticmethod
    def _as_dict(mode):
        if mode is None:
            return None
        return {
            "width": int(mode.dmPelsWidth), "height": int(mode.dmPelsHeight),
            "refresh": int(mode.dmDisplayFrequency or 0),
            "bpp": int(mode.dmBitsPerPel or 0),
            "x": int(mode.dmPosition.x), "y": int(mode.dmPosition.y),
        }

    def _enumerate_outputs(self):
        if not IS_WIN:
            return []
        outputs = []
        index = 0
        while True:
            adapter = _DISPLAY_DEVICEW()
            adapter.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
            if not _user32.EnumDisplayDevicesW(None, index, ctypes.byref(adapter), 0):
                break
            index += 1
            flags = int(adapter.StateFlags)
            if not flags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                continue
            if flags & DISPLAY_DEVICE_MIRRORING_DRIVER:
                continue
            monitor_name = ""
            monitor_id = ""
            monitor_index = 0
            while True:
                monitor = _DISPLAY_DEVICEW()
                monitor.cb = ctypes.sizeof(_DISPLAY_DEVICEW)
                if not _user32.EnumDisplayDevicesW(
                        adapter.DeviceName, monitor_index, ctypes.byref(monitor), 0):
                    break
                monitor_index += 1
                monitor_name = monitor_name or str(monitor.DeviceString or "")
                monitor_id = monitor_id or str(monitor.DeviceID or "")
                if monitor.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
                    monitor_name = str(monitor.DeviceString or monitor_name)
                    monitor_id = str(monitor.DeviceID or monitor_id)
                    break
            current = self._as_dict(self._devmode(ENUM_CURRENT_SETTINGS, adapter.DeviceName))
            if not current:
                continue
            match = re.search(r"DISPLAY(\d+)$", str(adapter.DeviceName), re.IGNORECASE)
            capture_index = max(0, int(match.group(1)) - 1) if match else len(outputs)
            virtual = is_virtual_output(
                adapter.DeviceString, adapter.DeviceID, monitor_name, monitor_id)
            outputs.append({
                "id": str(adapter.DeviceName),
                "device_name": str(adapter.DeviceName),
                "name": monitor_name or str(adapter.DeviceString) or str(adapter.DeviceName),
                "adapter": str(adapter.DeviceString or ""),
                "device_id": monitor_id or str(adapter.DeviceID or ""),
                "primary": bool(flags & DISPLAY_DEVICE_PRIMARY_DEVICE),
                "virtual": virtual,
                "capture_index": capture_index,
                "device_index": 0,
                "output_index": capture_index,
                "current": current,
            })
        return outputs

    def outputs(self, refresh=False):
        if not IS_WIN:
            return []
        if refresh or not hasattr(self, "_outputs"):
            selected = getattr(self, "_selected_name", None)
            self._outputs = self._enumerate_outputs()
            if selected and any(item["device_name"] == selected for item in self._outputs):
                self._selected_name = selected
        result = []
        for item in self._outputs:
            output = dict(item)
            output["current"] = self.current(item["device_name"]) or item.get("current")
            output["selected"] = item["device_name"] == getattr(self, "_selected_name", None)
            result.append(output)
        return result

    def selected_output(self):
        selected = getattr(self, "_selected_name", None)
        return next((item for item in self.outputs() if item["device_name"] == selected), None)

    def select_output(self, requested="auto"):
        if not IS_WIN:
            return False, None, "Выбор дисплея доступен только на Windows-хосте"
        outputs = self.outputs(refresh=True)
        if not outputs:
            return False, None, "Windows не сообщает ни одного активного дисплея"
        requested = str(requested or "auto").strip()
        lowered = requested.lower()
        chosen = None
        if lowered in ("auto", "virtual"):
            chosen = next((item for item in outputs if item["virtual"]), None)
            if chosen is None and lowered == "virtual":
                return False, None, "Виртуальный экран не найден: установите и включите IDD-драйвер"
        elif lowered == "physical":
            chosen = next((item for item in outputs if item["primary"] and not item["virtual"]), None)
            chosen = chosen or next((item for item in outputs if not item["virtual"]), None)
        else:
            chosen = next((item for item in outputs
                           if item["id"].lower() == lowered), None)
            if chosen is None:
                return False, None, f"Экран {requested} больше не доступен в Windows"
        chosen = chosen or next((item for item in outputs if item["primary"]), outputs[0])
        self._selected_name = chosen["device_name"]
        current = self.current()
        originals = getattr(self, "_originals", None)
        if isinstance(originals, dict) and current:
            originals.setdefault(self._selected_name, current)
        self._original = (originals or {}).get(self._selected_name, current)
        return True, self.selected_output(), None

    def current(self, device_name=None):
        if not IS_WIN:
            return None
        return self._as_dict(self._devmode(
            ENUM_CURRENT_SETTINGS,
            device_name if device_name is not None else getattr(self, "_selected_name", None)))

    def modes(self, device_name=None):
        if not IS_WIN:
            return []
        name = device_name if device_name is not None else getattr(self, "_selected_name", None)
        cache = getattr(self, "_mode_cache", {})
        if isinstance(cache, dict) and name in cache:
            return list(cache[name])
        unique = {}
        index = 0
        while True:
            mode = self._devmode(index, name)
            if mode is None:
                break
            index += 1
            info = self._as_dict(mode)
            if info["bpp"] < 32 or info["width"] < 800 or info["height"] < 600:
                continue
            key = (info["width"], info["height"])
            if key not in unique or info["refresh"] > unique[key]["refresh"]:
                unique[key] = info
        result = sorted(
            unique.values(), key=lambda item: item["width"] * item["height"], reverse=True)
        if isinstance(cache, dict):
            cache[name] = result
        return list(result)

    def _find_devmode(self, width, height, preferred_refresh=0):
        candidates = []
        index = 0
        while IS_WIN:
            mode = self._devmode(index)
            if mode is None:
                break
            index += 1
            if (mode.dmPelsWidth == width and mode.dmPelsHeight == height
                    and mode.dmBitsPerPel >= 32):
                candidates.append(mode)
        if not candidates:
            return None
        return min(candidates, key=lambda mode: (
            abs(int(mode.dmDisplayFrequency or 0) - int(preferred_refresh or 0)),
            -int(mode.dmDisplayFrequency or 0)))

    def set_mode(self, width, height, refresh=0):
        if not self.available:
            return False, "Смена системного разрешения доступна только на Windows-хосте"
        with self._lock:
            selected = self.selected_output() if hasattr(self, "_outputs") else None
            original = getattr(self, "_original", None) or {}
            # Physical monitors can advertise modes they cannot actually sync.
            # An IDD has no panel limit, so every mode it advertises is allowed.
            if not (selected and selected.get("virtual")):
                ow, oh = original.get("width"), original.get("height")
                if ow and oh and (int(width) > ow or int(height) > oh):
                    return False, (f"{width}×{height} превышает исходный режим монитора "
                                   f"{ow}×{oh} — не переключаю, чтобы не выйти за диапазон")
            current = self.current() or {}
            mode = self._find_devmode(int(width), int(height), refresh or current.get("refresh", 0))
            if mode is None:
                return False, f"Выбранный дисплей не поддерживает {width}×{height}"
            mode.dmFields = DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT
            if mode.dmDisplayFrequency:
                mode.dmFields |= DM_DISPLAYFREQUENCY
            name = getattr(self, "_selected_name", None)
            test = _user32.ChangeDisplaySettingsExW(
                name, ctypes.byref(mode), None, CDS_TEST, None)
            if test != DISP_CHANGE_SUCCESSFUL:
                return False, f"Windows отклонила режим {width}×{height} (код {test})"
            result = _user32.ChangeDisplaySettingsExW(name, ctypes.byref(mode), None, 0, None)
            if result != DISP_CHANGE_SUCCESSFUL:
                return False, f"Не удалось сменить разрешение Windows (код {result})"
            if isinstance(getattr(self, "_mode_cache", None), dict):
                self._mode_cache.pop(name, None)
            return True, None

    def set_best(self, target_width, target_height, max_pixels=None):
        selected = self.selected_output() if hasattr(self, "_outputs") else None
        modes = self.modes()
        if selected and selected.get("virtual"):
            safe = modes
        else:
            original = getattr(self, "_original", None) or {}
            ow = original.get("width", 1 << 30)
            oh = original.get("height", 1 << 30)
            safe = [mode for mode in modes
                    if mode["width"] <= ow and mode["height"] <= oh]
        chosen = best_mode(
            safe, int(target_width), int(target_height), max_pixels=max_pixels)
        if not chosen:
            return False, None, "Поддерживаемые режимы дисплея не найдены"
        ok, error = self.set_mode(chosen["width"], chosen["height"], chosen.get("refresh", 0))
        return ok, chosen if ok else None, error

    def restore(self):
        originals = getattr(self, "_originals", {})
        original = originals.get(getattr(self, "_selected_name", None)) if originals else None
        original = original or getattr(self, "_original", None)
        if not original:
            return False
        ok, _ = self.set_mode(
            original["width"], original["height"], original.get("refresh", 0))
        return ok
