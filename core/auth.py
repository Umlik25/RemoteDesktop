"""Пользователи, роли, токены, приглашения.

Пароли: PBKDF2-HMAC-SHA256, 200 000 итераций, соль на пользователя.
Токены сессий: случайные, живут в памяти процесса (сбрасываются при рестарте).
"""
import hashlib
import json
import os
import secrets
import threading
import time

from . import state

USERS_PATH = os.path.join(state.DATA_DIR, "users.json")
INVITES_PATH = os.path.join(state.DATA_DIR, "invites.json")

ROLES = ("owner", "admin", "user", "guest")
PROFILES = ("office", "dev", "design", "video", "game", "competitive", "custom")

_lock = threading.Lock()
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
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


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
            "created": u.get("created"),
        })
    return out


def create_user(username, password, role="user", profile="office", *, priority="normal",
                allow_input=True, allow_clipboard=False, allow_files=False,
                max_fps=60, expires=None, actor="system"):
    if role not in ROLES:
        raise ValueError("bad role")
    if not username or not password or len(password) < 4:
        raise ValueError("Имя пользователя и пароль (мин. 4 символа) обязательны")
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
            "blocked": False, "expires": expires,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save(USERS_PATH, users)
    state.audit("user.create", actor, {"username": username, "role": role})


def update_user(username, fields, actor="system"):
    allowed = {"role", "profile", "priority", "allow_input", "allow_clipboard",
               "allow_files", "max_fps", "blocked", "expires"}
    with _lock:
        users = _load(USERS_PATH)
        if username not in users:
            raise ValueError("Нет такого пользователя")
        for k, v in fields.items():
            if k in allowed:
                users[username][k] = v
        _save(USERS_PATH, users)
    state.audit("user.update", actor, {"username": username, "fields": fields})


def delete_user(username, actor="system"):
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
    if not new_password or len(new_password) < 4:
        raise ValueError("Пароль должен быть не короче 4 символов")
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
    return _load(USERS_PATH).get(username)


def has_users():
    return bool(_load(USERS_PATH))


def verify(username, password):
    u = get_user(username)
    if not u:
        return None
    if u.get("blocked"):
        return None
    exp = u.get("expires")
    if exp and time.time() > exp:
        return None
    if secrets.compare_digest(_hash_pw(password, u["salt"]), u["pw"]):
        return u
    return None


def issue_token(username):
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
    if not u or u.get("blocked"):
        return None, None
    return username, u


def revoke_user_tokens(username):
    with _lock:
        for t in [t for t, v in _tokens.items() if v["username"] == username]:
            _tokens.pop(t, None)


# ---- Приглашения (одноразовый код) ----

def create_invite(role="guest", profile="office", ttl_hours=24, priority="low",
                  allow_input=True, session_hours=None, actor="system"):
    code = secrets.token_urlsafe(8)
    with _lock:
        inv = _load(INVITES_PATH)
        inv[code] = {
            "role": role, "profile": profile, "priority": priority,
            "allow_input": allow_input,
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


def redeem_invite(code, username, password):
    """Одноразовый код -> создание учётной записи с правами из приглашения."""
    with _lock:
        inv = _load(INVITES_PATH)
        rec = inv.get(code)
        if not rec or rec.get("used") or time.time() > rec["expires_at"]:
            raise ValueError("Код недействителен или истёк")
        rec["used"] = True
        _save(INVITES_PATH, inv)
    expires = None
    if rec.get("session_hours"):
        expires = time.time() + rec["session_hours"] * 3600
    create_user(username, password, role=rec["role"], profile=rec["profile"],
                priority=rec.get("priority", "low"),
                allow_input=rec.get("allow_input", True),
                expires=expires, actor=f"invite:{code}")
