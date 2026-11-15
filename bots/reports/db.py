"""
Слой данных Acme (read-only реплика Core).

Все SQL-формулы выверены под модель Acme и портированы 1:1 из CRMBot
(`metabase.py`), чтобы цифры в любых отчётах **бились** с CRM-ботом и Metabase:

  • успех        = payment_status_id IN (3,6)   (complete + partial_complete)
  • выдача (ISS) = card-реквизит OR route_result.status='success' OR успех
  • EISS         = эффективная выдача (для потоков без выдачи берём total — иначе ложные 100%)
  • метод        = COALESCE(requisite_type, provider_payment_type, коридор-из-имени-продукта)
  • мерчант (in) = mr.iname (фильтр), отображение — COALESCE(brand_name, iname)
  • НЕ мерч-сторона: ВСЕ выгрузки фильтрованы NOTEST_FULL (is_test=false + блок тестовых мерчей)
  • время        = (created_at AT TIME ZONE 'Europe/Moscow')::date

ENV: ANALYTICS_DB_URL — read-only реплика, VPC-only (доступна только с ВПС Acme).
"""
import logging
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Tuple

import psycopg2
import psycopg2.extras

from config import ANALYTICS_DB_URL

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────
# SQL-фрагменты модели Acme (1:1 с CRMBot/metabase.py)
# ─────────────────────────────────────────────────────────────────
INV = "pricing_invoice"

REQJ = ("LEFT JOIN (SELECT DISTINCT invoice_id FROM pricing_invoicerequisite) reqj "
        "ON reqj.invoice_id = i.id")

ISS = ("(reqj.invoice_id IS NOT NULL "
       "OR COALESCE(i.data->'route_result'->>'status'='success', false) "
       "OR i.payment_status_id IN (3,6))")

ISS_SIGNAL = ("(reqj.invoice_id IS NOT NULL "
              "OR COALESCE(i.data->'route_result'->>'status'='success', false))")

EISS = (f"CASE WHEN COUNT(*) FILTER (WHERE {ISS_SIGNAL}) > 0 "
        f"THEN COUNT(*) FILTER (WHERE {ISS}) ELSE COUNT(*) END")

SUCC = "i.payment_status_id IN (3,6)"

MEXPR = ("COALESCE((SELECT pr2.requisite_type FROM pricing_invoicerequisite ir2 "
         "JOIN pricing_paymentrequisite pr2 ON pr2.id=ir2.requisite_id "
         "WHERE ir2.invoice_id=i.id LIMIT 1), "
         "i.data->'route_result'->'terminal_data'->>'provider_payment_type')")

PROD_METHOD = ("lower(regexp_replace(regexp_replace(p.name,"
               "'^[^_ ]+[_ ](?:RUB|TRY|KZT|UZS|EUR|USD|AMD|AZN|TJS)[_ ]',''),"
               "'([_ ](?:FTD|STD))?[_ ](?:In|Out|in|ln|IN|OUT)$',''))")

METHOD_EXPR = f"COALESCE({MEXPR}, NULLIF({PROD_METHOD}, ''))"

MERCHANT = "COALESCE(mr.brand_name, mr.iname, '—')"
MERJ = "LEFT JOIN pricing_merchant mr ON mr.id = i.merchant_id"

PRODJ = "LEFT JOIN pricing_product p ON p.id = i.primary_product_id"
ENDPOINT = "COALESCE(p.name, '—')"

TERMJ = "LEFT JOIN pricing_terminal t ON t.id = i.primary_terminal_id"
GATEJ_FROM_TERM = "LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id"
GATEJ_DIRECT = "LEFT JOIN pricing_paymentgate g ON g.id = i.primary_payment_gate_id"

# currency_code_id уже хранит ISO-код ('RUB','TRY','USD'…) строкой — JOIN не нужен.
CURJ = ""

# Двойной фильтр тестов (см. test-filter note): is_test=false +
# исключение мерчей с 'test' в имени/бренде. Использовать ВЕЗДЕ.
# Проверяются ОБА имени: у delta-test/bravo-test brand_name «чистый» (Delta),
# и COALESCE(brand_name, iname) такие мерчи пропускал.
NOTEST_FULL = ("i.is_test = false "
               "AND i.merchant_id NOT IN ("
               "SELECT id FROM pricing_merchant "
               "WHERE iname ILIKE %(_test_pat)s "
               "OR COALESCE(brand_name, '') ILIKE %(_test_pat)s)")

# Статусы paymentstatus (FK на pricing_paymentstatus).
STATUS_LABELS = {
    1: "approved",
    2: "pending",
    3: "complete",       # успех
    4: "failed",         # отказ
    5: "new",
    6: "partial_complete",  # тоже успех
}

# Маппинг кодов поведения юзеров (см. memory: behavior_by_endpoint).
USER_CODES = ("CANCELLED", "validation_error", "SESSION_EXPIRED")
ANTIFRAUD_CODES = ("ROUTING_ERROR",)
TECH_CODES = ("COMMON", "COMMON_ERROR", "provider_error", "provider_request_timeout")


# ─────────────────────────────────────────────────────────────────
# Соединение
# ─────────────────────────────────────────────────────────────────
def _conn():
    if not ANALYTICS_DB_URL:
        raise RuntimeError("ANALYTICS_DB_URL не задан — нужна read-only реплика Core")
    return psycopg2.connect(ANALYTICS_DB_URL, connect_timeout=10)


def _q(sql: str, params: Optional[dict] = None) -> List[dict]:
    """Выполнить SELECT, вернуть list[dict]. params — ВСЕГДА dict-стиль (%(name)s),
    чтобы '%' в строках/паттернах не интерпретировался psycopg2 (см. CRM-баги)."""
    params = dict(params or {})
    params.setdefault("_test_pat", "%test%")
    conn = None
    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET statement_timeout = 60000")
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"DB query failed: {e}\nSQL: {sql[:500]}")
        raise
    finally:
        if conn:
            conn.close()


def replica_healthy() -> bool:
    try:
        _q("SELECT 1 AS ok")
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────
# Период МСК → UTC-границы для created_at
# (created_at — timestamptz, сервер UTC; нам надо «сутки МСК»).
# date_from и date_to ВКЛЮЧИТЕЛЬНЫЕ (как мерч ожидает: «5–7 июня» = 3 дня).
# ─────────────────────────────────────────────────────────────────
def date_range_filter() -> str:
    """SQL-условие на интервал created_at в МСК-сутках.
    Привязано к плейсхолдерам %(_d_from)s / %(_d_to)s (даты включительно)."""
    return ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
            "BETWEEN %(_d_from)s AND %(_d_to)s")


def date_params(d_from: date, d_to: date) -> dict:
    return {"_d_from": d_from, "_d_to": d_to}


# ─────────────────────────────────────────────────────────────────
# 1. Списки сущностей для пикеров
# ─────────────────────────────────────────────────────────────────
def list_merchants(days: int = 30) -> List[dict]:
    """Мерчанты с трафиком за N дней. Возвращает [{iname, name, cnt}], iname-NN-null отброшены."""
    return _q(f"""
        SELECT mr.iname AS iname,
               COALESCE(mr.brand_name, mr.iname) AS name,
               COUNT(*) AS cnt
        FROM {INV} i {MERJ}
        WHERE i.created_at >= now() - interval '{int(days)} days'
          AND {NOTEST_FULL}
          AND mr.iname IS NOT NULL
        GROUP BY mr.iname, mr.brand_name
        HAVING COUNT(*) > 0
        ORDER BY cnt DESC, name ASC
    """)


def list_gates(days: int = 30) -> List[dict]:
    """Гейты-провайдеры с трафиком за N дней. Возвращает [{iname, cnt}]."""
    return _q(f"""
        SELECT g.iname AS iname, COUNT(*) AS cnt
        FROM {INV} i {TERMJ} {GATEJ_FROM_TERM}
        WHERE i.created_at >= now() - interval '{int(days)} days'
          AND {NOTEST_FULL}
          AND g.iname IS NOT NULL
        GROUP BY g.iname
        ORDER BY cnt DESC, g.iname ASC
    """)


def resolve_merchant(iname_or_query: str) -> Optional[dict]:
    """Точное iname → строка; иначе ищем по подстроке iname/brand_name."""
    q = (iname_or_query or "").strip().lower()
    if not q:
        return None
    rows = _q("""
        SELECT mr.iname AS iname,
               COALESCE(mr.brand_name, mr.iname) AS name
        FROM pricing_merchant mr
        WHERE LOWER(mr.iname) = %(q)s
           OR LOWER(COALESCE(mr.brand_name, '')) = %(q)s
        LIMIT 1
    """, {"q": q})
    if rows:
        return rows[0]
    rows = _q("""
        SELECT mr.iname AS iname,
               COALESCE(mr.brand_name, mr.iname) AS name
        FROM pricing_merchant mr
        WHERE COALESCE(mr.iname, '') ILIKE %(p)s
           OR COALESCE(mr.brand_name, '') ILIKE %(p)s
        ORDER BY COALESCE(mr.brand_name, mr.iname)
        LIMIT 1
    """, {"p": f"%{q}%"})
    return rows[0] if rows else None


def resolve_gate(iname: str) -> Optional[dict]:
    rows = _q("""
        SELECT g.iname AS iname
        FROM pricing_paymentgate g
        WHERE LOWER(g.iname) = LOWER(%(q)s)
        LIMIT 1
    """, {"q": iname or ""})
    return rows[0] if rows else None


# ─────────────────────────────────────────────────────────────────
# 2. Транзакции (потранзакционка)
# ─────────────────────────────────────────────────────────────────
# Колонки берём всегда «полным набором», фильтр для аудитории — в builder.py
# (так одна функция == один источник истины, исключаем расхождения).
_TX_SELECT = f"""
    i.id::text                                       AS uuid,
    i.merchant_order                                 AS merchant_order,
    i.external_id                                    AS external_id,
    (i.created_at AT TIME ZONE 'Europe/Moscow')::timestamp        AS created_at_msk,
    CASE WHEN i.payment_status_updated_at IS NOT NULL
         THEN (i.payment_status_updated_at AT TIME ZONE 'Europe/Moscow')::timestamp
    END                                              AS finalized_at_msk,
    {MERCHANT}                                       AS merchant,
    mr.iname                                         AS merchant_iname,
    {ENDPOINT}                                       AS product,
    COALESCE({METHOD_EXPR}, 'не определён')          AS method,
    COALESCE(g.iname, '—')                           AS gate,
    COALESCE(t.name, '—')                            AS terminal,
    UPPER(COALESCE(i.currency_code_id, ''))                    AS currency,
    i.price                                          AS price,
    i.amount_paid                                    AS amount_paid,
    i.payment_status_id                              AS status_id,
    COALESCE(i.last_payment_error_code, '')          AS error_code,
    COALESCE(i.last_payment_error_orig, '')          AS error_text,
    (i.data->'fees'->>'fee_from_merchant')::numeric  AS fee_from_merchant,
    ((i.data->'beneficiary'->>'deeplink') IS NOT NULL) AS req_issued,
    {ISS_SIGNAL}                                     AS iss_signal
"""

_TX_FROM = f"{INV} i {REQJ} {MERJ} {PRODJ} {TERMJ} {GATEJ_FROM_TERM} {CURJ}"


def fetch_transactions(scope: str,
                       key: Optional[str],
                       d_from: date,
                       d_to: date,
                       limit: Optional[int] = None) -> List[dict]:
    """
    Тянет потранзакционку за период.

    scope: 'merchant' (key = iname), 'gate' (key = gate iname), 'all' (key игнор).
    """
    params = date_params(d_from, d_to)
    where = [date_range_filter(), NOTEST_FULL]

    if scope == "merchant":
        where.append("mr.iname = %(key)s")
        params["key"] = key
    elif scope == "gate":
        where.append("g.iname = %(key)s")
        params["key"] = key
    elif scope == "all":
        pass
    else:
        raise ValueError(f"unknown scope: {scope}")

    sql = f"""
        SELECT {_TX_SELECT}
        FROM {_TX_FROM}
        WHERE {' AND '.join(where)}
        ORDER BY i.created_at DESC
        {f'LIMIT {int(limit)}' if limit else ''}
    """
    return _q(sql, params)


# ─────────────────────────────────────────────────────────────────
# 3. Сводка (KPI) — с разбивкой по валютам (мерчи бывают мульти-валютные)
# ─────────────────────────────────────────────────────────────────
def summary_kpi(scope: str, key: Optional[str], d_from: date, d_to: date) -> List[dict]:
    """KPI по валютам. Используем EISS через под-агрегат (как get_main_dashboard),
    чтобы потоки без выдачи (Havale) не давали ложных 100%."""
    params = date_params(d_from, d_to)
    where = [date_range_filter(), NOTEST_FULL]
    if scope == "merchant":
        where.append("mr.iname = %(key)s")
        params["key"] = key
    elif scope == "gate":
        where.append("g.iname = %(key)s")
        params["key"] = key

    sql = f"""
      SELECT cur,
             SUM(total)             AS total,
             SUM(success)           AS success,
             SUM(eff_issued)        AS eff_issued,
             SUM(real_issued)       AS real_issued,
             SUM(total - eff_issued) AS not_issued,
             ROUND(100.0*SUM(eff_issued)/NULLIF(SUM(total),0),2)   AS conv_to_issue,
             ROUND(100.0*SUM(success)/NULLIF(SUM(eff_issued),0),2) AS conv_to_pay,
             ROUND(100.0*SUM(success)/NULLIF(SUM(total),0),2)      AS conv_total,
             SUM(price_sum)         AS price_sum,
             SUM(paid_sum)          AS paid_sum
      FROM (
        SELECT UPPER(COALESCE(i.currency_code_id,'?'))                          AS cur,
               COUNT(*)                                               AS total,
               COUNT(*) FILTER (WHERE {SUCC})                         AS success,
               ({EISS})                                               AS eff_issued,
               COUNT(*) FILTER (WHERE {ISS_SIGNAL})                   AS real_issued,
               COALESCE(SUM(i.price),0)                               AS price_sum,
               COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}),0)   AS paid_sum
        FROM {_TX_FROM}
        WHERE {' AND '.join(where)}
        GROUP BY 1, i.merchant_id, i.primary_product_id
      ) t
      GROUP BY cur
      ORDER BY total DESC
    """
    return _q(sql, params)


def summary_by_method(scope: str, key: Optional[str], d_from: date, d_to: date) -> List[dict]:
    """Разбивка по методам (с EISS-учётом). Возвращает rows с total/success/eff/paid/conv."""
    params = date_params(d_from, d_to)
    where = [date_range_filter(), NOTEST_FULL]
    if scope == "merchant":
        where.append("mr.iname = %(key)s")
        params["key"] = key
    elif scope == "gate":
        where.append("g.iname = %(key)s")
        params["key"] = key

    sql = f"""
      SELECT method, UPPER(COALESCE(cur,'?')) AS currency,
             SUM(total)        AS total,
             SUM(success)      AS success,
             SUM(eff_issued)   AS eff_issued,
             ROUND(100.0*SUM(success)/NULLIF(SUM(eff_issued),0),2) AS conv_to_pay,
             ROUND(100.0*SUM(success)/NULLIF(SUM(total),0),2)      AS conv_total,
             SUM(paid_sum)     AS paid_sum
      FROM (
        SELECT COALESCE({METHOD_EXPR},'не определён') AS method,
               UPPER(COALESCE(i.currency_code_id,'?'))                       AS cur,
               COUNT(*)                                            AS total,
               COUNT(*) FILTER (WHERE {SUCC})                      AS success,
               ({EISS})                                            AS eff_issued,
               COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}),0) AS paid_sum
        FROM {_TX_FROM}
        WHERE {' AND '.join(where)}
        GROUP BY 1, 2, i.merchant_id, i.primary_product_id
      ) t
      GROUP BY method, currency
      ORDER BY SUM(total) DESC
    """
    return _q(sql, params)


def summary_by_product(scope: str, key: Optional[str], d_from: date, d_to: date) -> List[dict]:
    """Разбивка по продукту (= эндпоинту). С EISS, чтобы Havale-потоки не лгали."""
    params = date_params(d_from, d_to)
    where = [date_range_filter(), NOTEST_FULL]
    if scope == "merchant":
        where.append("mr.iname = %(key)s")
        params["key"] = key
    elif scope == "gate":
        where.append("g.iname = %(key)s")
        params["key"] = key

    sql = f"""
      SELECT product, UPPER(COALESCE(cur,'?')) AS currency,
             SUM(total)        AS total,
             SUM(success)      AS success,
             SUM(eff_issued)   AS eff_issued,
             ROUND(100.0*SUM(success)/NULLIF(SUM(eff_issued),0),2) AS conv_to_pay,
             ROUND(100.0*SUM(success)/NULLIF(SUM(total),0),2)      AS conv_total,
             SUM(paid_sum)     AS paid_sum
      FROM (
        SELECT {ENDPOINT}                                          AS product,
               UPPER(COALESCE(i.currency_code_id,'?'))                       AS cur,
               COUNT(*)                                            AS total,
               COUNT(*) FILTER (WHERE {SUCC})                      AS success,
               ({EISS})                                            AS eff_issued,
               COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}),0) AS paid_sum
        FROM {_TX_FROM}
        WHERE {' AND '.join(where)}
        GROUP BY 1, 2, i.primary_product_id
      ) t
      GROUP BY product, currency
      ORDER BY SUM(total) DESC
    """
    return _q(sql, params)


def summary_by_hour(scope: str, key: Optional[str], d_from: date, d_to: date) -> List[dict]:
    """Почасовая разбивка (агрегат по часу МСК, через все дни периода)."""
    params = date_params(d_from, d_to)
    where = [date_range_filter(), NOTEST_FULL]
    if scope == "merchant":
        where.append("mr.iname = %(key)s")
        params["key"] = key
    elif scope == "gate":
        where.append("g.iname = %(key)s")
        params["key"] = key

    sql = f"""
      SELECT hour,
             SUM(total)      AS total,
             SUM(success)    AS success,
             SUM(eff_issued) AS eff_issued,
             ROUND(100.0*SUM(success)/NULLIF(SUM(eff_issued),0),2) AS conv_to_pay,
             ROUND(100.0*SUM(success)/NULLIF(SUM(total),0),2)      AS conv_total,
             SUM(paid_sum)   AS paid_sum
      FROM (
        SELECT EXTRACT(HOUR FROM (i.created_at AT TIME ZONE 'Europe/Moscow'))::int AS hour,
               COUNT(*)                                            AS total,
               COUNT(*) FILTER (WHERE {SUCC})                      AS success,
               ({EISS})                                            AS eff_issued,
               COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}),0) AS paid_sum
        FROM {_TX_FROM}
        WHERE {' AND '.join(where)}
        GROUP BY 1, i.merchant_id, i.primary_product_id
      ) t
      GROUP BY hour
      ORDER BY hour
    """
    return _q(sql, params)


def summary_by_gate(scope: str, key: Optional[str], d_from: date, d_to: date) -> List[dict]:
    """Для отчёта провайдера/внутреннего: разбивка по терминалам гейта."""
    params = date_params(d_from, d_to)
    where = [date_range_filter(), NOTEST_FULL]
    if scope == "merchant":
        where.append("mr.iname = %(key)s")
        params["key"] = key
    elif scope == "gate":
        where.append("g.iname = %(key)s")
        params["key"] = key

    sql = f"""
      SELECT gate, terminal, UPPER(COALESCE(cur,'?')) AS currency,
             SUM(total)      AS total,
             SUM(success)    AS success,
             SUM(eff_issued) AS eff_issued,
             ROUND(100.0*SUM(success)/NULLIF(SUM(eff_issued),0),2) AS conv_to_pay,
             SUM(paid_sum)   AS paid_sum
      FROM (
        SELECT COALESCE(g.iname,'—') AS gate,
               COALESCE(t.name,'—')  AS terminal,
               UPPER(COALESCE(i.currency_code_id,'?')) AS cur,
               COUNT(*)                                            AS total,
               COUNT(*) FILTER (WHERE {SUCC})                      AS success,
               ({EISS})                                            AS eff_issued,
               COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}),0) AS paid_sum
        FROM {_TX_FROM}
        WHERE {' AND '.join(where)}
        GROUP BY 1, 2, 3, i.merchant_id, i.primary_product_id
      ) t
      GROUP BY gate, terminal, currency
      ORDER BY SUM(total) DESC
    """
    return _q(sql, params)


# ─────────────────────────────────────────────────────────────────
# 4. По дням (для диапазона). Стандарт owner: Дата|Заявок|Опл|Конв%|Оборот
# ─────────────────────────────────────────────────────────────────
def by_days(scope: str, key: Optional[str], d_from: date, d_to: date) -> List[dict]:
    """Разбивка по календарным дням МСК. Конверсия здесь — success/total (как мерчу понятно)."""
    params = date_params(d_from, d_to)
    where = [date_range_filter(), NOTEST_FULL]
    if scope == "merchant":
        where.append("mr.iname = %(key)s")
        params["key"] = key
    elif scope == "gate":
        where.append("g.iname = %(key)s")
        params["key"] = key

    sql = f"""
      SELECT day::date AS day,
             SUM(total) AS total,
             SUM(success) AS success,
             ROUND(100.0*SUM(success)/NULLIF(SUM(total),0),2) AS conv,
             SUM(paid_sum) AS paid_sum,
             UPPER(COALESCE(cur,'?')) AS currency
      FROM (
        SELECT (i.created_at AT TIME ZONE 'Europe/Moscow')::date AS day,
               UPPER(COALESCE(i.currency_code_id,'?')) AS cur,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}),0) AS paid_sum
        FROM {_TX_FROM}
        WHERE {' AND '.join(where)}
        GROUP BY 1, 2
      ) t
      GROUP BY day, currency
      ORDER BY day, currency
    """
    return _q(sql, params)


# ─────────────────────────────────────────────────────────────────
# 5. Анализ причин (поведение)
# ─────────────────────────────────────────────────────────────────
def behavior(scope: str, key: Optional[str], d_from: date, d_to: date) -> dict:
    """Бакеты исходов: paid / user_bad / antifraud / tech / pending.
    Возвращает {'totals': {...}, 'top_codes': [{code, gate, n, share}], 'by_endpoint': [...]}.
    """
    params = date_params(d_from, d_to)
    where = [date_range_filter(), NOTEST_FULL]
    if scope == "merchant":
        where.append("mr.iname = %(key)s")
        params["key"] = key
    elif scope == "gate":
        where.append("g.iname = %(key)s")
        params["key"] = key

    where_sql = " AND ".join(where)

    totals = _q(f"""
      SELECT COUNT(*) AS total,
             COUNT(*) FILTER (WHERE {SUCC}) AS paid,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code = 'CANCELLED') AS closed_form,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code IN ('validation_error','SESSION_EXPIRED')) AS idle,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code = 'ROUTING_ERROR') AS antifraud,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code IN ('COMMON','COMMON_ERROR','provider_error','provider_request_timeout')) AS tech,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code IS NULL) AS pending
      FROM {_TX_FROM}
      WHERE {where_sql}
    """, params)
    t = totals[0] if totals else {}

    top_codes = _q(f"""
      SELECT COALESCE(NULLIF(i.last_payment_error_code,''),'(пусто)') AS code,
             COALESCE(g.iname,'—') AS gate,
             COUNT(*) AS n
      FROM {_TX_FROM}
      WHERE {where_sql} AND NOT ({SUCC})
      GROUP BY 1, 2
      ORDER BY n DESC
      LIMIT 15
    """, params)

    by_endpoint = _q(f"""
      SELECT {MERCHANT} AS merchant, COALESCE(p.name,'—') AS endpoint,
             COUNT(*) AS total,
             COUNT(*) FILTER (WHERE {SUCC}) AS paid,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code = 'CANCELLED') AS closed_form,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code IN ('validation_error','SESSION_EXPIRED')) AS idle,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code = 'ROUTING_ERROR') AS antifraud,
             COUNT(*) FILTER (WHERE NOT ({SUCC}) AND i.last_payment_error_code IN ('COMMON','COMMON_ERROR','provider_error','provider_request_timeout')) AS tech
      FROM {_TX_FROM}
      WHERE {where_sql}
      GROUP BY 1, 2
      HAVING COUNT(*) >= 10
      ORDER BY total DESC
      LIMIT 40
    """, params)

    return {"totals": t, "top_codes": top_codes, "by_endpoint": by_endpoint}


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def status_label(status_id: Optional[int]) -> str:
    if status_id is None:
        return "—"
    return STATUS_LABELS.get(int(status_id), f"#{status_id}")


def is_success_status(status_id: Optional[int]) -> bool:
    return status_id in (3, 6)
