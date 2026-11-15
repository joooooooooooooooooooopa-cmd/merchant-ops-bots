"""Слой данных: сверка заявок и выплат Acme (read-only реплика, БД Core).

Поиск по external_id / merchant_order / invoice|withdrawal id / uuid (без дефисов),
а если по ID ничего нет — по реквизиту/телефону получателя (ILIKE, последние 60 дней).

Заявки (пай-ины) — pricing_invoice. Выплаты — pricing_withdrawalrequest.
Карточка помечается _kind: 'in' (заявка) / 'out' (выплата)."""
import os
import logging
from datetime import datetime

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)
ANALYTICS_DB_URL = os.getenv("ANALYTICS_DB_URL", "")

# ── ПАЙ-ИНЫ (invoice) ──────────────────────────────────────────
INV_METHOD = """COALESCE(
    pr.requisite_type,
    i.data->'route_result'->'terminal_data'->>'provider_payment_type',
    CASE
      WHEN t.name ILIKE '%%нспк%%' OR t.name ILIKE '%%nspk%%' OR t.name ILIKE '%%qr%%' THEN 'nspk'
      WHEN t.name ILIKE '%%havale%%' THEN 'havale'
      WHEN t.name ILIKE '%%transbor%%' OR t.name ILIKE '%%trans_am%%' THEN 'transborder'
      WHEN t.name ILIKE '%%c2c%%' THEN 'c2c'
      WHEN t.name ILIKE '%%ecom%%' THEN 'ecom'
      WHEN t.name ILIKE '%%sim%%' THEN 'sim'
      WHEN t.name ILIKE '%%card%%' THEN 'card'
      WHEN t.name ILIKE '%%sbp%%' OR t.name ILIKE '%%спб%%' THEN 'sbp'
    END
)"""

INV_REQUISITE = """COALESCE(
    pr.pan, pr.phone, pr.account_number,
    i.data->'beneficiary'->>'pan', i.data->'beneficiary'->>'phone',
    i.data->'beneficiary'->>'account_number', i.data->'beneficiary'->>'card',
    i.payment_phone, i.destination_pan
)"""

INV_PR_LATERAL = """LEFT JOIN LATERAL (
    SELECT pr.pan, pr.phone, pr.account_number, pr.name, pr.bank_name, pr.requisite_type
    FROM pricing_invoicerequisite ir
    JOIN pricing_paymentrequisite pr ON pr.id = ir.requisite_id
    WHERE ir.invoice_id = i.id LIMIT 1
) pr ON true"""

INV_SELECT = f"""
SELECT
  i.id::text   AS id,
  i.uuid::text AS uuid,
  i.external_id,
  i.merchant_order,
  (i.created_at AT TIME ZONE 'Europe/Moscow')                AS created_msk,
  (i.payment_status_updated_at AT TIME ZONE 'Europe/Moscow') AS status_upd_msk,
  s.iname  AS status,
  ROUND(i.price)       AS price,
  ROUND(i.amount_paid) AS amount_paid,
  'RUB'  AS currency,
  COALESCE(mr.brand_name, mr.iname, '—') AS merchant,
  p.name AS product,
  t.name AS terminal,
  g.iname AS gate,
  i.last_payment_error_code AS err_code,
  i.last_payment_error      AS err_msg,
  i.last_payment_error_orig AS err_orig,
  {INV_REQUISITE}                               AS requisite,
  COALESCE(pr.name, i.data->'beneficiary'->>'name')           AS recipient,
  COALESCE(pr.bank_name, i.data->'beneficiary'->>'bank_name') AS bank,
  i.data->'beneficiary'->>'deeplink'            AS deeplink,
  COALESCE(i.data->'route_result'->>'status','') = 'success' AS route_ok,
  EXISTS(SELECT 1 FROM pricing_invoicegatecall gc WHERE gc.invoice_id = i.id) AS has_gatecall,
  {INV_METHOD}                                  AS method,
  i.is_test
FROM pricing_invoice i
{INV_PR_LATERAL}
LEFT JOIN pricing_terminal      t  ON t.id  = i.primary_terminal_id
LEFT JOIN pricing_paymentgate   g  ON g.id  = t.payment_gate_id
LEFT JOIN pricing_paymentstatus s  ON s.id  = i.payment_status_id
LEFT JOIN pricing_product       p  ON p.id  = i.primary_product_id
LEFT JOIN pricing_merchant      mr ON mr.id = i.merchant_id
"""

INV_BY_ID = """
WHERE (
     lower(i.external_id) = lower(%(q)s)
  OR lower(i.merchant_order) = lower(%(q)s)
  OR replace(lower(i.merchant_order),'-','') = replace(lower(%(q)s),'-','')
  OR replace(lower(i.id::text),'-','')   = replace(lower(%(q)s),'-','')
  OR replace(lower(i.uuid::text),'-','') = replace(lower(%(q)s),'-','')
)
"""

INV_BY_REQUISITE = """
WHERE i.created_at > now() - interval '60 days' AND (
     i.payment_phone   ILIKE %(like)s
  OR i.destination_pan ILIKE %(like)s
  OR (i.data->'beneficiary'->>'phone')          ILIKE %(like)s
  OR (i.data->'beneficiary'->>'pan')            ILIKE %(like)s
  OR (i.data->'beneficiary'->>'account_number') ILIKE %(like)s
  OR EXISTS (
       SELECT 1 FROM pricing_invoicerequisite ir3
       JOIN pricing_paymentrequisite pr3 ON pr3.id = ir3.requisite_id
       WHERE ir3.invoice_id = i.id
         AND (pr3.pan ILIKE %(like)s OR pr3.phone ILIKE %(like)s OR pr3.account_number ILIKE %(like)s)
     )
)
"""

# ── ВЫПЛАТЫ (withdrawalrequest) ────────────────────────────────
WD_METHOD = """COALESCE(
    w.data->>'payment_method',
    CASE
      WHEN t.name ILIKE '%%havale%%' THEN 'havale'
      WHEN t.name ILIKE '%%card%%' THEN 'card'
      WHEN t.name ILIKE '%%sbp%%' THEN 'sbp'
      WHEN t.name ILIKE '%%c2c%%' THEN 'c2c'
    END
)"""

WD_REQUISITE = """COALESCE(
    w.data->'receiver_info'->>'receiver_account_number',
    NULLIF(regexp_replace(w.data->>'hashed_pan', '^[a-z]+:', ''), ''),
    w.data->>'phone_number',
    w.data->>'pan'
)"""

WD_RECIPIENT = """COALESCE(
    NULLIF(trim(concat_ws(' ', w.data->'receiver_info'->>'receiver_first_name',
                               w.data->'receiver_info'->>'receiver_last_name')), ''),
    NULLIF(trim(concat_ws(' ', w.data->>'first_name', w.data->>'last_name')), '')
)"""

WD_BANK = """COALESCE(
    w.data->>'beneficiary_bank_name',
    CASE WHEN jsonb_typeof(w.data->'beneficiary_bank') = 'string'
         THEN w.data->>'beneficiary_bank' END
)"""

WD_SELECT = f"""
SELECT
  w.id::text   AS id,
  w.uuid::text AS uuid,
  w.external_id,
  w.merchant_order,
  (w.created_at  AT TIME ZONE 'Europe/Moscow') AS created_msk,
  (w.finished_at AT TIME ZONE 'Europe/Moscow') AS status_upd_msk,
  w.status,
  ROUND(w.requested_amount) AS price,
  ROUND(w.amount)           AS amount_paid,
  w.currency_code_id AS currency,
  COALESCE(mr.brand_name, mr.iname, '—') AS merchant,
  p.name AS product,
  t.name AS terminal,
  g.iname AS gate,
  w.last_payment_error_code AS err_code,
  w.last_payment_error      AS err_msg,
  w.last_payment_error_orig AS err_orig,
  {WD_REQUISITE}  AS requisite,
  {WD_RECIPIENT}  AS recipient,
  {WD_BANK}       AS bank,
  {WD_METHOD}     AS method,
  w.is_test
FROM pricing_withdrawalrequest w
LEFT JOIN pricing_terminal    t  ON t.id  = w.primary_terminal_id
LEFT JOIN pricing_paymentgate g  ON g.id  = w.primary_payment_gate_id
LEFT JOIN pricing_product     p  ON p.id  = w.primary_product_id
LEFT JOIN pricing_merchant    mr ON mr.id = w.merchant_id
"""

WD_BY_ID = """
WHERE (
     lower(w.external_id) = lower(%(q)s)
  OR lower(w.merchant_order) = lower(%(q)s)
  OR replace(lower(w.merchant_order),'-','') = replace(lower(%(q)s),'-','')
  OR replace(lower(w.id::text),'-','')   = replace(lower(%(q)s),'-','')
  OR replace(lower(w.uuid::text),'-','') = replace(lower(%(q)s),'-','')
)
"""

WD_BY_REQUISITE = """
WHERE w.created_at > now() - interval '60 days' AND (
     (w.data->>'phone_number')                          ILIKE %(like)s
  OR (w.data->>'hashed_pan')                            ILIKE %(like)s
  OR (w.data->'receiver_info'->>'receiver_account_number') ILIKE %(like)s
  OR (w.data->>'pan')                                   ILIKE %(like)s
)
"""

# ── ПАКЕТНЫЙ поиск по списку ID (только по идентификаторам, без реквизитов) ──
INV_BY_IDS = """
WHERE (
     lower(i.external_id)                       = ANY(%(qs)s)
  OR lower(i.merchant_order)                    = ANY(%(qs)s)
  OR replace(lower(i.merchant_order),'-','')    = ANY(%(qsnd)s)
  OR replace(lower(i.id::text),'-','')          = ANY(%(qsnd)s)
  OR replace(lower(i.uuid::text),'-','')        = ANY(%(qsnd)s)
)
"""

WD_BY_IDS = """
WHERE (
     lower(w.external_id)                       = ANY(%(qs)s)
  OR lower(w.merchant_order)                    = ANY(%(qs)s)
  OR replace(lower(w.merchant_order),'-','')    = ANY(%(qsnd)s)
  OR replace(lower(w.id::text),'-','')          = ANY(%(qsnd)s)
  OR replace(lower(w.uuid::text),'-','')        = ANY(%(qsnd)s)
)
"""


def _connect():
    if not ANALYTICS_DB_URL:
        raise RuntimeError("ANALYTICS_DB_URL не задан (read-only реплика Core)")
    return psycopg2.connect(ANALYTICS_DB_URL, connect_timeout=10)


def _q(sql, params=None):
    conn = _connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET statement_timeout = 20000")
            cur.execute(sql, params or {})
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _tag(rows, kind):
    for r in rows:
        r["_kind"] = kind
    return rows


def _sort_key(r):
    return r.get("created_msk") or datetime.min


def search(query, limit=10):
    """Унифицированный поиск: сначала по ID (пай-ины+выплаты), потом — по реквизиту/телефону."""
    q = (query or "").strip()
    if not q:
        return []
    inv = _q(f"{INV_SELECT} {INV_BY_ID} ORDER BY i.created_at DESC LIMIT {int(limit)}", {"q": q})
    wd = _q(f"{WD_SELECT} {WD_BY_ID} ORDER BY w.created_at DESC LIMIT {int(limit)}", {"q": q})
    rows = _tag(inv, "in") + _tag(wd, "out")
    if rows:
        rows.sort(key=_sort_key, reverse=True)
        return rows[:limit]

    # fallback: поиск по реквизиту/телефону (только если запрос достаточно специфичен).
    # %/_ экранируем: иначе запрос вида «9%999» или «_____» — перебор чужих
    # реквизитов по маске + full-scan 60 дней
    if len(q) >= 5:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = "%" + esc + "%"
        inv = _q(f"{INV_SELECT} {INV_BY_REQUISITE} ORDER BY i.created_at DESC LIMIT {int(limit)}", {"like": like})
        wd = _q(f"{WD_SELECT} {WD_BY_REQUISITE} ORDER BY w.created_at DESC LIMIT {int(limit)}", {"like": like})
        rows = _tag(inv, "in") + _tag(wd, "out")
        rows.sort(key=_sort_key, reverse=True)
        return rows[:limit]
    return []


def search_many(queries, per_limit=400):
    """Пакетный лукап по списку ID (external_id / merchant_order / id / uuid).
    Один SQL по пай-инам + один по выплатам. Реквизитного фоллбэка тут нет —
    пакетный режим только по идентификаторам заявок. Возвращает плоский список строк."""
    qs, seen = [], set()
    for q in queries:
        ql = (q or "").strip().lower()
        if ql and ql not in seen:
            seen.add(ql)
            qs.append(ql)
    if not qs:
        return []
    qsnd = [q.replace("-", "") for q in qs]
    params = {"qs": qs, "qsnd": qsnd}
    inv = _q(f"{INV_SELECT} {INV_BY_IDS} ORDER BY i.created_at DESC LIMIT {int(per_limit)}", params)
    wd = _q(f"{WD_SELECT} {WD_BY_IDS} ORDER BY w.created_at DESC LIMIT {int(per_limit)}", params)
    rows = _tag(inv, "in") + _tag(wd, "out")
    rows.sort(key=_sort_key, reverse=True)
    return rows


def get_card(kind, item_id):
    """Карточка по точному id (uuid). kind: 'in' | 'out'."""
    if kind == "out":
        rows = _q(f"{WD_SELECT} WHERE w.id::text = %(q)s LIMIT 1", {"q": item_id})
        return _tag(rows, "out")[0] if rows else None
    rows = _q(f"{INV_SELECT} WHERE i.id::text = %(q)s LIMIT 1", {"q": item_id})
    return _tag(rows, "in")[0] if rows else None


def get_gate_calls(invoice_id, limit=20):
    """Попытки вызовов к гейту по пай-ину (у выплат таблицы попыток нет)."""
    sql = """
        SELECT (gc.created_at AT TIME ZONE 'Europe/Moscow') AS created_msk,
               gc.status, gc.error_code, gc.error_message, gc.duration,
               t.name AS terminal, g.iname AS gate
        FROM pricing_invoicegatecall gc
        LEFT JOIN pricing_terminal    t ON t.id = gc.terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE gc.invoice_id = %(q)s
        ORDER BY gc.created_at ASC
        LIMIT %(lim)s
    """
    return _q(sql, {"q": invoice_id, "lim": int(limit)})


_REQ_EXPR = ("COALESCE(i.payment_phone, i.data->'beneficiary'->>'phone', "
             "i.destination_pan, i.data->'beneficiary'->>'pan')")


def requisite_stats(invoice_id, days=3):
    """Статистика по реквизиту заявки за N дней: жив или нет (для саппорта одним кликом).
    Находит рек по invoice_id, считает все заявки на него."""
    sql = f"""
        WITH src AS (SELECT {_REQ_EXPR} AS req FROM pricing_invoice i WHERE i.id::text = %(q)s)
        SELECT (SELECT req FROM src) AS req,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) AS succ,
               COUNT(*) FILTER (WHERE i.payment_status_id = 4) AS failed,
               COUNT(DISTINCT i.customer_id) AS clients,
               (MAX(i.created_at) AT TIME ZONE 'Europe/Moscow') AS last_at
        FROM pricing_invoice i
        WHERE {_REQ_EXPR} = (SELECT req FROM src)
          AND i.created_at > now() - interval '{int(days)} days'
    """
    rows = _q(sql, {"q": invoice_id})
    return rows[0] if rows else None


def customer_orders(invoice_id, hours=24, limit=15):
    """Заявки клиента (по customer_id заявки) за N часов — история одного клиента."""
    sql = f"""
        WITH src AS (SELECT customer_id FROM pricing_invoice WHERE id::text = %(q)s)
        SELECT (i.created_at AT TIME ZONE 'Europe/Moscow') AS created_msk,
               ROUND(i.price) AS price, s.iname AS status,
               COALESCE(p.name,'—') AS product,
               i.merchant_order
        FROM pricing_invoice i
        LEFT JOIN pricing_paymentstatus s ON s.id = i.payment_status_id
        LEFT JOIN pricing_product p ON p.id = i.primary_product_id
        WHERE i.customer_id = (SELECT customer_id FROM src)
          AND i.created_at > now() - interval '{int(hours)} hours'
        ORDER BY i.created_at DESC
        LIMIT %(lim)s
    """
    return _q(sql, {"q": invoice_id, "lim": int(limit)})


def test_connection():
    try:
        _connect().close()
        return True
    except Exception:
        return False
