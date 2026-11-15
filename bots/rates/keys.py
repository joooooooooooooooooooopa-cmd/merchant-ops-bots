"""Одноразовые ключи доступа: админ генерит ключ → юзер вводит один раз → авторизован.
Хранилище — JSON (rate_keys.json)."""
import os
import json
import time
import secrets
import threading

KEYS_FILE = os.getenv("KEYS_FILE", "rate_keys.json")

# Сериализация записи (на случай thread-pool / concurrent_updates).
_LOCK = threading.RLock()


def _load():
    try:
        with open(KEYS_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        return {"keys": {}, "users": {}}
    except (json.JSONDecodeError, ValueError):
        # Файл повреждён — НЕ теряем молча: сохраняем копию для восстановления.
        try:
            os.replace(KEYS_FILE, KEYS_FILE + ".corrupt")
        except OSError:
            pass
        return {"keys": {}, "users": {}}
    d.setdefault("keys", {})
    d.setdefault("users", {})
    return d


def _save(d):
    # Атомарная запись: temp в той же директории + fsync + os.replace.
    tmp = KEYS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, KEYS_FILE)


def generate_key(created_by, name=""):
    with _LOCK:
        d = _load()
        key = "KEY-" + secrets.token_hex(4).upper()
        d["keys"][key] = {"created_by": created_by, "created_at": time.time(),
                          "name": name, "used_by": None, "used_at": None}
        _save(d)
        return key


def validate_and_consume(key, user_id):
    """Одноразово: ключ срабатывает один раз, после — становится использованным."""
    with _LOCK:
        d = _load()
        rec = d["keys"].get(key)
        if not rec or rec.get("used_by"):
            return False
        rec["used_by"] = user_id
        rec["used_at"] = time.time()
        d["users"][str(user_id)] = {"authorized_at": time.time(), "via_key": key}
        _save(d)
        return True


def authorize_user(user_id, via="master"):
    with _LOCK:
        d = _load()
        d["users"][str(user_id)] = {"authorized_at": time.time(), "via_key": via}
        _save(d)


def is_authorized(user_id, admin_ids=()):
    if user_id in admin_ids:
        return True
    return str(user_id) in _load()["users"]


def list_keys():
    return _load()["keys"]


def list_users():
    return _load()["users"]


def revoke_key(key):
    with _LOCK:
        d = _load()
        if key in d["keys"]:
            del d["keys"][key]
            _save(d)
            return True
        return False


def revoke_user(user_id):
    with _LOCK:
        d = _load()
        uid = str(user_id)
        if uid in d["users"]:
            del d["users"][uid]
            _save(d)
            return True
        return False
