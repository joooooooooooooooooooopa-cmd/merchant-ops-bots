"""Разбор выгрузок чатов Telegram (Export → Machine-readable JSON).
Дедуп по id сообщений: первый раз — всё (с лимитом), потом — только новое с прошлой выгрузки."""
import os
import json

STATE_FILE = os.getenv("CHATDUMP_STATE_FILE", "chat_dump_state.json")
FIRST_CAP = 400          # при первой выгрузке берём последние N сообщений (контроль токенов)
MAX_CHARS = 14000        # лимит транскрипта в анализ


def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def _flatten_text(t):
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        out = []
        for p in t:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                out.append(p.get("text", ""))
        return "".join(out)
    return ""


def parse(raw_bytes):
    """Разбирает JSON-выгрузку Telegram. Возвращает (chat_name, chat_id, messages[])."""
    data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    # одиночный чат: {name, id, messages:[...]}; полная выгрузка: {chats:{list:[...]}}
    if "messages" not in data and isinstance(data.get("chats"), dict):
        lst = data["chats"].get("list", [])
        data = max(lst, key=lambda c: len(c.get("messages", [])), default={})
    name = data.get("name") or "(без названия)"
    cid = str(data.get("id") or name)
    msgs = []
    for m in data.get("messages", []):
        if m.get("type") != "message":
            continue
        txt = _flatten_text(m.get("text"))
        if not txt.strip():
            continue
        msgs.append({
            "id": int(m.get("id") or 0),
            "date": (m.get("date") or "").replace("T", " ")[:16],
            "sender": m.get("from") or "?",
            "text": txt.strip(),
        })
    msgs.sort(key=lambda x: x["id"])
    return name, cid, msgs


def new_since(chat_key, messages):
    """Только сообщения новее последнего обработанного id. (first, new_msgs, total)."""
    st = _load()
    last = int(st.get(chat_key, 0))
    first = chat_key not in st
    if first:
        new = messages[-FIRST_CAP:]
    else:
        new = [m for m in messages if m["id"] > last]
    return first, new, len(messages)


def commit(chat_key, messages):
    """Зафиксировать максимальный обработанный id."""
    if not messages:
        return
    st = _load()
    st[chat_key] = max(m["id"] for m in messages)
    _save(st)


def transcript(messages):
    """Текст-транскрипт для анализа (с хвоста, под лимит символов)."""
    lines = [f"[{m['date']}] {m['sender']}: {m['text']}" for m in messages]
    out = "\n".join(lines)
    if len(out) > MAX_CHARS:
        out = out[-MAX_CHARS:]
    return out


def is_full_export(raw_bytes):
    """True если это полная выгрузка (chats.list с >1 чатами)."""
    try:
        d = json.loads(raw_bytes.decode("utf-8", errors="replace"))
        return isinstance(d.get("chats"), dict) and len(d["chats"].get("list", [])) > 1
    except Exception:
        return False


def parse_all_chats(raw_bytes):
    """Парсит ПОЛНУЮ выгрузку — возвращает [{chat, type, msgs:[{id,date,sender,text}, ...]}]."""
    d = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    chats_root = d.get("chats")
    if isinstance(chats_root, dict):
        chats = chats_root.get("list", [])
    elif isinstance(chats_root, list):
        chats = chats_root
    else:
        return []
    out = []
    for ch in chats:
        nm = ch.get("name") or ""
        msgs = []
        for m in ch.get("messages", []):
            if m.get("type") != "message":
                continue
            txt = _flatten_text(m.get("text"))
            if not txt.strip():
                continue
            msgs.append({
                "id": int(m.get("id") or 0),
                "date": (m.get("date") or "").replace("T", " ")[:16],
                "sender": m.get("from") or "?",
                "text": txt.strip(),
            })
        msgs.sort(key=lambda x: x["id"])
        out.append({"chat": nm, "type": ch.get("type", "?"), "msgs": msgs})
    return out
