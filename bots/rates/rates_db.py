"""Слой данных: ставки Acme из read-only реплики.
Разделено: ставки ЭНДПОИНТОВ (мерчантов) и ставки ПРОВАЙДЕРОВ (гейтов)."""
import os
import logging
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)
ANALYTICS_DB_URL = os.getenv("ANALYTICS_DB_URL", "")

# метод/коридор из имени продукта
PROD_METHOD = ("lower(regexp_replace(regexp_replace(p.name,"
               "'^[^_ ]+[_ ](?:RUB|TRY|KZT|UZS|EUR|USD|AMD|AZN|TJS|IDR)[_ ]',''),"
               "'([_ ](?:FTD|STD))?[_ ](?:In|Out|in|ln|IN|OUT)$',''))")
# имя мерчанта = первый токен (до _ или пробела)
MERCH_FROM_NAME = "substring(p.name from '^[^_ ]+')"

_TEST_MARKERS = ("test", "internal", "tease", "bdm_", "ops_test")


def _is_test(*parts):
    s = " ".join((p or "") for p in parts).lower()
    return any(k in s for k in _TEST_MARKERS)


def _connect():
    if not ANALYTICS_DB_URL:
        raise RuntimeError("ANALYTICS_DB_URL не задан")
    return psycopg2.connect(ANALYTICS_DB_URL, connect_timeout=10)


def _q(sql):
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET statement_timeout = 15000")
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_merchant_rates():
    """Ставки эндпоинтов (мерчантов): fee_from_merchant на продуктах-эндпоинтах.
    Гейты и тесты исключены. -> {merchant: [ {method, percent, fix, min, max} ]}."""
    rows = _q(f"""
        SELECT {MERCH_FROM_NAME} AS merchant,
               {PROD_METHOD} AS method,
               pc.percent, pc.fix_amount, pc.min_amount, pc.max_amount, p.name AS product
        FROM pricing_productcommission pc
        JOIN pricing_product p ON p.id = pc.product_id
        JOIN pricing_commissiontype ct ON ct.id = pc.commission_type_id
        WHERE ct.iname='fee_from_merchant' AND pc.commission_object='invoice'
          AND (pc.start_date IS NULL OR pc.start_date <= now())
          AND (pc.end_date IS NULL OR pc.end_date > now())
          AND (pc.percent <> 0 OR pc.fix_amount <> 0)
          AND p.name NOT ILIKE 'gate-%'
        ORDER BY merchant, pc.percent DESC
    """)
    result, seen = {}, set()
    for r in rows:
        if _is_test(r["merchant"], r["product"]):
            continue
        method = r["method"] or "—"
        pct, fix = float(r["percent"] or 0), float(r["fix_amount"] or 0)
        mn = float(r["min_amount"] or 0)
        mx = float(r["max_amount"]) if r["max_amount"] is not None else None
        key = (r["merchant"], method, pct, fix, mn, mx)
        if key in seen:
            continue
        seen.add(key)
        result.setdefault(r["merchant"], []).append(
            {"method": method, "percent": pct, "fix": fix, "min": mn, "max": mx})
    return result


def get_endpoint_rates():
    """Ставки ПО ЭНДПОИНТАМ (без схлопывания FTD/STD), включая нулевые (не проставлено).
    -> {merchant: [ {endpoint, percent, fix, min, max, is_set} ]}."""
    rows = _q(f"""
        SELECT {MERCH_FROM_NAME} AS merchant, p.name AS endpoint, {PROD_METHOD} AS method,
               pc.percent, pc.fix_amount, pc.min_amount, pc.max_amount
        FROM pricing_productcommission pc
        JOIN pricing_product p ON p.id = pc.product_id
        JOIN pricing_commissiontype ct ON ct.id = pc.commission_type_id
        WHERE ct.iname='fee_from_merchant' AND pc.commission_object='invoice'
          AND (pc.start_date IS NULL OR pc.start_date <= now())
          AND (pc.end_date IS NULL OR pc.end_date > now())
          AND p.name NOT ILIKE 'gate-%'
        ORDER BY merchant, p.name, pc.start_date DESC NULLS LAST
    """)
    result, seen = {}, set()
    for r in rows:
        if _is_test(r["merchant"], r["endpoint"]):
            continue
        pct, fix = float(r["percent"] or 0), float(r["fix_amount"] or 0)
        mn = float(r["min_amount"] or 0)
        mx = float(r["max_amount"]) if r["max_amount"] is not None else None
        key = (r["merchant"], r["endpoint"])
        if key in seen:
            continue
        seen.add(key)
        result.setdefault(r["merchant"], []).append(
            {"endpoint": r["endpoint"], "method": r["method"] or "—",
             "percent": pct, "fix": fix, "min": mn, "max": mx,
             "is_set": (pct != 0 or fix != 0)})
    return result


def get_merchant_portfolio(merchant, days=7):
    """Портфолио мерча: выработка за период (оборот/конв) + реальные гейты + методы/эндпоинты со ставками.
    Мерч матчится по первому токену имени продукта (как в rate-боте)."""
    d = int(days)
    esc = (merchant or "").replace("'", "''")
    # «Сутки» = календарный день МСК (как в CRM/TurnoverBot), а не скользящие 24ч —
    # иначе три бота показывают три разных «суток»
    if d == 1:
        win = ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
               "= (now() AT TIME ZONE 'Europe/Moscow')::date")
    else:
        win = f"i.created_at > now() - interval '{d} days'"
    # двойной фильтр тестов: токен «Bravo» матчит и продукты bravo-test-мерча
    notest = ("i.is_test=false AND p.name NOT ILIKE '%test%' "
              "AND i.merchant_id NOT IN (SELECT id FROM pricing_merchant "
              "WHERE iname ILIKE '%test%' OR COALESCE(brand_name,'') ILIKE '%test%')")
    base = (f"FROM pricing_invoice i JOIN pricing_product p ON p.id=i.primary_product_id "
            f"WHERE {win} AND {notest} AND substring(p.name from '^[^_ ]+') ILIKE '{esc}'")
    ISS = ("(EXISTS(SELECT 1 FROM pricing_invoicerequisite ir WHERE ir.invoice_id=i.id) "
           "OR COALESCE(i.data->'route_result'->>'status'='success',false) "
           "OR i.payment_status_id IN (3,6))")
    s = _q(f"""SELECT COUNT(*) total,
                      COUNT(*) FILTER (WHERE {ISS}) issued,
                      COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) success,
                      COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE i.payment_status_id IN (3,6))),0) amount
               {base}""")
    row = s[0] if s else {}
    tot = int(row.get("total") or 0); iss = int(row.get("issued") or 0); su = int(row.get("success") or 0)
    summary = {"total": tot, "issued": iss, "success": su,
               "amount": int(float(row.get("amount") or 0)),
               "conv": round(su * 100.0 / iss, 1) if iss else 0.0}

    gates = _q(f"""SELECT COALESCE(g.iname,'(нет гейта)') gate, COUNT(*) total,
                          COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) success
                   FROM pricing_invoice i JOIN pricing_product p ON p.id=i.primary_product_id
                   LEFT JOIN pricing_terminal t ON t.id=i.primary_terminal_id
                   LEFT JOIN pricing_paymentgate g ON g.id=t.payment_gate_id
                   WHERE {win} AND {notest} AND substring(p.name from '^[^_ ]+') ILIKE '{esc}'
                   GROUP BY 1 ORDER BY total DESC""")
    gate_list = []
    for x in gates:
        gt = int(x["total"]); gs = int(x["success"])
        gate_list.append({"gate": x["gate"], "total": gt, "success": gs,
                          "conv": round(gs * 100.0 / gt, 1) if gt else 0.0})
    return {"merchant": merchant, "days": d, "summary": summary, "gates": gate_list}


def get_provider_rates():
    """Ставки провайдеров (гейтов): payment_gate_fee по ТЕРМИНАЛАМ (коридор/сумма в имени терминала),
    сгруппировано по гейту. -> {gate: [ {terminal, obj, percent, fix, min, max} ]}."""
    rows = _q("""
        SELECT g.iname AS gate, t.name AS terminal, tc.commission_object AS obj,
               tc.percent, tc.fix_amount, tc.min_amount, tc.max_amount
        FROM pricing_terminalcommission tc
        JOIN pricing_terminal t ON t.id = tc.terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        JOIN pricing_commissiontype ct ON ct.id = tc.commission_type_id
        WHERE ct.iname='payment_gate_fee'
          AND (tc.start_date IS NULL OR tc.start_date <= now())
          AND (tc.end_date IS NULL OR tc.end_date > now())
          AND (tc.percent <> 0 OR tc.fix_amount <> 0)
        ORDER BY g.iname, t.name
    """)
    result, seen = {}, set()
    for r in rows:
        gate = r["gate"] or "(без гейта)"
        term = r["terminal"] or "—"
        if _is_test(gate, term):
            continue
        pct, fix = float(r["percent"] or 0), float(r["fix_amount"] or 0)
        mn = float(r["min_amount"] or 0)
        mx = float(r["max_amount"]) if r["max_amount"] is not None else None
        key = (gate, term, r["obj"], pct, fix, mn, mx)
        if key in seen:
            continue
        seen.add(key)
        result.setdefault(gate, []).append({
            "terminal": term, "obj": r["obj"], "percent": pct, "fix": fix, "min": mn, "max": mx})
    return result


def test_connection():
    try:
        _connect().close()
        return True
    except Exception:
        return False
