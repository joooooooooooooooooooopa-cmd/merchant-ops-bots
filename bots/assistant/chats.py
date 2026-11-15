"""Снапшот рабочих чатов с мерчами (из chats.md). Чтобы бот видел покрытие чатами
и подсвечивал пробелы (по брифу БДМ: «убедиться, что с каждым мерчом есть рабочий чат»)."""
import os
import difflib

CHATS_FILE = os.getenv("CHATS_FILE", "chats.md")
SALES_CHAT = "Продажные дела"
_cache = None


def _parse():
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    try:
        txt = open(CHATS_FILE, encoding="utf-8").read()
    except Exception:
        return _cache
    section = False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("## Чаты по мерчантам"):
            section = True
            continue
        if section and s.startswith("## "):
            break
        if not section or ":" not in s or s.startswith("#"):
            continue
        name, tags = s.split(":", 1)
        _cache[name.strip()] = tags.strip()
    return _cache


def all_chats():
    return _parse()


def merchant_chats(merchant):
    d = _parse()
    if not merchant:
        return None
    nl = merchant.strip().lower()
    for k in d:
        if k.lower() == nl:
            return {"merchant": k, "chats": d[k]}
    for k in d:
        if nl in k.lower() or k.lower() in nl:
            return {"merchant": k, "chats": d[k]}
    cl = difflib.get_close_matches(nl, [k.lower() for k in d], n=1, cutoff=0.6)
    if cl:
        for k in d:
            if k.lower() == cl[0]:
                return {"merchant": k, "chats": d[k]}
    return {"merchant": merchant, "chats": None, "note": "рабочий чат не найден — сообщить БДМ, чтобы добавили"}
