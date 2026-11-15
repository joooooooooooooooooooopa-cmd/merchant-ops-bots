"""Read-only слой к реплике Core. Ничего не пишем — только наглядность.
Прибыль/маржу НЕ считаем (наценка курса не в этой таблице). Показываем выработку,
эндпоинты, методы и оффер (ставка с мерча fee_from_merchant)."""
import re
import psycopg2
import psycopg2.extras

from config import ANALYTICS_DB_URL

_CUR = {"rub", "try", "kzt", "uzs", "eur", "usd", "amd", "azn", "tjs", "idr", "inr"}
_NOISE = {"ftd", "std", "in", "out", "ln", "test"}


def _clean_method(name, drop_first=False, drop=()):
    """Человеческое имя метода из имени продукта/терминала: срезаем валюту, FTD/STD,
    направление, коридоры сумм (10k+, 1k+), мусор. -> 'P2P Card', 'SBP', 'C2C'…"""
    drop = {d.lower() for d in drop if d}
    name = re.sub(r"\([^)]*\)", " ", name or "")  # убрать коридоры в скобках
    toks = [t for t in re.split(r"[_\s/]+", name.strip()) if t]
    if drop_first and toks:
        toks = toks[1:]  # первый токен = мерч (у продуктов)
    keep = []
    for t in toks:
        tl = t.lower()
        if tl in _CUR or tl in _NOISE or tl in drop:
            continue
        if re.fullmatch(r"\d+k?\+?", tl) or re.fullmatch(r"\d+", tl):
            continue
        keep.append(t)
    return (" ".join(keep).strip(" -")) or "—"

SUCC = "i.payment_status_id IN (3,6)"  # 3=complete, 6=partial_complete
NOTEST = "i.is_test = false"
# окно «сегодня по МСК»
TODAY = ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
         "= (now() AT TIME ZONE 'Europe/Moscow')::date")


def _q(sql, params=None):
    conn = psycopg2.connect(ANALYTICS_DB_URL, connect_timeout=10)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET statement_timeout = 20000")
            cur.execute(sql, params or {})
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def healthy() -> bool:
    try:
        _q("SELECT 1 AS ok")
        return True
    except Exception:
        return False


ISS = ("(EXISTS (SELECT 1 FROM pricing_invoicerequisite ir WHERE ir.invoice_id = i.id) "
       f"OR {SUCC})")
NOT_TESTMERCH = ("lower(COALESCE(m.iname,'')) NOT LIKE '%%test%%' "
                 "AND lower(COALESCE(m.iname,'')) NOT LIKE '%%internal%%'")


def _window(days):
    """SQL-условие окна: 1 = сегодня по МСК, иначе rolling N суток."""
    if int(days) <= 1:
        return TODAY, {}
    return "i.created_at > now() - interval %(win)s", {"win": f"{int(days)} days"}


def _dual_windows(days):
    """Текущее окно и эквивалентное предыдущее (для at-risk). Возвращает (base_interval, cur_sql, prev_sql)."""
    d = int(days)
    if d <= 1:
        cur = TODAY
        prev = ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
                "= (now() AT TIME ZONE 'Europe/Moscow')::date - 1 "
                "AND (i.created_at AT TIME ZONE 'Europe/Moscow')::time "
                "<= (now() AT TIME ZONE 'Europe/Moscow')::time")
        return "2 days", cur, prev
    cur = f"i.created_at > now() - interval '{d} days'"
    prev = f"i.created_at <= now() - interval '{d} days'"
    return f"{2 * d} days", cur, prev


def merchants(days=1):
    """Карточки мерчей: пролив за окно + сравнение с прошлым периодом (at-risk).
    -> [{mid,name,total,issued,success,conv,turnover, prev_*, risk, risk_reason}]."""
    base, cur, prev = _dual_windows(days)
    rows = _q(f"""
        SELECT m.id::text AS mid, COALESCE(m.brand_name, m.iname) AS name,
               COUNT(*) FILTER (WHERE {cur}) AS total,
               COUNT(*) FILTER (WHERE {cur} AND {SUCC}) AS success,
               COUNT(*) FILTER (WHERE {cur} AND {ISS}) AS issued,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {cur} AND {SUCC})), 0) AS turnover,
               COUNT(*) FILTER (WHERE {prev}) AS prev_total,
               COUNT(*) FILTER (WHERE {prev} AND {SUCC}) AS prev_success,
               COUNT(*) FILTER (WHERE {prev} AND {ISS}) AS prev_issued
        FROM pricing_invoice i
        JOIN pricing_merchant m ON m.id = i.merchant_id
        WHERE i.created_at > now() - interval '{base}' AND {NOTEST} AND {NOT_TESTMERCH}
        GROUP BY 1, 2
        HAVING COUNT(*) FILTER (WHERE {cur}) > 0
        ORDER BY turnover DESC, total DESC
    """)
    out = []
    for r in rows:
        iss = int(r["issued"] or 0)
        total = int(r["total"] or 0)
        success = int(r["success"] or 0)
        conv = round(success * 100.0 / iss, 1) if iss else 0.0
        p_iss = int(r["prev_issued"] or 0)
        p_total = int(r["prev_total"] or 0)
        p_conv = round(int(r["prev_success"] or 0) * 100.0 / p_iss, 1) if p_iss else 0.0
        risk, reason = False, ""
        if p_total >= 20:  # судим только при значимом прошлом объёме
            if total < p_total * 0.6:
                risk, reason = True, f"объём ↓ {round((1-total/p_total)*100)}% к прошлому периоду"
            elif p_conv - conv >= 12:
                risk, reason = True, f"конверсия ↓ {round(p_conv-conv,1)}п.п. ({p_conv}%→{conv}%)"
        out.append({"mid": r["mid"], "name": r["name"], "total": total, "issued": iss,
                    "success": success, "conv": conv, "turnover": int(float(r["turnover"] or 0)),
                    "prev_total": p_total, "prev_conv": p_conv, "risk": risk, "risk_reason": reason})
    return out


_DECLINE_CAT = {
    "CANCELLED": ("👤", "клиент закрыл форму"),
    "validation_error": ("⏳", "клиент не завершил"),
    "SESSION_EXPIRED": ("⏳", "сессия истекла"),
    "ROUTING_ERROR": ("🛡", "антифрод / маршрут"),
}


def _decline_cat(code):
    c = (code or "").upper()
    if code in _DECLINE_CAT:
        return _DECLINE_CAT[code]
    if "TIMEOUT" in c or "PROVIDER" in c or "COMMON" in c or "GATE" in c:
        return ("🛠", "тех / гейт")
    if "CANCEL" in c:
        return ("👤", "клиент закрыл форму")
    return ("•", "")


def merchant_declines(mid, days=1):
    """Топ причин, почему НЕ оплатили (не success), за окно. -> [{code,label,emoji,cnt,sample}]."""
    win, p = _window(days)
    p = dict(p, mid=mid)
    rows = _q(f"""
        SELECT COALESCE(i.last_payment_error_code, '(нет кода)') AS code,
               COUNT(*) AS cnt, MAX(i.last_payment_error) AS sample
        FROM pricing_invoice i
        WHERE {win} AND {NOTEST} AND i.merchant_id = %(mid)s AND i.payment_status_id NOT IN (3,6)
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8
    """, p)
    for r in rows:
        emoji, label = _decline_cat(r["code"])
        r["emoji"], r["label"] = emoji, label
        r["cnt"] = int(r["cnt"] or 0)
        r["sample"] = (r["sample"] or "")[:60]
    return rows


def merchant_list(days=30):
    """Все мерчи с активностью за N суток — для связки конструктора. -> [{mid,name}]."""
    return _q(f"""
        SELECT m.id::text AS mid, COALESCE(m.brand_name, m.iname) AS name
        FROM pricing_invoice i
        JOIN pricing_merchant m ON m.id = i.merchant_id
        WHERE i.created_at > now() - interval '{int(days)} days' AND {NOTEST} AND {NOT_TESTMERCH}
        GROUP BY 1, 2 ORDER BY 2
    """)


def merchant_endpoints(mid):
    """Эндпоинты мерча + оффер (ставка fee_from_merchant %). is_set = ставка проставлена."""
    rows = _q("""
        SELECT p.name AS endpoint,
               pc.percent AS percent, pc.fix_amount AS fix,
               p.is_available_for_invoice AS active
        FROM pricing_product p
        LEFT JOIN pricing_productcommission pc
               ON pc.product_id = p.id AND pc.commission_object = 'invoice'
              AND (pc.end_date IS NULL OR pc.end_date > now())
        LEFT JOIN pricing_commissiontype ct
               ON ct.id = pc.commission_type_id AND ct.iname = 'fee_from_merchant'
        WHERE p.merchant_id = %(mid)s
          AND p.name NOT ILIKE 'gate-%%'
        ORDER BY p.name
    """, {"mid": mid})
    out = []
    seen = set()
    for r in rows:
        name = r["endpoint"] or "—"
        pct = float(r["percent"]) if r["percent"] is not None else 0.0
        fix = float(r["fix"]) if r["fix"] is not None else 0.0
        key = (name,)
        if key in seen:
            continue
        seen.add(key)
        out.append({"endpoint": name, "method": _clean_method(name, drop_first=True),
                    "percent": pct, "fix": fix,
                    "is_set": (pct != 0 or fix != 0), "active": bool(r["active"])})
    return out


def merchant_methods(mid):
    """Распиывает методы мерча (уникальные, человеческие) из его эндпоинтов."""
    seen, out = set(), []
    for e in merchant_endpoints(mid):
        m = e["method"]
        if m and m != "—" and m.lower() not in seen:
            seen.add(m.lower()); out.append(m)
    return out


def provider_detail(gate):
    """Деталь провайдера: методы (из терминалов), терминалы, трафик за сутки, кто льёт."""
    g = (gate or "").replace("gate-", "").lower()
    terms = _q("""
        SELECT t.name AS name, t.status AS status,
               ROUND(t.min_amount) AS min_amount, ROUND(t.max_amount) AS max_amount
        FROM pricing_terminal t
        JOIN pricing_paymentgate pg ON pg.id = t.payment_gate_id
        WHERE pg.iname = %(gate)s AND t.is_available_for_invoice = true
          AND lower(COALESCE(t.name,'')) NOT LIKE '%%test%%'
        ORDER BY t.name
    """, {"gate": gate})
    methods, mseen = [], set()
    for t in terms:
        t["active"] = (t["status"] == "active")  # БЫЛ БАГ: фронт читает t.active, поле не вычислялось
        t["method"] = _clean_method(t["name"], drop=[g])
        t["min_amount"] = int(float(t["min_amount"])) if t["min_amount"] is not None else 0
        t["max_amount"] = int(float(t["max_amount"])) if t["max_amount"] is not None else 0
        m = t["method"]
        if m and m != "—" and m.lower() not in mseen:
            mseen.add(m.lower()); methods.append(m)
    traffic = _q(f"""
        SELECT COALESCE(mr.brand_name, mr.iname) AS merchant,
               COUNT(*) AS total, COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM pricing_invoice i
        JOIN pricing_terminal t ON t.id = i.primary_terminal_id
        JOIN pricing_paymentgate pg ON pg.id = t.payment_gate_id
        JOIN pricing_merchant mr ON mr.id = i.merchant_id
        WHERE {TODAY} AND {NOTEST} AND pg.iname = %(gate)s
        GROUP BY 1 ORDER BY total DESC
    """, {"gate": gate})
    for r in traffic:
        r["total"] = int(r["total"] or 0); r["success"] = int(r["success"] or 0)
    return {"gate": gate, "methods": methods, "terminals": terms, "traffic": traffic}


def merchant_gates(mid, days=7):
    """Реальные провайдеры/гейты по факту трафика мерча за окно."""
    win, p = _window(days)
    p = dict(p, mid=mid)
    return _q(f"""
        SELECT COALESCE(g.iname, '(без гейта)') AS gate,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM pricing_invoice i
        LEFT JOIN pricing_terminal t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE {win} AND {NOTEST} AND i.merchant_id = %(mid)s
        GROUP BY 1 ORDER BY total DESC
    """, p)


def providers():
    """Каталог провайдеров: гейт → терминалы (коридор/метод в имени) — что можно продавать мерчам."""
    rows = _q("""
        SELECT g.iname AS gate, t.name AS terminal, t.status AS status
        FROM pricing_paymentgate g
        JOIN pricing_terminal t ON t.payment_gate_id = g.id
        WHERE lower(COALESCE(g.iname,'')) NOT LIKE '%%mock%%'
          AND lower(COALESCE(g.iname,'')) NOT LIKE '%%test%%'
          AND t.is_available_for_invoice = true
          AND lower(COALESCE(t.name,'')) NOT LIKE '%%test%%'
        ORDER BY g.iname, t.name
    """)
    agg = {}
    order = []
    for r in rows:
        gate = r["gate"] or "—"
        if gate not in agg:
            agg[gate] = {"gate": gate, "terminals": [], "methods": [], "_seen": set()}
            order.append(gate)
        agg[gate]["terminals"].append({"name": r["terminal"], "active": r["status"] == "active"})
        m = _clean_method(r["terminal"], drop=[gate.replace("gate-", "").lower()])
        if m and m != "—" and m.lower() not in agg[gate]["_seen"]:
            agg[gate]["_seen"].add(m.lower()); agg[gate]["methods"].append(m)
    out = []
    for g in order:
        agg[g].pop("_seen", None)
        out.append(agg[g])
    return out


# ── Канонизация методов в категории (для cross-sell и матрицы) ───
CATEGORIES = ["SBP", "QR NSPK", "Card/C2C", "Transborder", "Havale", "Ecom"]


def canon_method(label):
    """Грубая, но устойчивая категория метода (порядок важен: Transborder до SBP/Card)."""
    l = (label or "").lower()
    if "havale" in l:
        return "Havale"
    if "qr" in l or "nspk" in l or "нспк" in l:
        return "QR NSPK"
    if "trans" in l or "interac" in l:
        return "Transborder"
    if "sbp" in l or "сбп" in l:
        return "SBP"
    if "ecom" in l:
        return "Ecom"
    if any(k in l for k in ("c2c", "card", "карт", "alfa", "rigel-kzt", "mcom", " mc", "bynqo-")):
        return "Card/C2C"
    return None


def _catalog_cat_gates():
    """Категория -> какие гейты её дают (из каталога провайдеров)."""
    m = {}
    for p in providers():
        for meth in p["methods"]:
            cat = canon_method(meth)
            if cat:
                m.setdefault(cat, set()).add(p["gate"])
    return {k: sorted(v) for k, v in m.items()}


def merchant_offer(mid):
    """Cross-sell: что ещё можно продать мерчу.
    -> {have:[категории], gap:[{cat,gates}], unset:[{endpoint,method}]}."""
    eps = merchant_endpoints(mid)
    have, unset = set(), []
    for e in eps:
        cat = canon_method(e["method"])
        if cat:
            have.add(cat)
        if not e["is_set"] and e["active"]:
            unset.append({"endpoint": e["endpoint"], "method": e["method"]})
    catg = _catalog_cat_gates()
    gap = [{"cat": cat, "gates": gates[:6]} for cat, gates in catg.items() if cat not in have]
    gap.sort(key=lambda x: x["cat"])
    return {"have": sorted(have), "gap": gap, "unset": unset[:12]}


def merchant_method_conv(mid, days=1):
    """Конверсия по методам мерча — где теряем. -> [{method,total,success,conv}]."""
    win, p = _window(days)
    p = dict(p, mid=mid)
    rows = _q(f"""
        SELECT p.name AS product, COUNT(*) AS total, COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM pricing_invoice i JOIN pricing_product p ON p.id = i.primary_product_id
        WHERE {win} AND {NOTEST} AND i.merchant_id = %(mid)s
        GROUP BY 1
    """, p)
    agg = {}
    for r in rows:
        meth = _clean_method(r["product"], drop_first=True)
        a = agg.setdefault(meth, {"method": meth, "total": 0, "success": 0})
        a["total"] += int(r["total"] or 0)
        a["success"] += int(r["success"] or 0)
    out = []
    for a in agg.values():
        a["conv"] = round(a["success"] * 100.0 / a["total"], 1) if a["total"] else 0.0
        out.append(a)
    out.sort(key=lambda x: -x["total"])
    return out


def overview(days=1):
    """Общая картина дня + моверы + at-risk (агрегат из merchants)."""
    ms = merchants(days)
    cur_total = sum(m["total"] for m in ms)
    cur_succ = sum(m["success"] for m in ms)
    cur_iss = sum(m["issued"] for m in ms)
    prev_total = sum(m["prev_total"] for m in ms)
    movers = []
    for m in ms:
        if m["prev_total"] >= 20:
            movers.append({"name": m["name"], "mid": m["mid"],
                           "delta": round((m["total"] / m["prev_total"] - 1) * 100),
                           "total": m["total"], "conv": m["conv"]})
    up = sorted([x for x in movers if x["delta"] > 0], key=lambda x: -x["delta"])[:3]
    down = sorted([x for x in movers if x["delta"] < 0], key=lambda x: x["delta"])[:3]
    return {"days": days, "merchants": len(ms), "total": cur_total, "success": cur_succ,
            "turnover": sum(m["turnover"] for m in ms),
            "conv": round(cur_succ * 100.0 / cur_iss, 1) if cur_iss else 0.0,
            "dvol": round((cur_total / prev_total - 1) * 100) if prev_total else None,
            "up": up, "down": down, "at_risk": [m["name"] for m in ms if m["risk"]]}


def gate_scorecard(days=1):
    """Скоринг гейтов: объём + конверсия (латентности в данных нет — duration=0)."""
    win, p = _window(days)
    rows = _q(f"""
        SELECT g.iname AS gate, COUNT(*) AS total, COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM pricing_invoice i
        JOIN pricing_terminal t ON t.id = i.primary_terminal_id
        JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE {win} AND {NOTEST} AND lower(COALESCE(g.iname,'')) NOT LIKE '%%mock%%'
        GROUP BY 1 HAVING COUNT(*) > 0 ORDER BY total DESC
    """, p)
    for r in rows:
        r["total"] = int(r["total"] or 0)
        r["success"] = int(r["success"] or 0)
        r["conv"] = round(r["success"] * 100.0 / r["total"], 1) if r["total"] else 0.0
    return rows


def method_matrix(days=1):
    """Матрица активные мерчи × категории методов (что подключено). Белые пятна = что продать."""
    active = {m["mid"]: m for m in merchants(days)}
    if not active:
        return {"categories": CATEGORIES, "rows": []}
    rows = _q(f"""
        SELECT m.id::text AS mid, p.name AS product
        FROM pricing_product p JOIN pricing_merchant m ON m.id = p.merchant_id
        WHERE p.is_available_for_invoice = true AND p.name NOT ILIKE 'gate-%%' AND {NOT_TESTMERCH}
    """)
    cats = {}
    for r in rows:
        if r["mid"] not in active:
            continue
        cat = canon_method(_clean_method(r["product"], drop_first=True))
        if cat:
            cats.setdefault(r["mid"], set()).add(cat)
    out = []
    for mid, m in active.items():
        have = cats.get(mid, set())
        out.append({"name": m["name"], "mid": mid, "turnover": m["turnover"],
                    "cells": [c in have for c in CATEGORIES]})
    out.sort(key=lambda x: -x["turnover"])
    return {"categories": CATEGORIES, "rows": out}


def provider_rates():
    """Ставки провайдеров (payment_gate_fee) по категории×гейту. -> {cat:{gate:{min,max}}}."""
    rows = _q("""
        SELECT g.iname AS gate, t.name AS term, tc.percent AS pct
        FROM pricing_terminalcommission tc
        JOIN pricing_terminal t ON t.id = tc.terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        JOIN pricing_commissiontype ct ON ct.id = tc.commission_type_id
        WHERE ct.iname = 'payment_gate_fee' AND (tc.end_date IS NULL OR tc.end_date > now())
          AND tc.percent IS NOT NULL AND tc.percent <> 0
    """)
    res = {}
    for r in rows:
        gate = r["gate"] or "—"
        cat = canon_method(_clean_method(r["term"], drop=[gate.replace("gate-", "").lower()]))
        if not cat:
            continue
        pct = float(r["pct"])
        d = res.setdefault(cat, {}).setdefault(gate, {"min": pct, "max": pct})
        d["min"] = min(d["min"], pct)
        d["max"] = max(d["max"], pct)
    return res


def method_catalog(days=1):
    """Витрина: метод -> провайдеры(+ставки) -> кто льёт / кому продать."""
    mm = method_matrix(days)
    rates = provider_rates()
    out = []
    for ci, cat in enumerate(mm["categories"]):
        have = [r["name"] for r in mm["rows"] if r["cells"][ci]]
        sell = [r["name"] for r in mm["rows"] if not r["cells"][ci]]
        provs = []
        for gate, rr in sorted(rates.get(cat, {}).items(), key=lambda x: x[1]["min"]):
            rng = f"{rr['min']:g}%" if rr["min"] == rr["max"] else f"{rr['min']:g}–{rr['max']:g}%"
            provs.append({"gate": gate, "rate": rng})
        out.append({"cat": cat, "providers": provs, "have": have, "sell": sell})
    return {"categories": out}


def conv_trend(days=14):
    """Дневная конверсия и оборот за N суток (МСК). -> [{date,total,issued,success,conv,turnover}]."""
    rows = _q(f"""
        SELECT (i.created_at AT TIME ZONE 'Europe/Moscow')::date AS d,
               COUNT(*) AS total, COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COUNT(*) FILTER (WHERE {ISS}) AS issued,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})), 0) AS turnover
        FROM pricing_invoice i
        JOIN pricing_merchant m ON m.id = i.merchant_id
        WHERE i.created_at > now() - interval '{int(days)} days' AND {NOTEST} AND {NOT_TESTMERCH}
        GROUP BY 1 ORDER BY 1
    """)
    out = []
    for r in rows:
        iss = int(r["issued"] or 0)
        su = int(r["success"] or 0)
        out.append({"date": str(r["d"]), "total": int(r["total"] or 0), "issued": iss,
                    "success": su, "conv": round(su * 100.0 / iss, 1) if iss else 0.0,
                    "turnover": int(float(r["turnover"] or 0))})
    return out


def merchant_conv_trend(mid, days=14):
    """Дневная конверсия одного мерча — для спарклайна в карточке."""
    rows = _q(f"""
        SELECT (i.created_at AT TIME ZONE 'Europe/Moscow')::date AS d,
               COUNT(*) FILTER (WHERE {SUCC}) AS success, COUNT(*) FILTER (WHERE {ISS}) AS issued
        FROM pricing_invoice i
        WHERE i.created_at > now() - interval '{int(days)} days' AND {NOTEST} AND i.merchant_id = %(mid)s
        GROUP BY 1 ORDER BY 1
    """, {"mid": mid})
    return [{"date": str(r["d"]),
             "conv": round(int(r["success"] or 0) * 100.0 / int(r["issued"] or 0), 1) if r["issued"] else 0.0}
            for r in rows]


def action_items(days=1):
    """Дела дня: кто просел, топ-допродажи, эндпоинты без ставки."""
    ms = merchants(days)
    catg = _catalog_cat_gates()
    at_risk = [{"name": m["name"], "mid": m["mid"], "reason": m["risk_reason"]} for m in ms if m["risk"]]
    cross, unset = [], []
    for m in ms:
        eps = merchant_endpoints(m["mid"])
        have, uc = set(), 0
        for e in eps:
            c = canon_method(e["method"])
            if c:
                have.add(c)
            if not e["is_set"] and e["active"]:
                uc += 1
        gaps = [c for c in catg if c not in have]
        if gaps:
            cross.append({"name": m["name"], "mid": m["mid"], "turnover": m["turnover"], "gaps": gaps})
        if uc:
            unset.append({"name": m["name"], "mid": m["mid"], "count": uc})
    cross.sort(key=lambda x: -x["turnover"])
    unset.sort(key=lambda x: -x["count"])
    return {"at_risk": at_risk, "cross": cross[:10], "unset": unset[:10]}


def boss_report(days=7):
    """Сводка для начальства: WoW + топ-мерчи + воронка возможностей."""
    ov = overview(days)
    ms = merchants(days)
    ai = action_items(days)
    return {"overview": ov,
            "top": [{"name": m["name"], "turnover": m["turnover"], "conv": m["conv"], "total": m["total"]}
                    for m in ms[:8]],
            "opp_cross": sum(len(c["gaps"]) for c in ai["cross"]),
            "opp_unset": sum(u["count"] for u in ai["unset"]),
            "at_risk": [a["name"] for a in ai["at_risk"]]}


def withdrawals(days=1):
    """Выплаты по мерчам за окно. -> [{merchant,total,done,rejected,pending,paid}]."""
    win = (TODAY.replace("i.created_at", "w.created_at") if int(days) <= 1
           else f"w.created_at > now() - interval '{int(days)} days'")
    rows = _q(f"""
        SELECT COALESCE(m.brand_name, m.iname) AS merchant,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE w.status='done') AS done,
               COUNT(*) FILTER (WHERE w.status='rejected') AS rejected,
               COUNT(*) FILTER (WHERE w.status='pending') AS pending,
               COALESCE(ROUND(SUM(w.amount) FILTER (WHERE w.status='done')), 0) AS paid
        FROM pricing_withdrawalrequest w
        JOIN pricing_merchant m ON m.id = w.merchant_id
        WHERE {win} AND w.is_test = false
          AND lower(COALESCE(m.iname,'')) NOT LIKE '%%test%%'
          AND lower(COALESCE(m.iname,'')) NOT LIKE '%%internal%%'
        GROUP BY 1 HAVING COUNT(*) > 0 ORDER BY total DESC
    """)
    for r in rows:
        for k in ("total", "done", "rejected", "pending"):
            r[k] = int(r[k] or 0)
        r["paid"] = int(float(r["paid"] or 0))
    return rows


def events(days=1):
    """Лента событий: просадки, моверы, «приём встал» — из текущих данных (без дублей по мерчу)."""
    ms = merchants(days)
    ov = overview(days)
    out, seen = [], set()

    def add(icon, name, mid, text):
        if mid in seen:
            return
        seen.add(mid)
        out.append({"icon": icon, "merchant": name, "mid": mid, "text": text})

    for m in ms:
        if m["issued"] >= 20 and m["success"] == 0:
            add("🛑", m["name"], m["mid"], "поток есть, 0 оплат")
    for m in ms:
        if m["risk"]:
            add("🚩", m["name"], m["mid"], m["risk_reason"])
    for u in ov["up"][:3]:
        add("📈", u["name"], u["mid"], f"объём +{u['delta']}%")
    for dn in ov["down"][:3]:
        add("📉", dn["name"], dn["mid"], f"объём {dn['delta']}%")
    return out
