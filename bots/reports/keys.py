"""
Доступ к боту.

Схема:
  • OWNER_ID — единственный, кто видит админ-кнопки и команды /newkey /keys /users /revoke.
  • Гости — ВЫДАННЫЙ им OWNER-ом одноразовый ключ формата KEY-XXXX. Ключ срабатывает
    один раз (помечается used_by + used_at), user_id попадает в allowlist навсегда.
  • Мастер-ключ ACCESS_KEY из config — bootstrap-fallback (на случай переезда),
    тоже добавляет user_id в allowlist. Тратится без ограничения попыток.

Состояние — JSON-файл `report_keys.json` рядом с ботом:
  {
    "users": [<user_id>, ...],      # кому открыт доступ
    "keys":  {
        "KEY-XXXX": {
            "created_at": "ISO",
            "created_by": <owner_id>,
            "used_by":    null | <user_id>,
            "used_at":    null | "ISO"
        },
        ...
    }
  }
"""
import json
import logging
import secrets
import string
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import ACCESS_KEY, ALLOWLIST_FILE, OWNER_ID

log = logging.getLogger(__name__)


# ── persistence ─────────────────────────────────────────────────
def _load() -> dict:
    if not ALLOWLIST_FILE.exists():
        return {"users": [], "keys": {}}
    try:
        data = json.loads(ALLOWLIST_FILE.read_text() or "{}")
        # совместимость со старым форматом (просто список юзеров)
        if isinstance(data, list):
            data = {"users": [int(x) for x in data], "keys": {}}
        data.setdefault("users", [])
        data.setdefault("keys", {})
        data["users"] = [int(x) for x in data["users"]]
        return data
    except Exception as e:
        log.warning(f"keys load failed: {e}")
        return {"users": [], "keys": {}}


def _save(data: dict) -> None:
    try:
        ALLOWLIST_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        log.warning(f"keys save failed: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── публичный API ──────────────────────────────────────────────
def is_owner(user_id: int) -> bool:
    return int(user_id) == OWNER_ID


def is_allowed(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    return int(user_id) in _load().get("users", [])


def try_unlock(user_id: int, text: str) -> bool:
    """Пытается активировать доступ по тексту.
    1) Одноразовый ключ KEY-XXXX из выпущенных OWNER-ом.
    2) Мастер-ключ ACCESS_KEY (bootstrap).
    Возвращает True, если доступ открыт."""
    if not text:
        return False
    text = text.strip()

    data = _load()

    # 1) одноразовый ключ
    k = data["keys"].get(text)
    if k is not None and k.get("used_by") is None:
        k["used_by"] = int(user_id)
        k["used_at"] = _now()
        if int(user_id) not in data["users"]:
            data["users"].append(int(user_id))
        _save(data)
        log.warning(f"key {text} consumed by user_id={user_id}")
        return True

    # 2) мастер-ключ (без расходования)
    if ACCESS_KEY and text == ACCESS_KEY:
        if int(user_id) not in data["users"]:
            data["users"].append(int(user_id))
            _save(data)
        log.warning(f"master key used by user_id={user_id}")
        return True

    return False


def revoke(user_id: int) -> bool:
    """Отозвать доступ (OWNER нельзя)."""
    if is_owner(user_id):
        return False
    data = _load()
    if int(user_id) not in data["users"]:
        return False
    data["users"] = [u for u in data["users"] if u != int(user_id)]
    _save(data)
    return True


# ── админка: только для OWNER ──────────────────────────────────
def _new_code() -> str:
    """Генерирует случайный код KEY-XXXX (4 символа A-Z + 0-9)."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "KEY-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if code not in _load().get("keys", {}):
            return code


def create_key(owner_id: int) -> str:
    """Создаёт новый одноразовый ключ и возвращает его текст."""
    code = _new_code()
    data = _load()
    data["keys"][code] = {
        "created_at": _now(),
        "created_by": int(owner_id),
        "used_by": None,
        "used_at": None,
    }
    _save(data)
    return code


def list_keys() -> List[dict]:
    """Возвращает все ключи, отсортированные по дате создания (новые сверху)."""
    data = _load()
    rows = []
    for code, meta in data.get("keys", {}).items():
        rows.append({
            "code": code,
            "created_at": meta.get("created_at"),
            "used_by": meta.get("used_by"),
            "used_at": meta.get("used_at"),
        })
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return rows


def list_users() -> List[int]:
    return list(_load().get("users", []))
