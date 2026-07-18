"""Пользователи, роли, токены, приглашения.

Пароли: PBKDF2-HMAC-SHA256, 200 000 итераций, соль на пользователя.
Токены сессий: случайные, живут в памяти процесса (сбрасываются при рестарте).
"""
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time

from . import state

USERS_PATH = os.path.join(state.DATA_DIR, "users.json")
INVITES_PATH = os.path.join(state.DATA_DIR, "invites.json")
RESET_PATH = os.path.join(state.DATA_DIR, "reset_codes.json")

ROLES = ("owner", "admin", "user", "guest")
PROFILES = ("office", "dev", "design", "video", "game", "competitive", "custom")
PRIORITIES = ("low", "normal", "high", "critical")
PASSWORD_MIN_LENGTH = 8
USERNAME_RE = re.compile(r"^[\w.-]{1,64}$", re.UNICODE)

_lock = threading.RLock()
_tokens = {}  # token -> {"username":..., "created":..., "expires":...}

TOKEN_TTL = 12 * 3600


def _load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(path, data):
    state.ensure_data_dir()
    fd, tmp_path = tempfile.mkstemp(prefix=".app_remote_", suffix=".tmp",
                                    dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass


def _hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


def _clean_username(username):
    return username.strip() if isinstance(username, str) else ""


def _validate_username(username):
    username = _clean_username(username)
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("Имя: 1–64 символа; разрешены буквы, цифры, точка, дефис и подчёркивание")
    return username


def _validate_password(password):
    if not isinstance(password, str) or len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Пароль должен быть не короче {PASSWORD_MIN_LENGTH} символов")


def _validate_policy(role, profile, priority, max_fps):
    if role not in ROLES:
        raise ValueError("Недопустимая роль")
    if profile not in PROFILES:
        raise ValueError("Недопустимый профиль")
    if priority not in PRIORITIES:
        raise ValueError("Недопустимый приоритет")
    try:
        max_fps = int(max_fps)
    except (TypeError, ValueError):
        raise ValueError("Максимальный FPS должен быть числом") from None
    if not 1 <= max_fps <= 240:
        raise ValueError("Максимальный FPS должен быть от 1 до 240")
    return max_fps


def user_is_active(user):
    if not user or user.get("blocked"):
        return False
    expires = user.get("expires")
    return not expires or time.time() <= expires


def list_users():
    users = _load(USERS_PATH)
    out = []
    for name, u in users.items():
        out.append({
            "username": name,
            "role": u.get("role", "user"),
            "blocked": u.get("blocked", False),
            "profile": u.get("profile", "office"),
            "allow_input": u.get("allow_input", True),
            "allow_clipboard": u.get("allow_clipboard", False),
            "allow_files": u.get("allow_files", False),
            "expires": u.get("expires"),
            "priority": u.get("priority", "normal"),
            "max_fps": u.get("max_fps", 60),
            "disk_quota_mb": u.get("disk_quota_mb", 2048),
            "created": u.get("created"),
        })
    return out


def create_user(username, password, role="user", profile="office", *, priority="normal",
                allow_input=True, allow_clipboard=False, allow_files=False,
                max_fps=60, disk_quota_mb=2048, expires=None, actor="system"):
    username = _validate_username(username)
    _validate_password(password)
    max_fps = _validate_policy(role, profile, priority, max_fps)
    try:
        disk_quota_mb = int(disk_quota_mb)
    except (TypeError, ValueError):
        raise ValueError("Квота файлов должна быть числом") from None
    if not 16 <= disk_quota_mb <= 102_400:
        raise ValueError("Квота файлов должна быть от 16 до 102400 МБ")
    with _lock:
        users = _load(USERS_PATH)
        if username in users:
            raise ValueError("Пользователь уже существует")
        salt = secrets.token_hex(16)
        users[username] = {
            "salt": salt, "pw": _hash_pw(password, salt), "role": role,
            "profile": profile, "priority": priority,
            "allow_input": allow_input, "allow_clipboard": allow_clipboard,
            "allow_files": allow_files, "max_fps": max_fps,
            "disk_quota_mb": disk_quota_mb,
            "blocked": False, "expires": expires,
            "storage_id": secrets.token_hex(12),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save(USERS_PATH, users)
    state.audit("user.create", actor, {"username": username, "role": role})


def update_user(username, fields, actor="system"):
    allowed = {"role", "profile", "priority", "allow_input", "allow_clipboard",
               "allow_files", "max_fps", "disk_quota_mb", "blocked", "expires"}
    username = _clean_username(username)
    if not isinstance(fields, dict):
        raise ValueError("Параметры пользователя должны быть объектом")
    fields = {k: v for k, v in fields.items() if k in allowed}
    if "role" in fields and fields["role"] not in ROLES:
        raise ValueError("Недопустимая роль")
    if "profile" in fields and fields["profile"] not in PROFILES:
        raise ValueError("Недопустимый профиль")
    if "priority" in fields and fields["priority"] not in PRIORITIES:
        raise ValueError("Недопустимый приоритет")
    if "max_fps" in fields:
        try:
            fields["max_fps"] = int(fields["max_fps"])
        except (TypeError, ValueError):
            raise ValueError("Максимальный FPS должен быть числом") from None
        if not 1 <= fields["max_fps"] <= 240:
            raise ValueError("Максимальный FPS должен быть от 1 до 240")
    if "disk_quota_mb" in fields:
        try:
            fields["disk_quota_mb"] = int(fields["disk_quota_mb"])
        except (TypeError, ValueError):
            raise ValueError("Квота файлов должна быть числом") from None
        if not 16 <= fields["disk_quota_mb"] <= 102_400:
            raise ValueError("Квота файлов должна быть от 16 до 102400 МБ")
    for key in ("allow_input", "allow_clipboard", "allow_files", "blocked"):
        if key in fields and not isinstance(fields[key], bool):
            raise ValueError(f"Поле {key} должно быть логическим")
    if "expires" in fields and fields["expires"] is not None:
        try:
            fields["expires"] = float(fields["expires"])
        except (TypeError, ValueError):
            raise ValueError("Срок доступа задан неверно") from None
    with _lock:
        users = _load(USERS_PATH)
        if username not in users:
            raise ValueError("Нет такого пользователя")
        users[username].update(fields)
        _save(USERS_PATH, users)
    state.audit("user.update", actor, {"username": username, "fields": fields})


def delete_user(username, actor="system"):
    username = _clean_username(username)
    with _lock:
        users = _load(USERS_PATH)
        users.pop(username, None)
        _save(USERS_PATH, users)
        for t in [t for t, v in _tokens.items() if v["username"] == username]:
            _tokens.pop(t, None)
    state.audit("user.delete", actor, {"username": username})


def set_password(username, new_password, actor="system"):
    """Сменить пароль пользователю, не трогая остальные поля/пользователей.
    Читает и пишет users.json атомарно — запущенный сервер подхватит новый
    пароль сразу (он читает файл на каждом входе). Существующие токены
    отзываются, активные сессии не завершаются."""
    username = _clean_username(username)
    _validate_password(new_password)
    with _lock:
        users = _load(USERS_PATH)
        if username not in users:
            raise ValueError("Нет такого пользователя")
        salt = secrets.token_hex(16)
        users[username]["salt"] = salt
        users[username]["pw"] = _hash_pw(new_password, salt)
        users[username]["blocked"] = False
        _save(USERS_PATH, users)
        for t in [t for t, v in _tokens.items() if v["username"] == username]:
            _tokens.pop(t, None)
    state.audit("user.password_reset", actor, {"username": username})


def get_user(username):
    return _load(USERS_PATH).get(_clean_username(username))


def has_users():
    return bool(_load(USERS_PATH))


def verify(username, password):
    u = get_user(username)
    if not user_is_active(u):
        return None
    if secrets.compare_digest(_hash_pw(password, u["salt"]), u["pw"]):
        return u
    return None


def issue_token(username):
    username = _clean_username(username)
    if not user_is_active(get_user(username)):
        raise ValueError("Пользователь заблокирован или срок доступа истёк")
    token = secrets.token_urlsafe(32)
    with _lock:
        _tokens[token] = {"username": username, "created": time.time(),
                          "expires": time.time() + TOKEN_TTL}
    return token


def check_token(token):
    """Возвращает (username, user_record) или (None, None)."""
    with _lock:
        rec = _tokens.get(token)
        if not rec or time.time() > rec["expires"]:
            _tokens.pop(token, None)
            return None, None
        username = rec["username"]
    u = get_user(username)
    if not user_is_active(u):
        with _lock:
            _tokens.pop(token, None)
        return None, None
    return username, u


def revoke_user_tokens(username):
    with _lock:
        for t in [t for t, v in _tokens.items() if v["username"] == username]:
            _tokens.pop(t, None)


# ---- Приглашения (одноразовый код) ----

def create_invite(role="guest", profile="office", ttl_hours=24, priority="low",
                  allow_input=True, allow_clipboard=False, allow_files=False,
                  session_hours=None, disk_quota_mb=512, actor="system"):
    _validate_policy(role, profile, priority, 60)
    ttl_hours = float(ttl_hours)
    session_hours = float(session_hours) if session_hours is not None else None
    if not 0 < ttl_hours <= 24 * 365:
        raise ValueError("Срок действия кода должен быть от 1 часа до 365 дней")
    if session_hours is not None and not 0 < session_hours <= 24 * 365:
        raise ValueError("Срок доступа должен быть от 1 часа до 365 дней")
    try:
        disk_quota_mb = int(disk_quota_mb)
    except (TypeError, ValueError):
        raise ValueError("Квота файлов должна быть числом") from None
    if not 16 <= disk_quota_mb <= 102_400:
        raise ValueError("Квота файлов должна быть от 16 до 102400 МБ")
    code = secrets.token_urlsafe(8)
    with _lock:
        inv = _load(INVITES_PATH)
        inv[code] = {
            "role": role, "profile": profile, "priority": priority,
            "allow_input": allow_input, "allow_clipboard": allow_clipboard,
            "allow_files": allow_files,
            "disk_quota_mb": disk_quota_mb,
            "expires_at": time.time() + ttl_hours * 3600,
            "session_hours": session_hours,
            "used": False,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save(INVITES_PATH, inv)
    state.audit("invite.create", actor, {"code": code, "role": role, "ttl_hours": ttl_hours})
    return code


def list_invites():
    inv = _load(INVITES_PATH)
    now = time.time()
    return [{"code": c, **v, "expired": now > v["expires_at"]} for c, v in inv.items()]


def revoke_invite(code, actor="system"):
    with _lock:
        inv = _load(INVITES_PATH)
        inv.pop(code, None)
        _save(INVITES_PATH, inv)
    state.audit("invite.revoke", actor, {"code": code})


# ---- Коды сброса пароля (одноразовые) ----

def create_reset_code(username, ttl_hours=1.0, actor="system"):
    """Одноразовый код сброса пароля. Создаёт владелец/админ на хосте,
    пользователь вводит его на экране подключения клиента."""
    ttl_hours = float(ttl_hours)
    if not 1 / 60 <= ttl_hours <= 24:
        raise ValueError("Код сброса может действовать от 1 минуты до 24 часов")
    with _lock:
        users = _load(USERS_PATH)
        if username not in users:
            raise ValueError("Нет такого пользователя")
        now = time.time()
        codes = {c: v for c, v in _load(RESET_PATH).items()
                 if not v.get("used") and now < v.get("expires_at", 0)}
        code = secrets.token_urlsafe(6)
        codes[code] = {"username": username, "expires_at": now + ttl_hours * 3600,
                       "used": False, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save(RESET_PATH, codes)
    state.audit("reset_code.create", actor, {"username": username, "ttl_hours": ttl_hours})
    return code


def redeem_reset_code(code, new_password):
    """Погасить код: меняет пароль только этому пользователю, ничего не сбрасывая."""
    _validate_password(new_password)
    with _lock:
        codes = _load(RESET_PATH)
        rec = codes.get(code)
        if not rec or rec.get("used") or time.time() > rec["expires_at"]:
            raise ValueError("Код недействителен или истёк")
        set_password(rec["username"], new_password, actor=f"reset-code:{code[:4]}…")
        rec["used"] = True
        _save(RESET_PATH, codes)
    return rec["username"]


def redeem_invite(code, username, password):
    """Одноразовый код -> создание учётной записи с правами из приглашения."""
    username = _validate_username(username)
    _validate_password(password)
    with _lock:
        inv = _load(INVITES_PATH)
        rec = inv.get(code)
        if not rec or rec.get("used") or time.time() > rec["expires_at"]:
            raise ValueError("Код недействителен или истёк")
        expires = None
        if rec.get("session_hours"):
            expires = time.time() + rec["session_hours"] * 3600
        create_user(username, password, role=rec["role"], profile=rec["profile"],
                    priority=rec.get("priority", "low"),
                    allow_input=rec.get("allow_input", True),
                    allow_clipboard=rec.get("allow_clipboard", False),
                    allow_files=rec.get("allow_files", False),
                    disk_quota_mb=rec.get("disk_quota_mb", 512),
                    expires=expires, actor=f"invite:{code}")
        rec["used"] = True
        _save(INVITES_PATH, inv)
