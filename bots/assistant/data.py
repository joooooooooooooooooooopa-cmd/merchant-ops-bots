"""Слой данных для AM-ассистента: метрики мерчантов из read-only реплики Core.
Все цифры — отсюда (бот их не выдумывает). Модель конверсии 1:1 с CRM-ботом."""
import os
import logging
import difflib

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)
ANALYTICS_DB_URL = os.getenv("ANALYTICS_DB_URL", "")

SUCC = "i.payment_status_id IN (3,6)"
REQJ = ("LEFT JOIN (SELECT DISTINCT invoice_id FROM pricing_invoicerequisite) reqj "
        "ON reqj.invoice_id = i.id")
ISS = ("(reqj.invoice_id IS NOT NULL "
       "OR COALESCE(i.data->'route_result'->>'status'='success', false) "
       "OR i.payment_status_id IN (3,6))")
MERCHANT = "COALESCE(mr.brand_name, mr.iname, '—')"
MERJ = "LEFT JOIN pricing_merchant mr ON mr.id = i.merchant_id"
PRODJ = "LEFT JOIN pricing_product p ON p.id = i.primary_product_id"
# Полный фильтр тестов: флаг + блок тест-мерчей по имени (iname И brand_name —
# у delta-test/bravo-test brand_name «чистый», по одному brand протекали).
# Без имённого блока тест-трафик с is_test=false искажал платформенные агрегаты
# (gate_health, platform_benchmark, antifraud_window и т.д.).
NOTEST = ("i.is_test = false AND i.merchant_id NOT IN ("
          "SELECT id FROM pricing_merchant "
          "WHERE iname ILIKE '%%test%%' OR COALESCE(brand_name,'') ILIKE '%%test%%')")
TODAY = ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
         "= (now() AT TIME ZONE 'Europe/Moscow')::date")
YESTERDAY = ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
             "= (now() AT TIME ZONE 'Europe/Moscow')::date - 1")
LAST7 = "i.created_at > now() - interval '7 days'"


# Признак выданного счёта/реквизита (card-рек ИЛИ счёт получателя в data ИЛИ route success)
ACCT = ("(reqj.invoice_id IS NOT NULL "
        "OR i.data->'beneficiary'->>'pan' IS NOT NULL "
        "OR i.data->'beneficiary'->>'account_number' IS NOT NULL "
        "OR COALESCE(i.data->'route_result'->>'status'='success', false))")
ANTIFRAUD = "i.last_payment_error_code = 'ROUTING_ERROR'"
GATEJ = ("LEFT JOIN pricing_terminal t ON t.id = i.primary_terminal_id "
         "LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id")


def _conn():
    if not ANALYTICS_DB_URL:
        raise RuntimeError("ANALYTICS_DB_URL не задан")
    return psycopg2.connect(ANALYTICS_DB_URL, connect_timeout=10)


def _q(sql, params=None):
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET statement_timeout = 20000")
            cur.execute(sql, params or ())
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


import re as _re
_SQL_FORBIDDEN = _re.compile(
    r'\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|'
    r'comment|call|do|merge|reindex|cluster|lock|listen|notify|set\s+role|reset|'
    r'pg_sleep|pg_read_file|pg_ls_dir|dblink)\b', _re.I)


def run_sql(sql, max_rows=200):
    """Произвольный READ-ONLY SELECT к реплике — для аналитика-ассистента (копает данные сам).
    Защита: только SELECT/WITH, один запрос, readonly-сессия, statement_timeout 30с, лимит строк."""
    s = (sql or "").strip().rstrip(";").strip()
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return {"error": "разрешены только SELECT/WITH запросы"}
    if ";" in s:
        return {"error": "только один запрос, без ';'"}
    if _SQL_FORBIDDEN.search(s):
        return {"error": "разрешено только чтение (SELECT), любые изменения запрещены"}
    conn = None
    try:
        conn = _conn()
        conn.set_session(readonly=True)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET statement_timeout = 30000")
            cur.execute(s)
            if not cur.description:
                return {"error": "запрос ничего не вернул"}
            rows = [dict(r) for r in cur.fetchmany(max_rows)]
        return {"rows": rows, "count": len(rows), "truncated": len(rows) >= max_rows}
    except Exception as e:
        return {"error": str(e)[:400]}
    finally:
        if conn:
            conn.close()


def test_connection():
    try:
        _conn().close()
        return True
    except Exception:
        return False


def list_merchants():
    rows = _q(f"""
        SELECT {MERCHANT} AS m, COUNT(*) AS n
        FROM pricing_invoice i {MERJ}
        WHERE {LAST7} AND {NOTEST}
        GROUP BY 1 ORDER BY n DESC
    """)
    out = []
    for r in rows:
        m = r["m"]
        if m and m != "—" and "test" not in m.lower():
            out.append(m)
    return out


def resolve_merchant(name):
    """Фаззи-резолв имени мерчанта в реальное название из БД."""
    ms = list_merchants()
    if not name:
        return None
    nl = name.strip().lower()
    for m in ms:
        if m.lower() == nl:
            return m
    for m in ms:
        if nl in m.lower() or m.lower() in nl:
            return m
    cl = difflib.get_close_matches(nl, [m.lower() for m in ms], n=1, cutoff=0.55)
    if cl:
        for m in ms:
            if m.lower() == cl[0]:
                return m
    return None


def _window(merchant, where_time):
    rows = _q(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {ISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COALESCE(ROUND(SUM(i.price) FILTER (WHERE {SUCC})),0) AS amount
        FROM pricing_invoice i {REQJ} {MERJ}
        WHERE {where_time} AND {NOTEST} AND {MERCHANT} = %s
    """, (merchant,))
    r = rows[0] if rows else {}
    tot = int(r.get("total") or 0)
    wr = int(r.get("with_req") or 0)
    su = int(r.get("success") or 0)
    return {
        "total": tot, "with_req": wr, "success": su,
        "amount_rub": int(float(r.get("amount") or 0)),
        "conv_pct": round(su * 100.0 / wr, 1) if wr else 0.0,
        "conv_of_total_pct": round(su * 100.0 / tot, 1) if tot else 0.0,
    }


def _by_endpoint(merchant, where_time):
    rows = _q(f"""
        SELECT COALESCE(p.name,'—') AS endpoint,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {ISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COALESCE(ROUND(SUM(i.price) FILTER (WHERE {SUCC})),0) AS amount
        FROM pricing_invoice i {REQJ} {MERJ} {PRODJ}
        WHERE {where_time} AND {NOTEST} AND {MERCHANT} = %s
        GROUP BY 1 ORDER BY total DESC
    """, (merchant,))
    out = []
    for r in rows:
        tot = int(r["total"]); wr = int(r["with_req"]); su = int(r["success"])
        out.append({
            "endpoint": r["endpoint"], "total": tot, "with_req": wr, "success": su,
            "amount_rub": int(float(r["amount"] or 0)),
            "conv_pct": round(su * 100.0 / wr, 1) if wr else 0.0,
        })
    return out


def merchant_overview(merchant):
    """Конверсия/оборот мерчанта: сегодня / вчера / 7д + разбивка по эндпоинтам (7д)."""
    return {
        "merchant": merchant,
        "today": _window(merchant, TODAY),
        "yesterday": _window(merchant, YESTERDAY),
        "last7d": _window(merchant, LAST7),
        "by_endpoint_7d": _by_endpoint(merchant, LAST7),
    }


def merchant_behavior(merchant, hours=24):
    """Разбор отвала по эндпоинтам: юзер (закрыл/протупил) vs анти-фрод vs наша/гейт."""
    h = int(hours)
    rows = _q(f"""
        WITH base AS (
          SELECT COALESCE(p.name,'—') AS endpoint,
                 i.payment_status_id AS st, i.last_payment_error_code AS code
          FROM pricing_invoice i {MERJ} {PRODJ}
          WHERE i.created_at > now() - interval '{h} hours' AND {NOTEST} AND {MERCHANT} = %s
        )
        SELECT endpoint, COUNT(*) AS total,
          COUNT(*) FILTER (WHERE st IN (3,6)) AS paid,
          COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code='CANCELLED') AS closed_form,
          COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code IN ('validation_error','SESSION_EXPIRED')) AS idle,
          COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code='ROUTING_ERROR') AS antifraud,
          COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code IN ('COMMON','COMMON_ERROR','provider_error','provider_request_timeout')) AS tech
        FROM base GROUP BY endpoint ORDER BY total DESC
    """, (merchant,))
    out = []
    for r in rows:
        t = int(r["total"]) or 1
        closed, idle = int(r["closed_form"]), int(r["idle"])
        af, tech, paid = int(r["antifraud"]), int(r["tech"]), int(r["paid"])
        out.append({
            "endpoint": r["endpoint"], "total": int(r["total"]), "paid": paid,
            "user_closed_form": closed, "user_idle_timeout": idle,
            "antifraud": af, "tech_our_gate": tech,
            "paid_pct": round(paid * 100.0 / t, 1),
            "user_abandon_pct": round((closed + idle) * 100.0 / t, 1),
            "tech_pct": round(tech * 100.0 / t, 1),
        })
    return out


def window_conv(merchant, minutes):
    """Конверсия мерчанта за последние N минут (для проактивного мониторинга просадки)."""
    m = int(minutes)
    rows = _q(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {ISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM pricing_invoice i {REQJ} {MERJ}
        WHERE i.created_at > now() - interval '{m} minutes' AND {NOTEST} AND {MERCHANT} = %s
    """, (merchant,))
    r = rows[0] if rows else {}
    wr = int(r.get("with_req") or 0); su = int(r.get("success") or 0)
    return {"total": int(r.get("total") or 0), "with_req": wr, "success": su,
            "conv": round(su * 100.0 / wr, 1) if wr else 0.0}


def gate_health(minutes=60):
    """Конверсия по гейтам за N минут — для детекта 'наша/гейтовая проблема'."""
    m = int(minutes)
    rows = _q(f"""
        SELECT COALESCE(g.iname,'(нет гейта)') AS gate,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {ISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM pricing_invoice i {REQJ}
        LEFT JOIN pricing_paymentgate g ON g.id = i.primary_payment_gate_id
        WHERE i.created_at > now() - interval '{m} minutes' AND {NOTEST}
        GROUP BY 1 ORDER BY total DESC
    """)
    out = []
    for r in rows:
        wr = int(r["with_req"]); su = int(r["success"])
        out.append({
            "gate": r["gate"], "total": int(r["total"]), "with_req": wr, "success": su,
            "conv_pct": round(su * 100.0 / wr, 1) if wr else 0.0,
        })
    return out


def withdrawals_overview(merchant, days=7):
    """Выплаты мерчанта за N дней: статусы (done/pending/rejected), суммы, валюты, топ-ошибка."""
    d = int(days)
    rows = _q(f"""
        SELECT w.status AS status, w.currency_code_id AS cur,
               COUNT(*) AS n, COALESCE(ROUND(SUM(w.amount)),0) AS amt
        FROM pricing_withdrawalrequest w
        LEFT JOIN pricing_merchant mr ON mr.id = w.merchant_id
        WHERE w.created_at > now() - interval '{d} days' AND w.is_test = false
          AND COALESCE(mr.brand_name, mr.iname, '—') = %(m)s
        GROUP BY w.status, w.currency_code_id ORDER BY n DESC
    """, {"m": merchant})
    err = _q(f"""
        SELECT w.last_payment_error_code AS code, COUNT(*) AS n
        FROM pricing_withdrawalrequest w
        LEFT JOIN pricing_merchant mr ON mr.id = w.merchant_id
        WHERE w.created_at > now() - interval '{d} days' AND w.is_test = false
          AND COALESCE(mr.brand_name, mr.iname, '—') = %(m)s
          AND w.status='rejected' AND w.last_payment_error_code IS NOT NULL
        GROUP BY 1 ORDER BY 2 DESC LIMIT 3
    """, {"m": merchant})
    by_status = [{"status": r["status"], "currency": r["cur"], "count": int(r["n"]),
                  "amount": int(float(r["amt"] or 0))} for r in rows]
    return {"merchant": merchant, "days": d, "by_status": by_status,
            "top_reject_errors": [{"code": e["code"], "count": int(e["n"])} for e in err]}


def lookup_order(query):
    """Сверка заявки/выплаты по ID (external_id/merchant_order/id/uuid). Возвращает до 5 совпадений."""
    q = (query or "").strip()
    if not q:
        return []
    inv = _q("""
        SELECT 'пай-ин' AS kind, i.external_id, i.merchant_order, i.id::text AS id,
               (i.created_at AT TIME ZONE 'Europe/Moscow') AS created_msk,
               s.iname AS status, ROUND(i.price) AS amount, ROUND(i.amount_paid) AS paid,
               COALESCE(mr.brand_name, mr.iname,'—') AS merchant, p.name AS endpoint,
               t.name AS terminal, g.iname AS gate,
               i.last_payment_error_code AS err_code, i.last_payment_error AS err_msg
        FROM pricing_invoice i
        LEFT JOIN pricing_paymentstatus s ON s.id = i.payment_status_id
        LEFT JOIN pricing_merchant mr ON mr.id = i.merchant_id
        LEFT JOIN pricing_product p ON p.id = i.primary_product_id
        LEFT JOIN pricing_terminal t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE (lower(i.external_id)=lower(%(q)s) OR lower(i.merchant_order)=lower(%(q)s)
               OR replace(lower(i.merchant_order),'-','')=replace(lower(%(q)s),'-','')
               OR replace(lower(i.id::text),'-','')=replace(lower(%(q)s),'-','')
               OR replace(lower(i.uuid::text),'-','')=replace(lower(%(q)s),'-',''))
        ORDER BY i.created_at DESC LIMIT 5
    """, {"q": q})
    wd = _q("""
        SELECT 'выплата' AS kind, w.external_id, w.merchant_order, w.id::text AS id,
               (w.created_at AT TIME ZONE 'Europe/Moscow') AS created_msk,
               w.status, ROUND(w.requested_amount) AS amount, ROUND(w.amount) AS paid,
               COALESCE(mr.brand_name, mr.iname,'—') AS merchant, p.name AS endpoint,
               w.currency_code_id AS currency,
               w.last_payment_error_code AS err_code, w.last_payment_error AS err_msg
        FROM pricing_withdrawalrequest w
        LEFT JOIN pricing_merchant mr ON mr.id = w.merchant_id
        LEFT JOIN pricing_product p ON p.id = w.primary_product_id
        WHERE (lower(w.external_id)=lower(%(q)s) OR lower(w.merchant_order)=lower(%(q)s)
               OR replace(lower(w.merchant_order),'-','')=replace(lower(%(q)s),'-','')
               OR replace(lower(w.id::text),'-','')=replace(lower(%(q)s),'-','')
               OR replace(lower(w.uuid::text),'-','')=replace(lower(%(q)s),'-',''))
        ORDER BY w.created_at DESC LIMIT 5
    """, {"q": q})
    for r in inv + wd:
        if r.get("created_msk"):
            r["created_msk"] = r["created_msk"].strftime("%d.%m.%Y %H:%M")
    return inv + wd


def merchant_rates(merchant):
    """Ставки мерчанта (fee_from_merchant) по его эндпоинтам — для ответов про комиссии."""
    rows = _q("""
        SELECT DISTINCT p.name AS endpoint, pc.percent, pc.fix_amount AS fix,
               pc.min_amount AS min, pc.max_amount AS max
        FROM pricing_productcommission pc
        JOIN pricing_product p ON p.id = pc.product_id
        JOIN pricing_commissiontype ct ON ct.id = pc.commission_type_id
        WHERE ct.iname='fee_from_merchant' AND pc.commission_object='invoice'
          AND (pc.end_date IS NULL OR pc.end_date > now())
          AND (pc.percent <> 0 OR pc.fix_amount <> 0)
          AND p.id IN (
            SELECT DISTINCT i.primary_product_id FROM pricing_invoice i
            LEFT JOIN pricing_merchant mr ON mr.id = i.merchant_id
            WHERE i.created_at > now() - interval '30 days' AND i.is_test=false
              AND COALESCE(mr.brand_name, mr.iname,'—') = %(m)s)
        ORDER BY endpoint
    """, {"m": merchant})
    return [{"endpoint": r["endpoint"], "percent": float(r["percent"] or 0),
             "fix": float(r["fix"] or 0),
             "min": float(r["min"] or 0),
             "max": float(r["max"]) if r["max"] is not None else None} for r in rows]


def merchant_trend(merchant, days=7):
    """Дневная конверсия мерчанта за N дней (тренд)."""
    d = int(days)
    rows = _q(f"""
        SELECT (i.created_at AT TIME ZONE 'Europe/Moscow')::date AS day,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {ISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COALESCE(ROUND(SUM(i.price) FILTER (WHERE {SUCC})),0) AS amount
        FROM pricing_invoice i {REQJ} {MERJ}
        WHERE i.created_at > now() - interval '{d} days' AND {NOTEST} AND {MERCHANT} = %(m)s
        GROUP BY 1 ORDER BY 1
    """, {"m": merchant})
    out = []
    for r in rows:
        wr = int(r["with_req"]); su = int(r["success"])
        out.append({"day": str(r["day"]), "total": int(r["total"]), "with_req": wr, "success": su,
                    "amount_rub": int(float(r["amount"] or 0)),
                    "conv_pct": round(su * 100.0 / wr, 1) if wr else 0.0})
    return out


def money_in_limbo(merchant, days=7):
    """«Деньги в подвисе» — заявкам выдан реквизит/маршрут, но НЕ оплачены (упущенная выручка)."""
    d = int(days)
    rows = _q(f"""
        SELECT COALESCE(p.name,'—') AS endpoint,
               COUNT(*) FILTER (WHERE {ISS} AND NOT {SUCC}) AS lost_cnt,
               COALESCE(ROUND(SUM(i.price) FILTER (WHERE {ISS} AND NOT {SUCC})),0) AS lost_rub
        FROM pricing_invoice i {REQJ} {MERJ} {PRODJ}
        WHERE i.created_at > now() - interval '{d} days' AND {NOTEST} AND {MERCHANT} = %(m)s
        GROUP BY 1 ORDER BY lost_rub DESC
    """, {"m": merchant})
    eps = [{"endpoint": r["endpoint"], "lost_count": int(r["lost_cnt"]),
            "lost_rub": int(float(r["lost_rub"] or 0))} for r in rows if int(r["lost_cnt"]) > 0]
    return {"merchant": merchant, "days": d,
            "lost_count": sum(e["lost_count"] for e in eps),
            "lost_rub": sum(e["lost_rub"] for e in eps),
            "by_endpoint": eps[:10]}


def platform_benchmark(merchant):
    """Конверсия мерча vs средняя по платформе (7д) vs цель + место в рейтинге мерчей."""
    from config import TARGET_CONV
    pr = _q(f"""SELECT COUNT(*) FILTER (WHERE {SUCC}) AS su, COUNT(*) FILTER (WHERE {ISS}) AS wr
                FROM pricing_invoice i {REQJ} WHERE {LAST7} AND {NOTEST}""")
    p = pr[0] if pr else {}
    psu = int(p.get("su") or 0); pwr = int(p.get("wr") or 0)
    plat = round(psu * 100.0 / pwr, 1) if pwr else 0.0
    mw = _window(merchant, LAST7)
    mconv = mw["conv_pct"]
    rows = _q(f"""SELECT {MERCHANT} AS m, COUNT(*) FILTER (WHERE {SUCC}) AS su,
                         COUNT(*) FILTER (WHERE {ISS}) AS wr
                  FROM pricing_invoice i {REQJ} {MERJ}
                  WHERE {LAST7} AND {NOTEST}
                  GROUP BY 1 HAVING COUNT(*) FILTER (WHERE {ISS}) >= 200""")
    pairs = sorted([(r["m"], round(int(r["su"]) * 100.0 / int(r["wr"]), 1))
                    for r in rows if int(r["wr"]) > 0], key=lambda x: -x[1])
    rank = next((i + 1 for i, (mm, _) in enumerate(pairs) if mm == merchant), None)
    return {"merchant": merchant, "merchant_conv_7d": mconv, "platform_avg_conv_7d": plat,
            "gap_vs_platform_pp": round(mconv - plat, 1),
            "target_conv": TARGET_CONV, "gap_vs_target_pp": round(mconv - TARGET_CONV, 1),
            "rank": rank, "of_merchants": len(pairs)}


def merchants_recent_vs_base(recent_min=120):
    """Для слежки за ВСЕМИ мерчами: конверсия за окно vs база 7д по каждому мерчу."""
    m = int(recent_min)
    recent = _q(f"""SELECT {MERCHANT} AS mm, COUNT(*) FILTER (WHERE {ISS}) AS wr,
                           COUNT(*) FILTER (WHERE {SUCC}) AS su
                    FROM pricing_invoice i {REQJ} {MERJ}
                    WHERE i.created_at > now() - interval '{m} minutes' AND {NOTEST} GROUP BY 1""")
    base = _q(f"""SELECT {MERCHANT} AS mm, COUNT(*) FILTER (WHERE {ISS}) AS wr,
                         COUNT(*) FILTER (WHERE {SUCC}) AS su
                  FROM pricing_invoice i {REQJ} {MERJ}
                  WHERE {LAST7} AND {NOTEST} GROUP BY 1""")
    bmap = {r["mm"]: (int(r["su"]), int(r["wr"])) for r in base}
    out = []
    for r in recent:
        mm = r["mm"]; rwr = int(r["wr"]); rsu = int(r["su"])
        if rwr <= 0 or not mm or "test" in mm.lower():
            continue
        bsu, bwr = bmap.get(mm, (0, 0))
        out.append({"merchant": mm, "recent_wr": rwr,
                    "recent_conv": round(rsu * 100.0 / rwr, 1),
                    "base_conv": round(bsu * 100.0 / bwr, 1) if bwr else 0.0})
    return out


def integration_candidates(days_new=21, max_success=10):
    """Авто-кандидаты на интеграцию из данных: новые мерчи без трафика ИЛИ есть трафик, но ~0 успехов
    (тестят/интегрируются). Подсказка — финально решает человек."""
    rows = _q("""
        SELECT COALESCE(mr.brand_name, mr.iname, '—') AS m,
               EXTRACT(DAY FROM now() - mr.created_at)::int AS age_days,
               COUNT(i.id) FILTER (WHERE i.created_at > now() - interval '30 days') AS total30,
               COUNT(i.id) FILTER (WHERE i.created_at > now() - interval '30 days'
                                   AND i.payment_status_id IN (3,6)) AS succ30
        FROM pricing_merchant mr
        LEFT JOIN pricing_invoice i ON i.merchant_id = mr.id AND i.is_test = false
        WHERE mr.is_test = false AND mr.iname NOT ILIKE '%%test%%'
          AND COALESCE(mr.brand_name, '') NOT ILIKE '%%test%%'
        GROUP BY 1, 2
    """)
    out = []
    for r in rows:
        m = r["m"]
        if not m or m == "—":
            continue
        total = int(r["total30"] or 0); succ = int(r["succ30"] or 0); age = int(r["age_days"] or 999)
        reason = None
        if total == 0 and age <= days_new:
            reason = f"новый ({age}д), трафика ещё нет"
        elif total > 0 and succ <= max_success:
            reason = f"трафик {total}/30д, успехов {succ} — тестит/интегрируется"
        elif age <= days_new and succ <= max_success:
            reason = f"новый ({age}д), успехов мало ({succ})"
        if reason:
            out.append({"merchant": m, "reason": reason, "age_days": age,
                        "total30": total, "succ30": succ})
    out.sort(key=lambda x: (x["succ30"], x["age_days"]))
    return out[:15]


def gate_window(minutes=120):
    """Гейты за окно: total/success/conv + доля упавших без выданного счёта (provider issuance health)."""
    m = int(minutes)
    rows = _q(f"""
        SELECT COALESCE(g.iname,'(нет гейта)') gate,
               COUNT(*) total,
               COUNT(*) FILTER (WHERE {SUCC}) success,
               COUNT(*) FILTER (WHERE NOT {SUCC}) failed,
               COUNT(*) FILTER (WHERE NOT {SUCC} AND NOT {ACCT}) failed_no_acct
        FROM pricing_invoice i {REQJ} {GATEJ}
        WHERE i.created_at > now() - interval '{m} minutes' AND {NOTEST}
        GROUP BY 1 ORDER BY total DESC
    """)
    out = []
    for r in rows:
        tot = int(r["total"]); su = int(r["success"]); fl = int(r["failed"]); fna = int(r["failed_no_acct"])
        out.append({"gate": r["gate"], "total": tot, "success": su, "failed": fl,
                    "failed_no_acct": fna,
                    "conv": round(su * 100.0 / tot, 1) if tot else 0.0,
                    "no_acct_share": round(fna * 100.0 / fl, 1) if fl else 0.0})
    return out


def gate_conv_baseline(days=7):
    """Базовая конверсия по гейтам за N дней — для сравнения с окном."""
    d = int(days)
    rows = _q(f"""
        SELECT COALESCE(g.iname,'(нет гейта)') gate,
               COUNT(*) total, COUNT(*) FILTER (WHERE {SUCC}) success
        FROM pricing_invoice i {GATEJ}
        WHERE i.created_at > now() - interval '{d} days' AND {NOTEST}
        GROUP BY 1
    """)
    return {r["gate"]: (round(int(r["success"]) * 100.0 / int(r["total"]), 1) if int(r["total"]) else 0.0)
            for r in rows}


def antifraud_window(minutes=120):
    """Анти-фрод реза за окно: общий % (ROUTING_ERROR) + топ мерчей по резу."""
    m = int(minutes)
    tot = _q(f"""
        SELECT COUNT(*) total, COUNT(*) FILTER (WHERE {ANTIFRAUD}) rejected
        FROM pricing_invoice i WHERE i.created_at > now() - interval '{m} minutes' AND {NOTEST}
    """)
    t = int(tot[0]["total"]) if tot else 0
    rj = int(tot[0]["rejected"]) if tot else 0
    by = _q(f"""
        SELECT {MERCHANT} merchant, COUNT(*) total, COUNT(*) FILTER (WHERE {ANTIFRAUD}) rejected
        FROM pricing_invoice i {MERJ}
        WHERE i.created_at > now() - interval '{m} minutes' AND {NOTEST}
        GROUP BY 1 HAVING COUNT(*) FILTER (WHERE {ANTIFRAUD}) > 0
        ORDER BY rejected DESC LIMIT 6
    """)
    return {"total": t, "rejected": rj,
            "rate": round(rj * 100.0 / t, 1) if t else 0.0,
            "by_merchant": [{"merchant": r["merchant"], "total": int(r["total"]),
                             "rejected": int(r["rejected"]),
                             "rate": round(int(r["rejected"]) * 100.0 / int(r["total"]), 1)}
                            for r in by]}


def priority_brief():
    """Сводка по приоритетным мерчам (Bravo, Alpha): overview + поведение 24ч + гейты."""
    from config import PRIORITY
    res = {"merchants": [], "gate_health_60m": gate_health(60)}
    for name in PRIORITY:
        m = resolve_merchant(name)
        if not m:
            res["merchants"].append({"requested": name, "error": "не найден"})
            continue
        res["merchants"].append({
            "merchant": m,
            "overview": merchant_overview(m),
            "behavior_24h": merchant_behavior(m, 24),
        })
    return res
