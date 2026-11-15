"""Локальное хранилище ассистента: заметки по мерчам + напоминания (JSON)."""
import os
import json
import time
import uuid
from datetime import datetime

NOTES_FILE = os.getenv("NOTES_FILE", "notes.json")
REM_FILE = os.getenv("REMINDERS_FILE", "reminders.json")
INTEG_FILE = os.getenv("INTEGRATION_FILE", "integration.json")
QUIET_FILE = os.getenv("QUIET_FILE", "quiet_events.json")


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, d):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


# ── Заметки по мерчам ──────────────────────────────────────────
def add_note(merchant, text):
    d = _load(NOTES_FILE, {})
    k = (merchant or "").strip().lower()
    d.setdefault(k, []).append({"ts": time.time(), "text": text, "merchant": merchant})
    _save(NOTES_FILE, d)
    return True


def get_notes(merchant, limit=10):
    d = _load(NOTES_FILE, {})
    k = (merchant or "").strip().lower()
    items = d.get(k, [])[-limit:]
    return [{"when": datetime.fromtimestamp(n["ts"]).strftime("%d.%m %H:%M"), "text": n["text"]}
            for n in items]


def recent_notes(limit=15):
    d = _load(NOTES_FILE, {})
    flat = []
    for arr in d.values():
        flat += arr
    flat.sort(key=lambda n: n["ts"], reverse=True)
    return [{"merchant": n.get("merchant"), "when": datetime.fromtimestamp(n["ts"]).strftime("%d.%m %H:%M"),
             "text": n["text"]} for n in flat[:limit]]


# ── Напоминания ────────────────────────────────────────────────
def add_reminder(due_ts, text):
    d = _load(REM_FILE, [])
    rid = uuid.uuid4().hex[:8]
    d.append({"id": rid, "due_ts": float(due_ts), "text": text, "fired": False, "created": time.time()})
    _save(REM_FILE, d)
    return rid


def due_reminders(now=None):
    now = now or time.time()
    d = _load(REM_FILE, [])
    due, changed = [], False
    for r in d:
        if not r.get("fired") and r.get("due_ts", 0) <= now:
            due.append(r)
            r["fired"] = True
            changed = True
    if changed:
        _save(REM_FILE, d)
    return due


def list_reminders():
    d = _load(REM_FILE, [])
    out = []
    for r in d:
        if not r.get("fired"):
            out.append({"when": datetime.fromtimestamp(r["due_ts"]).strftime("%d.%m %H:%M"), "text": r["text"]})
    return out


# ── Интеграционный трекер ──────────────────────────────────────
def integ_add(merchant, stage="", note=""):
    d = _load(INTEG_FILE, [])
    for it in d:
        if it["merchant"].lower() == merchant.lower():
            if stage:
                it["stage"] = stage
            if note:
                it["note"] = note
            _save(INTEG_FILE, d)
            return it["merchant"]
    d.append({"merchant": merchant, "stage": stage, "note": note,
              "added": time.time(), "last_contact": 0})
    _save(INTEG_FILE, d)
    return merchant


def integ_list():
    d = _load(INTEG_FILE, [])
    out = []
    for it in d:
        lc = it.get("last_contact", 0)
        days = None if not lc else round((time.time() - lc) / 86400, 1)
        out.append({"merchant": it["merchant"], "stage": it.get("stage", ""),
                    "note": it.get("note", ""), "days_since_contact": days})
    out.sort(key=lambda x: (x["days_since_contact"] is not None, -(x["days_since_contact"] or 1e9)))
    return out


def integ_touch(merchant):
    d = _load(INTEG_FILE, [])
    for it in d:
        if it["merchant"].lower() == merchant.lower():
            it["last_contact"] = time.time()
            _save(INTEG_FILE, d)
            return True
    d.append({"merchant": merchant, "stage": "", "note": "",
              "added": time.time(), "last_contact": time.time()})
    _save(INTEG_FILE, d)
    return True


def integ_remove(merchant):
    d = _load(INTEG_FILE, [])
    d2 = [it for it in d if it["merchant"].lower() != merchant.lower()]
    _save(INTEG_FILE, d2)
    return len(d) != len(d2)


# ── Очередь событий в тихие часы ───────────────────────────────
def quiet_add(text):
    d = _load(QUIET_FILE, [])
    d.append({"ts": time.time(), "text": text})
    _save(QUIET_FILE, d)


def quiet_flush():
    d = _load(QUIET_FILE, [])
    _save(QUIET_FILE, [])
    return d
