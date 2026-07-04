"""Host Capability Report и Capacity Planner.

Все цифры — прогноз, а не гарантия. Каждый уровень и каждая оценка
сопровождаются полем confidence и причинами.
"""

PROFILES = {
    "office":      {"title": "Офис",                 "ram_gb": 2.0, "cpu_score": 1.0, "mbps": 8,  "needs_gpu": False},
    "dev":         {"title": "Разработка",           "ram_gb": 4.0, "cpu_score": 2.5, "mbps": 10, "needs_gpu": False},
    "design":      {"title": "Дизайн / графика",     "ram_gb": 6.0, "cpu_score": 3.0, "mbps": 25, "needs_gpu": True},
    "video":       {"title": "Видео",                "ram_gb": 6.0, "cpu_score": 3.5, "mbps": 30, "needs_gpu": True},
    "game":        {"title": "Игры",                 "ram_gb": 8.0, "cpu_score": 4.0, "mbps": 40, "needs_gpu": True},
    "competitive": {"title": "Соревновательные игры","ram_gb": 8.0, "cpu_score": 5.0, "mbps": 50, "needs_gpu": True},
}

# Профили качества потока по умолчанию (клиент может переопределить)
STREAM_PROFILES = {
    "office":      {"fps": 30,  "quality": 55, "scale": 1.0,  "label": "Офис"},
    "dev":         {"fps": 30,  "quality": 70, "scale": 1.0,  "label": "Разработка"},
    "design":      {"fps": 60,  "quality": 80, "scale": 1.0,  "label": "Дизайн"},
    "video":       {"fps": 60,  "quality": 75, "scale": 1.0,  "label": "Видео"},
    "game":        {"fps": 60,  "quality": 70, "scale": 1.0,  "label": "Игры"},
    "competitive": {"fps": 120, "quality": 60, "scale": 0.75, "label": "Соревновательные"},
    "custom":      {"fps": 60,  "quality": 70, "scale": 1.0,  "label": "Пользовательский"},
}


def _gpu_class(static):
    """Грубая классификация лучшей GPU: none/basic/mid/high."""
    best = "none"
    for g in static.get("gpus", []):
        name = (g.get("name") or "").lower()
        vram = g.get("vram_gb") or 0
        if any(s in name for s in ("rtx 40", "rtx 50", "rx 79", "rx 9")) or vram >= 16:
            return "high"
        if any(s in name for s in ("rtx", "gtx 16", "rx 6", "rx 7", "arc")) or vram >= 8:
            best = "mid"
        elif any(s in name for s in ("uhd", "iris", "vega", "radeon graphics", "hd graphics", "apple")):
            if best == "none":
                best = "basic"
    return best


def build_report(static, bench, load, config):
    enc = static["encoders"]
    virt = static["virtualization"]
    gpu_class = _gpu_class(static)
    ram = static["ram_gb"]
    threads = static["threads"]
    lan_mbps = max((n["speed_mbps"] for n in static.get("nics", [])), default=0)

    levels = {}

    def level(key, available, reasons, limits, gpu_mode, confidence):
        levels[key] = {"available": available, "reasons": reasons,
                       "limitations": limits, "gpu_mode": gpu_mode,
                       "confidence": confidence}

    # Remote Work Ready
    ok = ram >= 8 and threads >= 4
    level("remote_work_ready", ok,
          [f"RAM {ram} ГБ, потоков CPU: {threads}"] +
          (["Достаточно для 1+ офисных сессий"] if ok else ["Мало RAM или ядер CPU"]),
          ["Кодирование в MVP программное (MJPEG); H.264/HEVC — в дорожной карте"],
          "без GPU-виртуализации (захват рабочего стола)", "high")

    # Creative Ready (порог 15 ГБ: системы «16 ГБ» репортуют ~15.8)
    ok = ram >= 15 and gpu_class in ("mid", "high") and (enc["h264"] or enc["hevc"])
    level("creative_ready", ok,
          [f"GPU класс: {gpu_class}, аппаратные энкодеры: {', '.join(enc['apis']) or 'нет'}"],
          ["Цветовая точность зависит от кодека и битрейта; HDR требует HEVC/AV1 и поддержки клиента"],
          "разделение GPU по времени (без изоляции VRAM)", enc["confidence"])

    # Gaming Ready
    ok = gpu_class in ("mid", "high") and ram >= 15 and enc["hw_encoders"] >= 1
    level("gaming_ready", ok,
          [f"GPU класс: {gpu_class}", f"Аппаратных энкодеров: {enc['hw_encoders']}"],
          ["Задержка зависит от сети и кодека",
           "Часть игр с античитами/DRM может не работать удалённо или в VM"],
          "GPU хоста напрямую, один игровой поток", enc["confidence"])

    # Multi-Gaming Ready — честно: требует passthrough/vGPU
    pass_ok = virt.get("gpu_passthrough") == "likely"
    multi_gpu = len([g for g in static.get("gpus", []) if (g.get("vram_gb") or 0) >= 6]) >= 2
    ok = (pass_ok and multi_gpu) or virt.get("vgpu") == "yes"
    reasons = []
    if multi_gpu:
        reasons.append("Обнаружено 2+ дискретных GPU — возможен passthrough по одной на VM")
    if not pass_ok:
        reasons.append("IOMMU/passthrough не подтверждён на этой ОС" +
                       (" (Windows-хост: DDA только в Windows Server; рекомендуется Linux/KVM)" if "Windows" in static["os"] else ""))
    level("multi_gaming_ready", ok, reasons or ["Нет подтверждённого vGPU/passthrough"],
          ["Несколько игровых пользователей на ОДНОЙ GPU требуют vGPU (лицензии NVIDIA GRID) или SR-IOV",
           "На потребительских GPU официального vGPU нет"],
          "GPU passthrough (по одной GPU на VM)" if multi_gpu else "недоступен",
          "low" if not ok else "medium")

    return {
        "levels": levels,
        "gpu_class": gpu_class,
        "lan_mbps": lan_mbps,
        "warning": "Это оценка возможностей, а не гарантия производительности. "
                   "Фактическое качество зависит от сети, кодека и одновременной нагрузки.",
    }


def capacity_plan(static, bench, load, config):
    """Прогноз числа одновременных сессий по профилям + узкое место."""
    ram_total = static["ram_gb"]
    reserve = config.get("owner_reserve_percent", 25) / 100
    ram_avail = ram_total * (1 - reserve) - 2  # 2 ГБ на ОС
    cpu_score = bench["multi_score_est"] * (1 - reserve)
    lan_mbps = max((n["speed_mbps"] for n in static.get("nics", [])), default=100)
    gpu_class = _gpu_class(static)
    enc_slots = {"none": 0, "basic": 1, "mid": 2, "high": 3}[gpu_class] + 2  # MJPEG кодируется CPU — слоты условные

    # поправка на текущую загрузку
    cpu_free = max(0.1, 1 - load["cpu_percent"] / 100)
    ram_free_gb = max(0.5, ram_total * (1 - load["ram_percent"] / 100) - 1)

    out = {}
    for key, p in PROFILES.items():
        by_ram = ram_avail / p["ram_gb"]
        by_cpu = cpu_score / p["cpu_score"]
        by_net = (lan_mbps * 0.7) / p["mbps"]
        cands = {"RAM": by_ram, "CPU": by_cpu, "Сеть": by_net}
        if p["needs_gpu"]:
            gpu_cap = {"none": 0, "basic": 0.5, "mid": 2, "high": 4}[gpu_class]
            if key in ("game", "competitive"):
                gpu_cap = {"none": 0, "basic": 0, "mid": 1, "high": 2}[gpu_class]
            cands["GPU"] = gpu_cap
            cands["Энкодер"] = enc_slots
        bottleneck = min(cands, key=cands.get)
        recommended = max(0, int(min(cands.values())))
        cur_by_cpu = (bench["multi_score_est"] * cpu_free) / p["cpu_score"]
        cur_by_ram = ram_free_gb / p["ram_gb"]
        current = max(0, int(min(recommended, cur_by_cpu, cur_by_ram)))
        conf = "medium" if not p["needs_gpu"] else ("low" if gpu_class in ("none", "basic") else "medium")
        quality = ("хорошее" if recommended >= 1 and bottleneck not in ("GPU", "Энкодер")
                   else "ограниченное")
        out[key] = {
            "title": p["title"],
            "recommended": recommended,
            "current_available": current,
            "bottleneck": bottleneck,
            "expected_quality": quality,
            "confidence": conf,
            "warning": ("Превышение рекомендуемого числа сессий приведёт к предсказуемой "
                        "деградации: битрейт → FPS → разрешение → пауза сессии"),
        }
    return out
