"""Общее состояние приложения: конфиг, журнал аудита, пути к данным."""
import json
import os
import threading
import time

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(APP_DIR, "data")
WEB_DIR = os.path.join(APP_DIR, "web")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
AUDIT_PATH = os.path.join(DATA_DIR, "audit.log")
SESSIONS_LOG_PATH = os.path.join(DATA_DIR, "sessions.log")

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "role": None,                 # "host" | "client"
    "host_name": None,            # отображаемое имя хоста
    "host_port": 8532,            # HTTP/WS порт хоста
    "discovery_port": 8533,       # UDP порт LAN-обнаружения
    "client_port": 8600,          # локальная панель клиента
    "accepting": True,            # принимать новые подключения
    "work_only_mode": False,      # экономный рабочий режим: качество/нагрузка ниже, ввод не блокируется
    "owner_gaming_mode": False,   # режим «игровая сессия владельца»: агрессивный резерв ресурсов
    "owner_reserve_percent": 25,  # резерв CPU/ресурсов владельцу хоста
    "max_sessions": 4,
}


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_config():
    ensure_data_dir()
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg):
    ensure_data_dir()
    with _lock:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)


def audit(action, actor="system", details=None):
    """Журнал действий администратора и событий безопасности (JSONL)."""
    ensure_data_dir()
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "actor": actor,
        "action": action,
        "details": details or {},
    }
    with _lock:
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def audit_tail(n=200):
    try:
        with open(AUDIT_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except FileNotFoundError:
        return []


def log_session_event(event, session):
    """История подключений (JSONL)."""
    ensure_data_dir()
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
    rec.update(session)
    with _lock:
        with open(SESSIONS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def session_history(n=200):
    try:
        with open(SESSIONS_LOG_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except FileNotFoundError:
        return []
