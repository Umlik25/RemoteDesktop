"""Аппаратный H.264-поток экрана Windows через FFmpeg/NVENC.

FFmpeg захватывает Desktop Duplication и передаёт D3D11-кадры в NVENC без
копирования полного BGRA-кадра через Python. Результат — фрагментированный MP4,
который браузер декодирует аппаратно через Media Source Extensions.
"""
import os
import platform
import shutil
import subprocess


H264_MIME = 'video/mp4; codecs="avc1.640033"'


def unavailable(reason="Аппаратный H.264 ещё не проверен"):
    return {
        "available": False,
        "codec": "h264",
        "api": "NVENC",
        "transport": "fmp4-mse",
        "mime": H264_MIME,
        "executable": None,
        "reason": reason,
    }


def _ffmpeg_executables():
    candidates = []
    configured = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if configured and os.path.isfile(configured):
        candidates.append(configured)
    system = shutil.which("ffmpeg")
    if system:
        candidates.append(system)
    try:
        import imageio_ffmpeg
        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    return list(dict.fromkeys(path for path in candidates if path))


def _listing(executable, flag):
    try:
        result = subprocess.run(
            [executable, "-hide_banner", flag], capture_output=True,
            text=True, timeout=10, creationflags=(0x08000000 if os.name == "nt" else 0))
        return (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def detect():
    if platform.system() != "Windows":
        return unavailable("NVENC-захват рабочего стола доступен на Windows-хосте")
    executables = _ffmpeg_executables()
    if not executables:
        return unavailable("FFmpeg не найден; установите зависимости проекта заново")
    last_missing = []
    for executable in executables:
        encoders = _listing(executable, "-encoders")
        filters = _listing(executable, "-filters")
        missing = []
        if "h264_nvenc" not in encoders:
            missing.append("h264_nvenc")
        if "ddagrab" not in filters:
            missing.append("ddagrab")
        if not missing:
            return {
                "available": True,
                "codec": "h264",
                "api": "NVENC",
                "transport": "fmp4-mse",
                "mime": H264_MIME,
                "executable": executable,
                "reason": None,
            }
        last_missing = missing
    return unavailable(
        "Доступные сборки FFmpeg не поддерживают " + ", ".join(last_missing))


def target_bitrate_mbps(width, height, fps, quality, safe_network_mbps=0):
    """Низколатентный CBR-бюджет, масштабируемый по пикселям и FPS."""
    megapixels_per_second = max(1, int(width)) * max(1, int(height)) * max(1, int(fps)) / 1e6
    bits_per_pixel = 0.055 + max(20, min(95, int(quality))) * 0.00115
    target = megapixels_per_second * bits_per_pixel
    if safe_network_mbps:
        target = min(target, float(safe_network_mbps) * 0.8)
    return max(3, min(200, int(round(target))))


def command(capabilities, fps, quality, width, height, safe_network_mbps=0):
    executable = capabilities.get("executable")
    if not capabilities.get("available") or not executable:
        raise ValueError(capabilities.get("reason") or "Аппаратный H.264 недоступен")
    fps = max(1, min(240, int(fps)))
    bitrate = target_bitrate_mbps(
        width, height, fps, quality, safe_network_mbps)
    # frag_every_frame не ждёт GOP перед передачей сегмента. B-кадры и lookahead
    # выключены: задержка важнее небольшой экономии битрейта.
    return [
        executable, "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "lavfi", "-i", f"ddagrab=framerate={fps}:draw_mouse=0",
        "-an", "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull",
        "-rc", "cbr", "-b:v", f"{bitrate}M", "-maxrate", f"{bitrate}M",
        "-bufsize", f"{max(2, bitrate // 2)}M", "-g", str(fps),
        "-keyint_min", str(fps), "-bf", "0", "-profile:v", "high",
        "-movflags", "+frag_every_frame+empty_moov+default_base_moof+omit_tfhd_offset",
        "-flush_packets", "1", "-f", "mp4", "pipe:1",
    ]
