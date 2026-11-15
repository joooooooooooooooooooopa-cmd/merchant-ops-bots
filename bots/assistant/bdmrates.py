"""Снапшот таблицы БДМ (утверждённые условия мерчантов) — источник правды по ставкам.
Ассистент сверяет это с фактическими ставками в БД (merchant_rates) и флагает расхождения."""
import os
import difflib

RATES_FILE = os.getenv("BDM_RATES_FILE", "bdm_rates.md")
_cache = None


def _parse():
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    try:
        txt = open(RATES_FILE, encoding="utf-8").read()
    except Exception:
        return _cache
    section = False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("## Условия по мерчантам"):
            section = True
            continue
        if section and s.startswith("## "):
            break
        if not section or not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 3:
            continue
        merch, method = cells[0], cells[1]
        if (not merch or not method or merch in ("#", "МЕРЧАНТ")
                or method.upper() in ("МЕТОД", "МЕРЧАНТ")):
            continue
        _cache.setdefault(merch, []).append({
            "method": method,
            "in": cells[2] if len(cells) > 2 else "",
            "out": cells[3] if len(cells) > 3 else "",
            "course": cells[4] if len(cells) > 4 else "",
            "info": cells[5] if len(cells) > 5 else "",
        })
    return _cache


def merchants():
    return list(_parse().keys())


def conditions(merchant):
    d = _parse()
    if not merchant:
        return None
    nl = merchant.strip().lower()
    for k in d:
        if k.lower() == nl:
            return {"merchant": k, "approved": d[k]}
    for k in d:
        if nl in k.lower() or k.lower() in nl:
            return {"merchant": k, "approved": d[k]}
    cl = difflib.get_close_matches(nl, [k.lower() for k in d], n=1, cutoff=0.5)
    if cl:
        for k in d:
            if k.lower() == cl[0]:
                return {"merchant": k, "approved": d[k]}
    return None


def geo_section():
    try:
        txt = open(RATES_FILE, encoding="utf-8").read()
    except Exception:
        return ""
    i = txt.find("## Гео-прайс")
    return txt[i:] if i >= 0 else ""
