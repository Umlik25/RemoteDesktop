"""Управление разрешением общего рабочего стола Windows.

Это не виртуальный монитор: ChangeDisplaySettingsEx меняет режим физического
основного дисплея. HostServer выдаёт такую возможность только явно разрешённой
сессии и восстанавливает исходный режим при отключении.
"""
import math
import platform
import threading


IS_WIN = platform.system() == "Windows"


def best_mode(modes, target_width, target_height):
    """Ближайший режим по aspect ratio, затем по числу пикселей."""
    if not modes or target_width < 1 or target_height < 1:
        return None
    target_aspect = target_width / target_height
    target_area = target_width * target_height

    def score(mode):
        aspect = mode["width"] / mode["height"]
        aspect_error = abs(math.log(aspect / target_aspect))
        area_error = abs(math.log((mode["width"] * mode["height"]) / target_area))
        # Геометрия важнее абсолютного числа пикселей: меньше чёрных полос.
        return aspect_error * 8 + area_error, -mode.get("refresh", 0)

    return min(modes, key=score)


if IS_WIN:
    import ctypes
    from ctypes import wintypes

    CCHDEVICENAME = 32
    CCHFORMNAME = 32
    ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
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

    _user32 = ctypes.windll.user32
    _user32.EnumDisplaySettingsW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(_DEVMODEW)]
    _user32.EnumDisplaySettingsW.restype = wintypes.BOOL
    _user32.ChangeDisplaySettingsExW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(_DEVMODEW), wintypes.HWND,
        wintypes.DWORD, wintypes.LPVOID]
    _user32.ChangeDisplaySettingsExW.restype = wintypes.LONG


class DisplayManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._mode_cache = None
        self._original = self.current()

    @property
    def available(self):
        return IS_WIN and self._original is not None

    def _devmode(self, index):
        if not IS_WIN:
            return None
        mode = _DEVMODEW()
        mode.dmSize = ctypes.sizeof(_DEVMODEW)
        if not _user32.EnumDisplaySettingsW(None, index, ctypes.byref(mode)):
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
        }

    def current(self):
        if not IS_WIN:
            return None
        return self._as_dict(self._devmode(ENUM_CURRENT_SETTINGS))

    def modes(self):
        if not IS_WIN:
            return []
        if self._mode_cache is not None:
            return list(self._mode_cache)
        unique = {}
        index = 0
        while True:
            mode = self._devmode(index)
            if mode is None:
                break
            index += 1
            info = self._as_dict(mode)
            if info["bpp"] < 32 or info["width"] < 800 or info["height"] < 600:
                continue
            key = (info["width"], info["height"])
            if key not in unique or info["refresh"] > unique[key]["refresh"]:
                unique[key] = info
        self._mode_cache = sorted(
            unique.values(), key=lambda item: item["width"] * item["height"], reverse=True)
        return list(self._mode_cache)

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
            current = self.current() or {}
            mode = self._find_devmode(int(width), int(height), refresh or current.get("refresh", 0))
            if mode is None:
                return False, f"Windows/монитор не поддерживает {width}×{height}"
            mode.dmFields = DM_BITSPERPEL | DM_PELSWIDTH | DM_PELSHEIGHT
            if mode.dmDisplayFrequency:
                mode.dmFields |= DM_DISPLAYFREQUENCY
            test = _user32.ChangeDisplaySettingsExW(None, ctypes.byref(mode), None, CDS_TEST, None)
            if test != DISP_CHANGE_SUCCESSFUL:
                return False, f"Windows отклонила режим {width}×{height} (код {test})"
            result = _user32.ChangeDisplaySettingsExW(None, ctypes.byref(mode), None, 0, None)
            if result != DISP_CHANGE_SUCCESSFUL:
                return False, f"Не удалось сменить разрешение Windows (код {result})"
            return True, None

    def set_best(self, target_width, target_height):
        chosen = best_mode(self.modes(), int(target_width), int(target_height))
        if not chosen:
            return False, None, "Поддерживаемые режимы дисплея не найдены"
        ok, error = self.set_mode(chosen["width"], chosen["height"], chosen.get("refresh", 0))
        return ok, chosen if ok else None, error

    def restore(self):
        original = self._original
        if not original:
            return False
        ok, _ = self.set_mode(original["width"], original["height"], original.get("refresh", 0))
        return ok
