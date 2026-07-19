"""Определение железа, загрузки и возможностей кодирования.

Всё определяется по принципу «лучшее из доступного»: если данные получить
нельзя (нет nvidia-smi, нет прав и т.п.) — поле помечается как unknown,
а не выдумывается.
"""
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time

import psutil

IS_WIN = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

_static_cache = None
_bench_cache = None
_net_prev = None  # (ts, bytes_sent, bytes_recv) для расчёта скорости
_gpu_load_cache = (0.0, None)

_VIRTUAL_NIC_MARKERS = (
    "loopback", "pseudo", "radmin", "hamachi", "tailscale", "zerotier",
    "wireguard", "openvpn", "vpn", "tunnel", "tap", "tun", "virtual",
    "hyper-v", "vmware", "vbox", "bluetooth",
)

_VM_MARKERS = (
    "virtual machine", "vmware", "virtualbox", "kvm", "qemu", "xen",
    "parallels", "bhyve", "hvm domu", "bochs", "hyper-v", "openstack",
    "amazon ec2", "google compute engine", "digitalocean",
)

_HYPERVISOR_NAMES = (
    ("vmware", "VMware"),
    ("virtualbox", "VirtualBox"),
    ("microsoft corporation virtual machine", "Hyper-V"),
    ("hyper-v", "Hyper-V"),
    ("kvm", "KVM"),
    ("qemu", "QEMU/KVM"),
    ("xen", "Xen"),
    ("parallels", "Parallels"),
    ("bhyve", "bhyve"),
)


def _run(cmd, timeout=10):
    try:
        kw = {}
        if IS_WIN:
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return out.stdout.strip()
    except Exception:
        return ""


def _ps(command):
    return _run(["powershell", "-NoProfile", "-Command", command], timeout=20)


def _nvidia_smi(query):
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"])
    return out or None


def classify_machine_environment(*values):
    """Classify a machine from vendor/model strings without guessing from CPU flags."""
    description = " ".join(str(value or "") for value in values).strip()
    lowered = description.lower()
    is_vm = any(marker in lowered for marker in _VM_MARKERS)
    hypervisor = None
    if is_vm:
        for marker, name in _HYPERVISOR_NAMES:
            if marker in lowered:
                hypervisor = name
                break
    return {"kind": "vm" if is_vm else "physical",
            "hypervisor": hypervisor, "description": description[:300]}


def get_machine_environment():
    """Detect whether this agent runs on bare metal or inside a VM."""
    values = []
    source = "platform"
    if IS_WIN:
        raw = _ps("Get-CimInstance Win32_ComputerSystem | "
                  "Select-Object Manufacturer,Model | ConvertTo-Json -Compress")
        try:
            data = json.loads(raw) if raw else {}
            values.extend((data.get("Manufacturer"), data.get("Model")))
            source = "Win32_ComputerSystem"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    elif IS_LINUX:
        virt = _run(["systemd-detect-virt", "--vm"]) if shutil.which("systemd-detect-virt") else ""
        if virt and virt != "none":
            values.append(virt)
            source = "systemd-detect-virt"
        for path in ("/sys/class/dmi/id/sys_vendor", "/sys/class/dmi/id/product_name"):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    values.append(f.read().strip())
            except OSError:
                pass
    elif IS_MAC:
        values.append(_run(["sysctl", "-n", "hw.model"]))
    values.extend((platform.system(), platform.release(), platform.machine()))
    result = classify_machine_environment(*values)
    result["source"] = source
    return result


def get_gpus():
    gpus = []
    if IS_WIN:
        raw = _ps("Get-CimInstance Win32_VideoController | "
                  "Select-Object Name,AdapterRAM,DriverVersion | ConvertTo-Json")
        try:
            data = json.loads(raw) if raw else []
            if isinstance(data, dict):
                data = [data]
            for g in data:
                vram = g.get("AdapterRAM") or 0
                gpus.append({"name": g.get("Name", "Unknown GPU"),
                             "vram_gb": round(vram / 2**30, 1) if vram else None,
                             "driver": g.get("DriverVersion")})
        except (json.JSONDecodeError, TypeError):
            pass
    elif IS_LINUX:
        out = _run(["sh", "-c", "lspci | grep -Ei 'vga|3d|display'"])
        for line in out.splitlines():
            gpus.append({"name": line.split(":", 2)[-1].strip(), "vram_gb": None, "driver": None})
    elif IS_MAC:
        out = _run(["system_profiler", "SPDisplaysDataType", "-json"])
        try:
            for g in json.loads(out).get("SPDisplaysDataType", []):
                gpus.append({"name": g.get("sppci_model", "Apple GPU"), "vram_gb": None, "driver": None})
        except (json.JSONDecodeError, TypeError):
            pass
    # Точный VRAM/энкодеры от nvidia-smi, если есть NVIDIA
    smi = _nvidia_smi("name,memory.total")
    if smi:
        for i, line in enumerate(smi.splitlines()):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                for g in gpus:
                    if parts[0] in g["name"] or g["name"] in parts[0]:
                        g["vram_gb"] = round(float(parts[1]) / 1024, 1)
                        g["nvidia"] = True
    return gpus or [{"name": "GPU не определена", "vram_gb": None, "driver": None}]


def detect_encoders(gpus):
    """Эвристика по вендору/поколению. confidence: high/medium/low."""
    enc = {"h264": False, "hevc": False, "av1": False, "apis": [], "hw_encoders": 0,
           "confidence": "low", "notes": []}
    for g in gpus:
        name = (g.get("name") or "").lower()
        if "nvidia" in name or "geforce" in name or "quadro" in name or "rtx" in name or "gtx" in name:
            enc["apis"].append("NVENC")
            enc["h264"] = enc["hevc"] = True
            enc["hw_encoders"] += 1
            enc["confidence"] = "medium"
            # AV1-энкодер начиная с Ada (RTX 40xx/50xx)
            if any(s in name for s in ("rtx 40", "rtx 50", "rtx 41", "rtx 45")):
                enc["av1"] = True
            else:
                enc["notes"].append("AV1-кодирование NVENC доступно только на RTX 40-й серии и новее")
        elif "amd" in name or "radeon" in name:
            enc["apis"].append("AMF")
            enc["h264"] = enc["hevc"] = True
            enc["hw_encoders"] += 1
            enc["confidence"] = "medium"
            if "rx 7" in name or "rx 9" in name:
                enc["av1"] = True
        elif "intel" in name and ("uhd" in name or "iris" in name or "arc" in name or "hd graphics" in name):
            enc["apis"].append("QuickSync")
            enc["h264"] = enc["hevc"] = True
            enc["hw_encoders"] += 1
            enc["confidence"] = "medium"
            if "arc" in name:
                enc["av1"] = True
        elif "apple" in name or IS_MAC:
            enc["apis"].append("VideoToolbox")
            enc["h264"] = enc["hevc"] = True
            enc["hw_encoders"] += 1
            enc["confidence"] = "medium"
    if not enc["apis"]:
        enc["notes"].append("Аппаратный энкодер не обнаружен — доступно только CPU-кодирование")
    enc["notes"].append("Определено эвристикой по модели GPU; точная проверка — пробным кодированием (в дорожной карте)")
    return enc


def get_virtualization():
    v = {"cpu_virt": None, "iommu": None, "kvm": None, "hyperv": None,
         "gpu_passthrough": "unknown", "vgpu": "unknown", "notes": []}
    if IS_WIN:
        raw = _ps("Get-CimInstance Win32_Processor | "
                  "Select-Object VirtualizationFirmwareEnabled,SecondLevelAddressTranslationExtensions | ConvertTo-Json")
        try:
            d = json.loads(raw)
            if isinstance(d, list):
                d = d[0]
            v["cpu_virt"] = bool(d.get("VirtualizationFirmwareEnabled"))
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        hv = _ps("(Get-CimInstance Win32_ComputerSystem).HypervisorPresent")
        v["hyperv"] = hv.strip().lower() == "true" if hv else None
        v["notes"].append("IOMMU/VT-d на Windows надёжно определяется только из BIOS/msinfo32 — проверьте вручную")
        v["notes"].append("GPU passthrough на Windows-хосте ограничен (DDA только в Windows Server); "
                          "для мульти-GPU-виртуализации рекомендуется Linux/KVM")
    elif IS_LINUX:
        v["kvm"] = os.path.exists("/dev/kvm")
        try:
            v["iommu"] = bool(os.listdir("/sys/class/iommu"))
        except OSError:
            v["iommu"] = False
        try:
            with open("/proc/cpuinfo") as f:
                flags = f.read()
            v["cpu_virt"] = ("vmx" in flags) or ("svm" in flags)
        except OSError:
            pass
        if v["iommu"] and v["kvm"]:
            v["gpu_passthrough"] = "likely"
        v["notes"].append("vGPU (SR-IOV/MIG/GRID) требует поддержки конкретной GPU и драйверов вендора")
    elif IS_MAC:
        v["cpu_virt"] = True  # Hypervisor.framework
        v["gpu_passthrough"] = "no"
        v["notes"].append("macOS: только Virtualization.framework, GPU passthrough недоступен; "
                          "хостинг macOS-гостей юридически ограничен железом Apple")
    return v


def get_static_info():
    global _static_cache
    if _static_cache:
        return _static_cache
    cpu_name = platform.processor() or ""
    cores = psutil.cpu_count(logical=False) or 0
    threads = psutil.cpu_count(logical=True) or 0
    if IS_WIN:
        raw = _ps("Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors | ConvertTo-Json")
        try:
            d = json.loads(raw)
            if isinstance(d, list):
                d = d[0]
            cpu_name = d.get("Name", cpu_name).strip()
            cores = d.get("NumberOfCores", cores)
            threads = d.get("NumberOfLogicalProcessors", threads)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    elif IS_LINUX:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        cpu_name = line.split(":", 1)[1].strip()
                        break
        except OSError:
            pass
    elif IS_MAC:
        cpu_name = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or cpu_name

    mem = psutil.virtual_memory()
    disks = []
    for p in psutil.disk_partitions(all=False):
        try:
            du = psutil.disk_usage(p.mountpoint)
            disks.append({"mount": p.mountpoint, "total_gb": round(du.total / 2**30),
                          "free_gb": round(du.free / 2**30)})
        except OSError:
            continue

    nics = []
    addresses = psutil.net_if_addrs()
    for name, st in psutil.net_if_stats().items():
        if st.isup and st.speed and st.speed > 0:
            ipv4 = [a.address for a in addresses.get(name, [])
                    if a.family == socket.AF_INET]
            lowered = name.lower()
            virtual = any(marker in lowered for marker in _VIRTUAL_NIC_MARKERS)
            if ipv4 and all(ip.startswith(("127.", "25.", "26.", "169.254."))
                            for ip in ipv4):
                virtual = True
            nics.append({"name": name, "speed_mbps": st.speed,
                         "addresses": ipv4, "virtual": virtual})

    gpus = get_gpus()
    _static_cache = {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu": cpu_name,
        "cores": cores,
        "threads": threads,
        "ram_gb": round(mem.total / 2**30, 1),
        "gpus": gpus,
        "disks": disks,
        "nics": nics,
        "encoders": detect_encoders(gpus),
        "virtualization": get_virtualization(),
        "environment": get_machine_environment(),
    }
    return _static_cache


def get_load():
    global _net_prev, _gpu_load_cache
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    now = time.time()
    io = psutil.net_io_counters()
    up_mbps = down_mbps = 0.0
    if _net_prev:
        dt = max(now - _net_prev[0], 0.001)
        up_mbps = (io.bytes_sent - _net_prev[1]) * 8 / dt / 1e6
        down_mbps = (io.bytes_recv - _net_prev[2]) * 8 / dt / 1e6
    _net_prev = (now, io.bytes_sent, io.bytes_recv)

    disk_pct = None
    try:
        root = "C:\\" if IS_WIN else "/"
        du = psutil.disk_usage(root)
        disk_pct = du.percent
    except OSError:
        pass

    load = {
        "cpu_percent": cpu,
        "ram_percent": mem.percent,
        "ram_used_gb": round(mem.used / 2**30, 1),
        "disk_percent": disk_pct,
        "net_up_mbps": round(up_mbps, 1),
        "net_down_mbps": round(down_mbps, 1),
        "gpu_percent": None, "vram_percent": None,
        "gpu_temp": None, "gpu_power_w": None,
        "encoder_percent": None,
    }
    # Запуск nvidia-smi может занимать сотни миллисекунд и сам создавать
    # микрофризы. Метрики GPU не требуют частоты видеопотока, кэшируем их.
    smi_ts, smi = _gpu_load_cache
    mono = time.monotonic()
    if mono - smi_ts >= 5.0:
        smi = _nvidia_smi("utilization.gpu,utilization.memory,memory.used,memory.total,temperature.gpu,power.draw,utilization.encoder")
        _gpu_load_cache = (mono, smi)
    if smi:
        try:
            parts = [p.strip() for p in smi.splitlines()[0].split(",")]
            load["gpu_percent"] = float(parts[0])
            mu, mt = float(parts[2]), float(parts[3])
            load["vram_percent"] = round(mu / mt * 100, 1) if mt else None
            load["gpu_temp"] = float(parts[4])
            try:
                load["gpu_power_w"] = float(parts[5])
            except ValueError:
                pass
            if len(parts) > 6:
                try:
                    load["encoder_percent"] = float(parts[6])
                except ValueError:
                    pass
        except (ValueError, IndexError):
            pass
    return load


def quick_benchmark():
    """Короткий CPU-бенчмарк (~1 c): PBKDF2-итерации, нормированные к эталону.
    score 1.0 ≈ один поток современного десктопного ядра (~2021)."""
    global _bench_cache
    if _bench_cache:
        return _bench_cache
    import hashlib
    t0 = time.perf_counter()
    n = 0
    while time.perf_counter() - t0 < 0.6:
        hashlib.pbkdf2_hmac("sha256", b"bench", b"salt0123", 60_000)
        n += 1
    dt = time.perf_counter() - t0
    single = (n / dt) / 14.0  # ~14 итераций/с — условный эталон
    threads = psutil.cpu_count(logical=True) or 1
    _bench_cache = {
        "single_score": round(single, 2),
        "multi_score_est": round(single * threads * 0.7, 1),  # 0.7 — потери на SMT/turbo
        "note": "Короткий синтетический тест; точность средняя",
    }
    return _bench_cache
