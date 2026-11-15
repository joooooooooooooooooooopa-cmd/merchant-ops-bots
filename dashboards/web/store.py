"""Локальная БД дашборда (SQLite): passkey-креды, сессии (косвенно), конструктор интеграции.
Core НЕ трогаем — это только наши собственные данные (трекинг мерчей на интеграции)."""
import sqlite3
import json
import time

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    cred_id     TEXT PRIMARY KEY,      -- base64url credential id
    public_key  TEXT NOT NULL,          -- base64url cose public key
    sign_count  INTEGER NOT NULL DEFAULT 0,
    label       TEXT,                   -- имя устройства/passkey
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracked (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    stage       TEXT NOT NULL DEFAULT 'lead',   -- lead/integration/live/paused
    sold        TEXT NOT NULL DEFAULT '[]',     -- JSON: проданные методы/гейты
    journey     TEXT NOT NULL DEFAULT '[]',     -- JSON: [{ts, text}] путь интеграции
    note        TEXT NOT NULL DEFAULT '',
    merchant_id TEXT,                            -- связка с живым мерчем Core (когда подключился)
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS done_marks (
    key   TEXT NOT NULL,                  -- идентификатор авто-дела (risk:<mid> / cross:<mid> / unset:<mid>)
    day   TEXT NOT NULL,                  -- YYYY-MM-DD (МСК) — дела отмечаются на конкретный день
    ts    REAL NOT NULL,
    PRIMARY KEY (key, day)
);
CREATE TABLE IF NOT EXISTS checklist (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    day   TEXT NOT NULL,                  -- YYYY-MM-DD (МСК) — чеклист на день
    text  TEXT NOT NULL,
    done  INTEGER NOT NULL DEFAULT 0,
    ts    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant_id TEXT NOT NULL,
    text        TEXT NOT NULL,
    ts          REAL NOT NULL
);
"""

STAGES = ["lead", "integration", "live", "paused"]


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with _conn() as c:
        c.executescript(_SCHEMA)
        # миграция: rp_id у credentials (для мульти-домен passkey — localhost + ts.net)
        cols = [r[1] for r in c.execute("PRAGMA table_info(credentials)")]
        if "rp_id" not in cols:
            c.execute("ALTER TABLE credentials ADD COLUMN rp_id TEXT")
        c.execute("UPDATE credentials SET rp_id='localhost' WHERE rp_id IS NULL")
        # миграция: checklist.body — расширенный текст (готовое сообщение/контекст), раскрывается на клик
        cols_cl = [r[1] for r in c.execute("PRAGMA table_info(checklist)")]
        if "body" not in cols_cl:
            c.execute("ALTER TABLE checklist ADD COLUMN body TEXT")


# ── passkey credentials (rp_id = домен, к которому привязан passkey) ──
def list_credentials(rp_id=None):
    with _conn() as c:
        if rp_id is not None:
            return [dict(r) for r in c.execute(
                "SELECT * FROM credentials WHERE rp_id=? ORDER BY created_at", (rp_id,))]
        return [dict(r) for r in c.execute("SELECT * FROM credentials ORDER BY created_at")]


def has_credentials(rp_id=None) -> bool:
    with _conn() as c:
        if rp_id is not None:
            return c.execute("SELECT COUNT(*) FROM credentials WHERE rp_id=?", (rp_id,)).fetchone()[0] > 0
        return c.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] > 0


def add_credential(cred_id, public_key, sign_count, label, rp_id="localhost"):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO credentials (cred_id, public_key, sign_count, label, created_at, rp_id) "
            "VALUES (?,?,?,?,?,?)",
            (cred_id, public_key, int(sign_count), label or "passkey", time.time(), rp_id),
        )


def get_credential(cred_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM credentials WHERE cred_id=?", (cred_id,)).fetchone()
        return dict(r) if r else None


def update_sign_count(cred_id, sign_count):
    with _conn() as c:
        c.execute("UPDATE credentials SET sign_count=? WHERE cred_id=?", (int(sign_count), cred_id))


# ── конструктор: трекинг мерчей на интеграции ────────────────────
def list_tracked():
    with _conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM tracked ORDER BY updated_at DESC")]
    for r in rows:
        r["sold"] = json.loads(r["sold"] or "[]")
        r["journey"] = json.loads(r["journey"] or "[]")
    return rows


def add_tracked(name, stage="lead", note=""):
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tracked (name, stage, sold, journey, note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, stage if stage in STAGES else "lead", "[]",
             json.dumps([{"ts": now, "text": "Заведён на трекинг"}], ensure_ascii=False), note, now, now),
        )
        return cur.lastrowid


def update_tracked(tid, **fields):
    allowed = {"name", "stage", "note", "merchant_id"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?"); vals.append(time.time())
    vals.append(tid)
    with _conn() as c:
        c.execute(f"UPDATE tracked SET {', '.join(sets)} WHERE id=?", vals)


def set_sold(tid, sold_list):
    with _conn() as c:
        c.execute("UPDATE tracked SET sold=?, updated_at=? WHERE id=?",
                  (json.dumps(sold_list, ensure_ascii=False), time.time(), tid))


def add_journey(tid, text):
    with _conn() as c:
        r = c.execute("SELECT journey FROM tracked WHERE id=?", (tid,)).fetchone()
        if not r:
            return
        j = json.loads(r["journey"] or "[]")
        j.append({"ts": time.time(), "text": text})
        c.execute("UPDATE tracked SET journey=?, updated_at=? WHERE id=?",
                  (json.dumps(j, ensure_ascii=False), time.time(), tid))


def delete_tracked(tid):
    with _conn() as c:
        c.execute("DELETE FROM tracked WHERE id=?", (tid,))


# ── отметки «сделал» по авто-делам (на день) ─────────────────────
def list_done(day):
    with _conn() as c:
        return [r["key"] for r in c.execute("SELECT key FROM done_marks WHERE day=?", (day,))]


def set_done(key, day, done=True):
    with _conn() as c:
        if done:
            c.execute("INSERT OR REPLACE INTO done_marks (key, day, ts) VALUES (?,?,?)",
                      (key, day, time.time()))
        else:
            c.execute("DELETE FROM done_marks WHERE key=? AND day=?", (key, day))


# ── личный чеклист на день ───────────────────────────────────────
def list_checklist(day):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, text, body, done, ts FROM checklist WHERE day=? ORDER BY ts", (day,))]


def add_check(day, text, body=None):
    with _conn() as c:
        cur = c.execute("INSERT INTO checklist (day, text, body, done, ts) VALUES (?,?,?,0,?)",
                        (day, text, body, time.time()))
        return cur.lastrowid


def toggle_check(cid, done):
    with _conn() as c:
        c.execute("UPDATE checklist SET done=? WHERE id=?", (1 if done else 0, cid))


def update_check_body(cid, body):
    with _conn() as c:
        c.execute("UPDATE checklist SET body=? WHERE id=?", (body, cid))


def delete_check(cid):
    with _conn() as c:
        c.execute("DELETE FROM checklist WHERE id=?", (cid,))


# ── заметки по мерчу ─────────────────────────────────────────────
def list_notes(mid):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, text, ts FROM notes WHERE merchant_id=? ORDER BY ts DESC", (mid,))]


def add_note(mid, text):
    with _conn() as c:
        cur = c.execute("INSERT INTO notes (merchant_id, text, ts) VALUES (?,?,?)",
                        (mid, text, time.time()))
        return cur.lastrowid


def delete_note(nid):
    with _conn() as c:
        c.execute("DELETE FROM notes WHERE id=?", (nid,))
