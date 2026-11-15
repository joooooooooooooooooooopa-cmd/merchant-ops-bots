"""
Слой данных — ACME (прямой доступ к read-only реплике PostgreSQL, БД Core).
Прямой доступ к реплике (Metabase из цепочки убран). Имя модуля сохранено (`metabase`),
чтобы остальной бот (`import metabase as mb`) работал без правок.

Контракт (формы возврата) сохранён 1:1 с прежней версией, поэтому
handlers/formatters/jobs работают без изменений.

ENV:
  ANALYTICS_DB_URL = postgresql://user:pass@host:port/dbname   (read-only реплика Core)

Модель Acme (БД Core):
  заявка        = pricing_invoice (i)
  успех         = payment_status_id IN (3,6)   (complete + partial_complete)
  выдача        = есть card-реквизит ИЛИ route_result.status='success' ИЛИ успех
  метод         = COALESCE(requisite_type, route_result.terminal_data.provider_payment_type)
  мерчант       = COALESCE(merchant.brand_name, merchant.iname)
  сегодня (МСК) = (created_at AT TIME ZONE 'Europe/Moscow')::date = (now() AT TIME ZONE 'Europe/Moscow')::date
  всегда is_test = false
"""
import os
import logging
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

ANALYTICS_DB_URL = os.getenv("ANALYTICS_DB_URL", "")

# ── SQL-фрагменты модели Acme ───────────────────────────────
INV = "pricing_invoice"
REQJ = ("LEFT JOIN (SELECT DISTINCT invoice_id FROM pricing_invoicerequisite) reqj "
        "ON reqj.invoice_id = i.id")
ISS = ("(reqj.invoice_id IS NOT NULL "
       "OR COALESCE(i.data->'route_result'->>'status'='success', false) "
       "OR i.payment_status_id IN (3,6))")              # выдача
# Реальный признак выдачи (БЕЗ success-fallback): card-реквизит или route success.
# Нужен, чтобы отличить потоки без выдачи (Havale) — там ISS схлопывается в успех → ложные 100%.
ISS_SIGNAL = ("(reqj.invoice_id IS NOT NULL "
              "OR COALESCE(i.data->'route_result'->>'status'='success', false))")
# Эффективная "выдача" (знаменатель конверсии в оплату), агрегатное выражение для GROUP BY:
# если у группы есть реальный признак выдачи → берём выдачу, иначе → ВСЕГО заявок (success/total).
EISS = (f"CASE WHEN COUNT(*) FILTER (WHERE {ISS_SIGNAL}) > 0 "
        f"THEN COUNT(*) FILTER (WHERE {ISS}) ELSE COUNT(*) END")
SUCC = "i.payment_status_id IN (3,6)"                   # успех
MEXPR = ("COALESCE((SELECT pr2.requisite_type FROM pricing_invoicerequisite ir2 "
         "JOIN pricing_paymentrequisite pr2 ON pr2.id=ir2.requisite_id "
         "WHERE ir2.invoice_id=i.id LIMIT 1), "
         "i.data->'route_result'->'terminal_data'->>'provider_payment_type')")
# Метод из ИМЕНИ ПРОДУКТА (коридор) — fallback, чтобы не было «не определён» (требует JOIN PRODJ → p).
PROD_METHOD = ("lower(regexp_replace(regexp_replace(p.name,"
               "'^[^_ ]+[_ ](?:RUB|TRY|KZT|UZS|EUR|USD|AMD|AZN|TJS)[_ ]',''),"
               "'([_ ](?:FTD|STD))?[_ ](?:In|Out|in|ln|IN|OUT)$',''))")
# Метод заявки: реквизит → provider_payment_type → коридор из имени продукта (требует PRODJ).
METHOD_EXPR = f"COALESCE({MEXPR}, NULLIF({PROD_METHOD}, ''))"
MERCHANT = "COALESCE(mr.brand_name, mr.iname, '—')"
MERJ = "LEFT JOIN pricing_merchant mr ON mr.id = i.merchant_id"
TODAY = ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
         "= (now() AT TIME ZONE 'Europe/Moscow')::date")
YESTERDAY = ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
             "= (now() AT TIME ZONE 'Europe/Moscow')::date - 1")
NOTEST = "i.is_test = false"
# NOTEST_FULL — двухуровневый фильтр тестов: внутренний флаг + блок тестовых мерчей по имени.
# Самодостаточная константа (не требует {MERJ} в FROM): использует NOT IN subquery.
# Использовать ВЕЗДЕ в боте/алертах. Без неё в дашборд протекали BDM_Test, internal-test.
# Проверяются ОБА имени: у delta-test/bravo-test и т.п. brand_name «чистый» (Delta),
# и COALESCE(brand_name, iname) такие мерчи пропускал.
NOTEST_FULL = ("i.is_test = false "
               "AND i.merchant_id NOT IN ("
               "SELECT id FROM pricing_merchant "
               "WHERE iname ILIKE '%%test%%' "
               "OR COALESCE(brand_name, '') ILIKE '%%test%%')")
MSK_HOUR = "EXTRACT(HOUR FROM (i.created_at AT TIME ZONE 'Europe/Moscow'))::int"
PRODJ = "LEFT JOIN pricing_product p ON p.id = i.primary_product_id"   # эндпоинт = продукт
ENDPOINT = "COALESCE(p.name, '—')"
# Ключ реквизита (КУДА уходят деньги): бенефициар-телефон → карта-получатель → бенефициар-pan.
# payment_phone — ПОСЛЕДНИМ фолбэком: он заполнен только у Transborder_AM и там это телефон
# ПЛАТЕЛЬЩИКА, не реквизит (раньше стоял первым → один плательщик с 7 фейлами
# выглядел как «сдохший реквизит со стриком 7», хотя бенефициар у каждой заявки был свой).
REQKEY = ("COALESCE(i.data->'beneficiary'->>'phone', i.destination_pan, "
          "i.data->'beneficiary'->>'pan', i.payment_phone)")


# ── Подключение / выполнение ───────────────────────────────────
def _conn():
    if not ANALYTICS_DB_URL:
        raise RuntimeError("ANALYTICS_DB_URL не задан (read-only реплика Core)")
    return psycopg2.connect(ANALYTICS_DB_URL, connect_timeout=10)


def _q(sql, params=None, _retries=1):
    """Выполнить SELECT, вернуть list[dict]. Ретрай на транзиентном обрыве соединения.
    ВАЖНО: при ошибке возвращает [] — джобы НЕ должны трактовать пустой результат как
    «нет проблем» при недоступной реплике; для детекта недоступности — replica_healthy()."""
    conn = None
    try:
        conn = _conn()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET statement_timeout = 30000")
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]
    except psycopg2.OperationalError as e:
        if _retries > 0:
            logger.warning(f"DB operational error, retry: {e}")
            return _q(sql, params, _retries - 1)
        logger.error(f"DB query failed (operational): {e}")
        return []
    except Exception as e:
        logger.error(f"DB query failed: {e}")
        return []
    finally:
        if conn:
            conn.close()


def replica_healthy() -> bool:
    """Отвечает ли реплика (для алерта о недоступности данных в мониторинге)."""
    conn = None
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 5000")
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception as e:
        logger.error(f"replica health check failed: {e}")
        return False
    finally:
        if conn:
            conn.close()


def execute_question_raw_sql(sql):
    """Совместимость со старым интерфейсом (get_payout_stats и т.п.)."""
    return _q(sql)


def test_connection() -> bool:
    try:
        return bool(_q("SELECT 1 AS ok"))
    except Exception:
        return False


# ── 1. Конверсия за сегодня по мерчантам (#76) ─────────────────
def get_conversion_today():
    rows = _q(f"""
        SELECT {MERCHANT} AS "Мерчант",
               COUNT(*) AS "Всего",
               ({EISS}) AS "С реквизитами",
               COUNT(*) FILTER (WHERE {SUCC}) AS "Успешных",
               COALESCE(ROUND(SUM(i.price)),0) AS "Сумма",
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})),0) AS "Сумма успешных",
               ROUND(100.0*COUNT(*) FILTER (WHERE {SUCC})
                     /NULLIF(({EISS}),0),2) AS "Конверсия в оплату %"
        FROM {INV} i {REQJ} {MERJ}
        WHERE {TODAY} AND {NOTEST_FULL}
        GROUP BY 1 ORDER BY "Всего" DESC
    """)
    # back-compat: часть хендлеров читает старый ключ "Конверсия %"
    for r in rows:
        r["Конверсия %"] = r["Конверсия в оплату %"]
    if rows:
        tot = sum(r["Всего"] for r in rows)
        # ИТОГО-выдача = сумма эффективной выдачи по мерчантам (учитывает потоки без выдачи → total)
        req = sum((r["С реквизитами"] or 0) for r in rows)
        suc = sum(r["Успешных"] for r in rows)
        conv_total = round(100.0 * suc / req, 2) if req else 0
        rows.append({
            "Мерчант": "📊 ИТОГО",
            "Всего": tot,
            "С реквизитами": req,
            "Успешных": suc,
            "Сумма": sum(float(r["Сумма"] or 0) for r in rows),
            "Сумма успешных": sum(float(r["Сумма успешных"] or 0) for r in rows),
            "Конверсия в оплату %": conv_total,
            "Конверсия %": conv_total,
        })
    return rows


# ── 2. Главный дашборд (#82) ───────────────────────────────────
def get_main_dashboard():
    # Считаем эффективную выдачу ПО ПОТОКАМ (мерчант+продукт), затем суммируем —
    # чтобы потоки без выдачи (Havale) считались по total, а не давали ложные 100%.
    rows = _q(f"""
        SELECT SUM(total) AS "Всего заявок",
               SUM(success) AS "Успешных заявок",
               SUM(total - eff_issued) AS "Невыдано реквизитов",
               ROUND(100.0*SUM(eff_issued)/NULLIF(SUM(total),0),2) AS "Конверсия в реквизиты %",
               ROUND(100.0*SUM(success)/NULLIF(SUM(eff_issued),0),2) AS "Конверсия в оплату %",
               SUM(paid_sum) AS "Сумма успешных"
        FROM (
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE {SUCC}) AS success,
                   ({EISS}) AS eff_issued,
                   COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})),0) AS paid_sum
            FROM {INV} i {REQJ}
            WHERE {TODAY} AND {NOTEST_FULL}
            GROUP BY i.merchant_id, i.primary_product_id
        ) t
    """)
    return rows[0] if rows else {}


# ── 3. Конверсия по методам (#77) ──────────────────────────────
def get_conversion_by_method():
    return _q(f"""
        SELECT COALESCE({METHOD_EXPR},'не определён') AS "Метод",
               COUNT(*) AS "Всего",
               COUNT(*) FILTER (WHERE {SUCC}) AS "Успешных",
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})),0) AS "Сумма",
               ROUND(100.0*COUNT(*) FILTER (WHERE {SUCC})
                     /NULLIF(({EISS}),0),2) AS "Конверсия %"
        FROM {INV} i {REQJ} {PRODJ}
        WHERE {TODAY} AND {NOTEST_FULL}
        GROUP BY 1 ORDER BY "Всего" DESC
    """)


# ── 4. Почасовая статистика (#78) ──────────────────────────────
def get_hourly_stats():
    return _q(f"""
        SELECT {MSK_HOUR} AS "Час",
               COUNT(*) AS "Ордеров",
               COUNT(*) FILTER (WHERE {SUCC}) AS "Успешных",
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})),0) AS "Сумма",
               ROUND(100.0*COUNT(*) FILTER (WHERE {SUCC})
                     /NULLIF(({EISS}),0),2) AS "Конверсия %"
        FROM {INV} i {REQJ}
        WHERE {TODAY} AND {NOTEST_FULL}
        GROUP BY 1 ORDER BY 1
    """)


# ── 5. Неоплаченные (реквизит выдан, не оплачен) с external_id (#95) ──
def get_unpaid_orders():
    # ISS_SIGNAL вместо ISS — у нас status=4 (failed), success-fallback в ISS бесполезен,
    # но создаёт ложное впечатление «success+failed». Семантика та же, читается без подвоха.
    # merchant_order/uuid вместо external_id: примеры идут в сообщения для пересылки мерчу,
    # а external_id — провайдерская сторона (OUTPUT), мерч по нему заявку не найдёт
    # (и показывать его мерчу нельзя).
    rows = _q(f"""
        SELECT {MERCHANT} AS merchant_name,
               COALESCE(i.merchant_order, i.uuid::text) AS order_id,
               COALESCE({MEXPR},'') AS method,
               p.name AS endpoint
        FROM {INV} i {REQJ} {MERJ} {PRODJ}
        WHERE {TODAY} AND {NOTEST_FULL}
          AND i.payment_status_id = 4 AND {ISS_SIGNAL}
        ORDER BY i.created_at DESC
    """)
    result = {}
    for r in rows:
        m = r.get("merchant_name") or "—"
        result.setdefault(m, []).append({
            "order_id": r.get("order_id") or "",
            "method": r.get("method") or "",
            "endpoint": r.get("endpoint") or "",
        })
    return result


# ── 6. Конверсия по мерчант+метод (#256) ───────────────────────
def get_conv_by_merchant_method():
    rows = _q(f"""
        SELECT {MERCHANT} AS merchant_name,
               COALESCE({METHOD_EXPR},'не определён') AS method,
               COUNT(*) AS total,
               ({EISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS successful,
               ROUND(100.0*COUNT(*) FILTER (WHERE {SUCC})
                     /NULLIF(({EISS}),0),1) AS conv_pct
        FROM {INV} i {REQJ} {MERJ} {PRODJ}
        WHERE {TODAY} AND {NOTEST_FULL}
        GROUP BY 1,2 ORDER BY total DESC
    """)
    result = {}
    for r in rows:
        name = r.get("merchant_name", "")
        if name:
            result.setdefault(name, []).append({
                "method": r.get("method", "unknown"),
                "total": int(r.get("total", 0) or 0),
                "with_req": int(r.get("with_req", 0) or 0),
                "successful": int(r.get("successful", 0) or 0),
                "conv_pct": float(r.get("conv_pct", 0) or 0),
            })
    return result


# ── 6b. Конверсия по мерчант → эндпоинт → метод ────────────────
def _build_endpoint_tree(where_sql, only_available=True):
    """Вложенный разрез: {мерчант: {эндпоинт: {with_req, successful, total, methods}}}.
    only_available=True — только ВКЛЮЧЁННЫЕ эндпоинты (is_available_for_invoice), чтобы не алертить по выключенным каналам."""
    avail = "AND COALESCE(p.is_available_for_invoice, false) = true" if only_available else ""
    rows = _q(f"""
        SELECT {MERCHANT} AS merchant_name,
               {ENDPOINT} AS endpoint,
               COALESCE({METHOD_EXPR},'не определён') AS method,
               COUNT(*) AS total,
               ({EISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS successful
        FROM {INV} i {REQJ} {MERJ} {PRODJ}
        WHERE {where_sql} AND {NOTEST_FULL} {avail}
        GROUP BY 1,2,3 ORDER BY total DESC
    """)
    result = {}
    for r in rows:
        name = r.get("merchant_name") or ""
        ep = r.get("endpoint") or "—"
        if not name:
            continue
        ep_node = result.setdefault(name, {}).setdefault(ep, {
            "total": 0, "with_req": 0, "successful": 0, "methods": []
        })
        total = int(r.get("total") or 0)
        with_req = int(r.get("with_req") or 0)
        succ = int(r.get("successful") or 0)
        ep_node["total"] += total
        ep_node["with_req"] += with_req
        ep_node["successful"] += succ
        ep_node["methods"].append({
            "method": r.get("method") or "не определён",
            "total": total,
            "with_req": with_req,
            "successful": succ,
            "conv_pct": round(100.0 * succ / with_req, 1) if with_req else 0,
        })
    return result


def get_conv_by_merchant_endpoint_method():
    """За сегодня (МСК)."""
    return _build_endpoint_tree(TODAY)


def get_recent_by_merchant_endpoint(minutes=20):
    """За последние N минут — для моментального детекта резкого падения канала."""
    return _build_endpoint_tree(f"i.created_at >= now() - interval '{int(minutes)} minutes'")


# ── 7. Почасовая по мерчантам и методам (#255) ─────────────────
def get_hourly_by_merchant():
    rows = _q(f"""
        SELECT {MERCHANT} AS merchant_name,
               COALESCE({MEXPR},'не определён') AS payment_type,
               {MSK_HOUR} AS hour,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS successful,
               ROUND(100.0*COUNT(*) FILTER (WHERE {SUCC})
                     /NULLIF(({EISS}),0),1) AS conv_pct
        FROM {INV} i {REQJ} {MERJ}
        WHERE {TODAY} AND {NOTEST_FULL}
        GROUP BY 1,2,3 ORDER BY 1,2,3
    """)
    result = {}
    for r in rows:
        name = r.get("merchant_name", "")
        method = r.get("payment_type", "unknown")
        if name:
            result.setdefault(name, {}).setdefault(method, []).append({
                "hour": int(r.get("hour") or 0),
                "total": int(r.get("total") or 0),
                "successful": int(r.get("successful") or 0),
                "conv_pct": float(r.get("conv_pct") or 0),
            })
    return result


def get_merchant_hourly(merchant):
    """Почасовая (календарные часы МСК, сегодня) для ОДНОГО мерча: hour/total/issued/success/conv_pct.
    Конверсия = success / issued (EISS) — консистентно с get_hourly_by_merchant.
    issued = эффективная выдача по потоку (мерчант+продукт), чтобы потоки без выдачи (Havale) считались по total."""
    return _q(f"""
        SELECT hour, SUM(total) AS total, SUM(eff_issued) AS issued, SUM(success) AS success,
               ROUND(100.0*SUM(success)/NULLIF(SUM(eff_issued),0),1) AS conv_pct
        FROM (
            SELECT {MSK_HOUR} AS hour,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE {SUCC}) AS success,
                   ({EISS}) AS eff_issued
            FROM {INV} i {REQJ} {MERJ}
            WHERE {TODAY} AND {NOTEST_FULL} AND {MERCHANT} = %(m)s
            GROUP BY 1, i.primary_product_id
        ) t
        GROUP BY hour ORDER BY hour
    """, {"m": merchant})


# ── 8. Анализ отказов (по коду ошибки) (#81) ───────────────────
def get_canceled_analysis():
    return _q(f"""
        SELECT COALESCE(i.last_payment_error_code,'(нет кода)') AS "Причина",
               COUNT(*) AS "Количество"
        FROM {INV} i
        WHERE {TODAY} AND {NOTEST_FULL} AND i.payment_status_id = 4
        GROUP BY 1 ORDER BY 2 DESC
    """)


# ── 9. Сравнение с вчера (#80) ─────────────────────────────────
def get_compare_yesterday():
    out = []
    for label, pred in (("Сегодня", TODAY), ("Вчера", YESTERDAY)):
        r = _q(f"""
            SELECT SUM(total) AS "Ордеров",
                   SUM(success) AS "Успешных",
                   ROUND(100.0*SUM(success)/NULLIF(SUM(eff_issued),0),2) AS "Конверсия %"
            FROM (
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE {SUCC}) AS success,
                       ({EISS}) AS eff_issued
                FROM {INV} i {REQJ}
                WHERE {pred} AND {NOTEST_FULL}
                GROUP BY i.merchant_id, i.primary_product_id
            ) t
        """)
        row = r[0] if r else {"Ордеров": 0, "Успешных": 0, "Конверсия %": 0}
        row["День"] = label
        out.append(row)
    return out


# ── 10. Плохие клиенты (#97) ───────────────────────────────────
def get_bad_clients():
    return _q(f"""
        SELECT {MERCHANT} AS merchant_name,
               i.customer_id::text AS client_id,
               COUNT(*) AS total_orders,
               COUNT(*) FILTER (WHERE {SUCC}) AS completed_orders,
               COUNT(*) FILTER (WHERE i.payment_status_id = 4) AS failed_orders,
               ROUND(100.0*COUNT(*) FILTER (WHERE {SUCC})/NULLIF(COUNT(*),0),1) AS conversion_pct,
               'bad_user' AS issue_type
        FROM {INV} i {MERJ}
        WHERE {TODAY} AND {NOTEST_FULL} AND i.customer_id IS NOT NULL
        GROUP BY 1,2
        HAVING COUNT(*) >= 10
           AND ROUND(100.0*COUNT(*) FILTER (WHERE {SUCC})/NULLIF(COUNT(*),0),1) < 30
        ORDER BY total_orders DESC
        LIMIT 50
    """)


# ── 11. Ставки по методам — Acme не отдаёт (комиссии в др. таблице) ──
def get_rates_by_method():
    return []  # TODO: при необходимости — из pricing_merchantcommission


# ── 12. PayOUT (выплаты) — withdrawalrequest ───────────────────
_PAYOUT_MERCHANTS_SQL = f"""
    SELECT {MERCHANT} AS merchant,
           COUNT(*) AS total,
           COUNT(*) FILTER (WHERE w.status = 'done') AS closed,
           ROUND(COUNT(*) FILTER (WHERE w.status='done')*100.0/NULLIF(COUNT(*),0),2) AS conv_pct,
           COALESCE(ROUND(SUM(w.amount) FILTER (WHERE w.status='done')),0) AS closed_amount
    FROM pricing_withdrawalrequest w
    LEFT JOIN pricing_merchant mr ON mr.id = w.merchant_id
    WHERE w.is_test = false
      AND (w.created_at AT TIME ZONE 'Europe/Moscow')::date = (now() AT TIME ZONE 'Europe/Moscow')::date
    GROUP BY 1 ORDER BY total DESC
"""
_PAYOUT_RANGES_SQL = """
    SELECT CASE
             WHEN w.amount >= 1000  AND w.amount < 5000   THEN '1-5K'
             WHEN w.amount >= 5000  AND w.amount < 10000  THEN '5-10K'
             WHEN w.amount >= 10000 AND w.amount < 20000  THEN '10-20K'
             WHEN w.amount >= 20000 AND w.amount < 40000  THEN '20-40K'
             WHEN w.amount >= 40000 AND w.amount <= 100000 THEN '40-100K'
             ELSE 'Other' END AS check_range,
           COUNT(*) AS total,
           COUNT(*) FILTER (WHERE w.status='done') AS closed,
           ROUND(COUNT(*)*100.0/NULLIF(SUM(COUNT(*)) OVER(),0),1) AS pct_of_total,
           ROUND(COUNT(*) FILTER (WHERE w.status='done')*100.0/NULLIF(COUNT(*),0),1) AS conv_pct,
           COALESCE(ROUND(SUM(w.amount) FILTER (WHERE w.status='done')),0) AS closed_amount
    FROM pricing_withdrawalrequest w
    WHERE w.is_test = false
      AND (w.created_at AT TIME ZONE 'Europe/Moscow')::date = (now() AT TIME ZONE 'Europe/Moscow')::date
    GROUP BY 1 ORDER BY MIN(w.amount)
"""


def get_payout_stats() -> dict:
    with ThreadPoolExecutor(max_workers=2) as ex:
        fm = ex.submit(_q, _PAYOUT_MERCHANTS_SQL)
        fr = ex.submit(_q, _PAYOUT_RANGES_SQL)
        try:
            merchants = fm.result(timeout=30) or []
        except Exception:
            merchants = []
        try:
            ranges = fr.result(timeout=30) or []
        except Exception:
            ranges = []
    return {"merchants": merchants, "ranges": ranges}


# ── Инфра-мониторинг: гейты/терминалы, латентность, ошибки, объём ──
def _gate_alert_excl(g="g", t="t"):
    """Исключение для ГЕЙТОВЫХ алертов (временно, снять — вернуть ''):
    gate-altair убран ПОЛНОСТЬЮ (все терминалы); на gate-sigma убран только МК-терминал.
    COALESCE — чтобы строки без гейта (NULL iname) не отфильтровались. g/t — алиасы paymentgate/terminal."""
    return (f"AND COALESCE({g}.iname,'') <> 'gate-altair' "
            f"AND NOT (COALESCE({g}.iname,'') = 'gate-sigma' AND COALESCE({t}.name,'') ILIKE 'MK%%')")


def get_recent_gate_conversion(minutes=20):
    """Конверсия по ГЕЙТАМ за последние N минут (для детекта 'гейт отвалился')."""
    return _q(f"""
        SELECT COALESCE(g.iname,'(нет гейта)') AS gate,
               COUNT(*) AS total,
               ({EISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM {INV} i {REQJ} {PRODJ}
        LEFT JOIN pricing_paymentgate g ON g.id = i.primary_payment_gate_id
        LEFT JOIN pricing_terminal t ON t.id = i.primary_terminal_id
        WHERE i.created_at >= now() - interval '{int(minutes)} minutes' AND {NOTEST_FULL}
          AND COALESCE(p.is_available_for_invoice, false) = true
          {_gate_alert_excl('g','t')}
        GROUP BY 1 ORDER BY total DESC
    """)


def get_active_requisites(minutes=15):
    """Активные реки за N мин: пул реквизитов (invoicerequisite) по провайдерам/терминалам
    с лимитами терминала и эндпоинтами + провайдерский роутинг без выдачи реквизита.
    Основной приём идёт через пул реквизитов (gate-orion, sticky-аккаунты);
    QR/Havale и прочие провайдеры роутятся на счёт провайдера без выдачи реквизита."""
    m = int(minutes)
    win = f"i.created_at >= now() - interval '{m} minutes'"

    terminals = _q(f"""
        SELECT COALESCE(g.iname,'(нет гейта)') AS gate,
               COALESCE(t.name, t.iname, '(?)') AS terminal,
               t.status AS t_status,
               COALESCE(t.is_available_for_invoice, false) AS t_avail,
               t.min_amount, t.max_amount,
               t.limit_day_amount, t.current_day_amount,
               t.limit_day_count, t.current_day_count,
               COUNT(DISTINCT ir.requisite_id) AS reqs,
               COUNT(*) AS issuances,
               COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM pricing_invoicerequisite ir
        JOIN {INV} i ON i.id = ir.invoice_id
        LEFT JOIN pricing_terminal    t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE {win} AND {NOTEST_FULL}
        GROUP BY 1,2,3,4,5,6,7,8,9,10
        ORDER BY issuances DESC
    """)

    endpoints = _q(f"""
        SELECT COALESCE(t.name, t.iname, '(?)') AS terminal,
               COALESCE(p.name,'—') AS endpoint,
               {MERCHANT} AS merchant,
               COUNT(*) AS issuances,
               COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM pricing_invoicerequisite ir
        JOIN {INV} i ON i.id = ir.invoice_id
        {MERJ} {PRODJ}
        LEFT JOIN pricing_terminal t ON t.id = i.primary_terminal_id
        WHERE {win} AND {NOTEST_FULL}
        GROUP BY 1,2,3
        ORDER BY issuances DESC
    """)

    no_req = _q(f"""
        SELECT COALESCE(g.iname,'нероутженные') AS gate,
               COUNT(*) AS issuances,
               COUNT(*) FILTER (WHERE {SUCC}) AS success
        FROM {INV} i
        LEFT JOIN pricing_terminal    t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE {win} AND {NOTEST_FULL}
          AND NOT EXISTS (SELECT 1 FROM pricing_invoicerequisite ir WHERE ir.invoice_id = i.id)
        GROUP BY 1 ORDER BY issuances DESC
    """)

    ep_by_term = {}
    for e in endpoints:
        ep_by_term.setdefault(e["terminal"], []).append(e)
    for t in terminals:
        t["endpoints"] = ep_by_term.get(t["terminal"], [])

    return {"minutes": m, "terminals": terminals, "no_req": no_req}


def get_behavior_by_endpoint(hours=24, min_total=20):
    """Поведение/отвал юзеров по эндпоинтам за N часов — классификация исхода по коду ошибки.
    Юзер-отвал (CANCELLED=закрыл форму, validation_error/SESSION_EXPIRED=протупил/таймаут) vs
    анти-фрод (ROUTING_ERROR) vs наша/гейтовая ошибка (COMMON*/provider*). → 'кого пушить'."""
    h = int(hours)
    rows = _q(f"""
      WITH base AS (
        SELECT {MERCHANT} AS merchant, COALESCE(p.name,'—') AS endpoint,
               i.payment_status_id AS st, i.last_payment_error_code AS code
        FROM {INV} i {MERJ} {PRODJ}
        WHERE i.created_at > now() - interval '{h} hours' AND {NOTEST_FULL}
      )
      SELECT merchant, endpoint, COUNT(*) AS total,
        COUNT(*) FILTER (WHERE st IN (3,6)) AS paid,
        COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code='CANCELLED') AS closed_form,
        COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code IN ('validation_error','SESSION_EXPIRED')) AS idle,
        COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code='ROUTING_ERROR') AS antifraud,
        COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code IN ('COMMON','COMMON_ERROR','provider_error','provider_request_timeout')) AS tech,
        COUNT(*) FILTER (WHERE st NOT IN (3,6) AND code IS NULL) AS pending
      FROM base
      GROUP BY merchant, endpoint
      HAVING COUNT(*) >= {int(min_total)}
      ORDER BY total DESC
    """)
    out = []
    for r in rows:
        t = int(r["total"]) or 1
        paid, closed, idle = int(r["paid"]), int(r["closed_form"]), int(r["idle"])
        af, tech, pend = int(r["antifraud"]), int(r["tech"]), int(r["pending"])
        user_bad = closed + idle
        out.append({
            "merchant": r["merchant"], "endpoint": r["endpoint"], "total": int(r["total"]),
            "paid": paid, "closed": closed, "idle": idle, "antifraud": af,
            "tech": tech, "pending": pend, "user_bad": user_bad,
            "paid_pct": round(paid * 100.0 / t, 1),
            "user_pct": round(user_bad * 100.0 / t, 1),
            "tech_pct": round(tech * 100.0 / t, 1),
            "af_pct": round(af * 100.0 / t, 1),
        })
    return out


def get_recent_gate_latency(minutes=20):
    """Латентность/ошибки вызовов гейтов за N минут (pricing_invoicegatecall).
    Тестовые инвойсы (в т.ч. прогоны TestBot) исключены — иначе искажают p95/failed%."""
    return _q(f"""
        SELECT COALESCE(g.iname,'(?)') AS gate,
               COUNT(*) AS calls,
               ROUND((percentile_cont(0.50) WITHIN GROUP (ORDER BY gc.duration))::numeric,2) AS p50,
               ROUND((percentile_cont(0.95) WITHIN GROUP (ORDER BY gc.duration))::numeric,2) AS p95,
               COUNT(*) FILTER (WHERE gc.status='failed') AS failed
        FROM pricing_invoicegatecall gc
        JOIN pricing_invoice i ON i.id = gc.invoice_id
        JOIN pricing_terminal t ON t.id = gc.terminal_id
        JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE gc.created_at >= now() - interval '{int(minutes)} minutes'
          AND t.status = 'active'
          AND {NOTEST_FULL}
          {_gate_alert_excl('g','t')}
        GROUP BY 1 ORDER BY calls DESC
    """)


def get_recent_routing_errors(minutes=20):
    """Кол-во заявок с ошибкой маршрутизации / без гейта за N минут."""
    r = _q(f"""
        SELECT COUNT(*) AS errs
        FROM {INV} i
        WHERE i.created_at >= now() - interval '{int(minutes)} minutes' AND {NOTEST_FULL}
          AND (i.last_payment_error_code IN ('ROUTING_ERROR','validation_error')
               OR i.primary_payment_gate_id IS NULL)
    """)
    return (r[0]["errs"] if r else 0) or 0


def get_recent_decline_codes(minutes=60):
    """Топ кодов отказа (failed) за N минут — для детекта спайка конкретной ошибки."""
    return _q(f"""
        SELECT COALESCE(i.last_payment_error_code,'(нет кода)') AS code, COUNT(*) AS c
        FROM {INV} i
        WHERE i.created_at >= now() - interval '{int(minutes)} minutes' AND {NOTEST_FULL}
          AND i.payment_status_id = 4
        GROUP BY 1 ORDER BY 2 DESC
    """)


def get_decline_context(merchant=None, minutes=180, endpoint=None):
    """Контекст отказов мерча/эндпоинта за окно (для алерта остановки трафика):
    total/ok/failed/conv + топ кодов ошибок с гейтом. Нужно, чтобы видеть, встал ли мерч НА ФОНЕ отказов."""
    where = [f"i.created_at >= now() - interval '{int(minutes)} minutes'", NOTEST_FULL]
    params = {}
    if merchant is not None:
        where.append(f"{MERCHANT} = %(m)s")
        params["m"] = merchant
    if endpoint is not None:
        where.append(f"{ENDPOINT} = %(ep)s")
        params["ep"] = endpoint
    wsql = " AND ".join(where)
    agg = _q(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS ok,
               COUNT(*) FILTER (WHERE i.payment_status_id = 4) AS failed
        FROM {INV} i {MERJ} {PRODJ}
        WHERE {wsql}
    """, params)
    a = agg[0] if agg else {}
    reasons = _q(f"""
        SELECT COALESCE(i.last_payment_error_code,'(нет кода)') AS code,
               COALESCE(g.iname,'(нет гейта)') AS gate,
               COUNT(*) AS n
        FROM {INV} i {MERJ} {PRODJ}
        LEFT JOIN pricing_terminal tt ON tt.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = tt.payment_gate_id
        WHERE {wsql} AND i.payment_status_id = 4
        GROUP BY 1,2 ORDER BY n DESC LIMIT 5
    """, params)
    total = int(a.get("total") or 0)
    ok = int(a.get("ok") or 0)
    failed = int(a.get("failed") or 0)
    return {"total": total, "ok": ok, "failed": failed,
            "conv": round(100.0 * ok / total, 1) if total else 0,
            "reasons": [{"code": r.get("code"), "gate": r.get("gate"), "n": int(r.get("n") or 0)} for r in reasons]}


def get_gate_limits() -> list:
    """Текущие лимиты активных invoice-терминалов по гейтам (чтобы не держать в голове).
    [{gate, terminal, min_amount, max_amount, day_used, day_limit, cnt_used, cnt_limit}]."""
    return _q("""
        SELECT g.iname AS gate, COALESCE(t.name, t.iname, '(?)') AS terminal,
               t.min_amount, t.max_amount,
               t.current_day_amount AS day_used, t.limit_day_amount AS day_limit,
               t.current_day_count AS cnt_used, t.limit_day_count AS cnt_limit
        FROM pricing_terminal t
        JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE t.status = 'active' AND COALESCE(t.is_available_for_invoice, false) = true
          AND g.iname ILIKE 'gate-%%'
          AND COALESCE(t.name, t.iname, '') NOT ILIKE '%%test%%'
        ORDER BY g.iname, t.name
    """)


def get_callback_failures(minutes: int = 30) -> list:
    """Недоставленные callback'и (наш вебхук мерчу вернул не-2xx) за окно, по мерчу.
    Возвращает [{merchant, n, codes}] — codes = '404×40, 500×3'."""
    rows = _q(f"""
        SELECT COALESCE(m.brand_name, m.iname, '—') AS merchant,
               COALESCE(w.response_http_status, 0) AS code,
               COUNT(*) AS n
        FROM webhook_webhook w
        LEFT JOIN pricing_merchant m ON m.id = w.merchant_id
        WHERE w.created_at >= now() - interval '{int(minutes)} minutes'
          AND (w.response_http_status >= 400 OR w.response_http_status IS NULL)
          AND COALESCE(m.iname, '') NOT ILIKE '%%test%%'
          AND COALESCE(m.brand_name, '') NOT ILIKE '%%test%%'
        GROUP BY 1, 2
    """)
    agg = {}
    for r in rows:
        a = agg.setdefault(r["merchant"], {"merchant": r["merchant"], "n": 0, "codes": {}})
        a["n"] += int(r["n"])
        code = r["code"] or "нет ответа"
        a["codes"][str(code)] = a["codes"].get(str(code), 0) + int(r["n"])
    out = list(agg.values())
    for a in out:
        a["codes_str"] = ", ".join(f"{k}×{v}" for k, v in sorted(a["codes"].items(), key=lambda kv: -kv[1]))
    out.sort(key=lambda a: -a["n"])
    return out


def get_stuck_pending(minutes: int = 120, older_min: int = 15) -> list:
    """Заявки, зависшие в pending/new дольше older_min, созданные за окно — по мерчу×эндпоинту.
    Возвращает [{merchant, endpoint, n}]."""
    return _q(f"""
        SELECT {MERCHANT} AS merchant, {ENDPOINT} AS endpoint, COUNT(*) AS n
        FROM {INV} i {MERJ} {PRODJ}
        WHERE i.created_at >= now() - interval '{int(minutes)} minutes'
          AND i.created_at < now() - interval '{int(older_min)} minutes'
          AND i.payment_status_id IN (2, 5) AND {NOTEST_FULL}
        GROUP BY 1, 2 ORDER BY n DESC
    """)


def get_spam_customers(merchant: str, period: str = "d0", min_unpaid: int = 5) -> list:
    """Клиенты мерча с ≥min_unpaid заявками БЕЗ единой оплаты за период (дубли/абьюз).
    Возвращает [{cnt, orders}] (orders — до 10 merchant_order для пересылки мерчу)."""
    if period == "d0":
        wt = "(i.created_at AT TIME ZONE 'Europe/Moscow')::date = (now() AT TIME ZONE 'Europe/Moscow')::date"
    else:
        days = {"d7": 7, "d30": 30}.get(period, 7)
        wt = f"i.created_at >= now() - interval '{days} days'"
    return _q(f"""
        SELECT COUNT(*) AS cnt,
               (array_agg(i.merchant_order ORDER BY i.created_at DESC))[1:10] AS orders
        FROM {INV} i {MERJ}
        WHERE {wt} AND {NOTEST_FULL} AND mr.iname = %(m)s AND i.customer_id IS NOT NULL
        GROUP BY i.customer_id
        HAVING COUNT(*) FILTER (WHERE {SUCC}) = 0 AND COUNT(*) >= {int(min_unpaid)}
        ORDER BY cnt DESC LIMIT 50
    """, {"m": merchant})


def get_error_report(merchant: str, period: str = "d7") -> dict:
    """Данные для Excel-отчёта по ошибкам мерча (merchant-clean, БЕЗ гейта/терминала).
    period: d0=сегодня / d7 / d30. Возвращает {summary, rows[{code,n,amount}]}."""
    if period == "d0":
        wt = "(i.created_at AT TIME ZONE 'Europe/Moscow')::date = (now() AT TIME ZONE 'Europe/Moscow')::date"
    else:
        days = {"d7": 7, "d30": 30}.get(period, 7)
        wt = f"i.created_at >= now() - interval '{days} days'"
    s = _q(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS paid,
               COUNT(*) FILTER (WHERE i.payment_status_id = 4) AS failed,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})), 0) AS turnover,
               COALESCE(ROUND(SUM(i.price) FILTER (WHERE i.payment_status_id = 4)), 0) AS failed_amt,
               MIN((i.created_at AT TIME ZONE 'Europe/Moscow')::date) AS dfrom,
               MAX((i.created_at AT TIME ZONE 'Europe/Moscow')::date) AS dto
        FROM {INV} i {MERJ}
        WHERE {wt} AND {NOTEST_FULL} AND mr.iname = %(m)s
    """, {"m": merchant})
    r = s[0] if s else {}
    rows = _q(f"""
        SELECT COALESCE(NULLIF(i.last_payment_error_code, ''), '(нет кода)') AS code,
               COUNT(*) AS n,
               COALESCE(ROUND(SUM(i.price)), 0) AS amount
        FROM {INV} i {MERJ}
        WHERE {wt} AND {NOTEST_FULL} AND mr.iname = %(m)s AND i.payment_status_id = 4
        GROUP BY 1 ORDER BY n DESC
    """, {"m": merchant})
    return {
        "summary": {
            "total": int(r.get("total") or 0), "paid": int(r.get("paid") or 0),
            "failed": int(r.get("failed") or 0), "turnover": int(r.get("turnover") or 0),
            "failed_amt": int(r.get("failed_amt") or 0),
            "dfrom": str(r.get("dfrom") or ""), "dto": str(r.get("dto") or ""),
        },
        "rows": [{"code": x["code"], "n": int(x["n"]), "amount": int(x["amount"] or 0)} for x in rows],
    }


def get_conversion_views(merchant: str, period: str = "d0") -> dict:
    """Три взгляда на конверсию мерча + дубли — чтобы снять спор «у вас 40, у нас 27».
    period: d0=сегодня(МСК) / d7 / d30.
    conv_merchant = success/total (как видит мерч, включая дубли и недолетевшие);
    conv_gate     = success/issued (наша — кто дошёл до выдачи реквизита);
    conv_intent   = уник.клиенты с оплатой / все уник.клиенты (без дублей, реальная воронка)."""
    if period == "d0":
        wt = "(i.created_at AT TIME ZONE 'Europe/Moscow')::date = (now() AT TIME ZONE 'Europe/Moscow')::date"
    else:
        days = {"d7": 7, "d30": 30}.get(period, 7)
        wt = f"i.created_at >= now() - interval '{days} days'"
    issued = ("(EXISTS(SELECT 1 FROM pricing_invoicerequisite r WHERE r.invoice_id=i.id) "
              "OR COALESCE(i.data->'route_result'->>'status','')='success' "
              "OR i.payment_status_id IN (3,6))")
    rows = _q(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COUNT(*) FILTER (WHERE {issued}) AS issued,
               COUNT(DISTINCT i.customer_id) AS uniq,
               COUNT(DISTINCT i.customer_id) FILTER (WHERE {SUCC}) AS uniq_succ
        FROM {INV} i {MERJ}
        WHERE {wt} AND {NOTEST_FULL} AND mr.iname = %(m)s
    """, {"m": merchant})
    r = rows[0] if rows else {}
    total = int(r.get("total") or 0)
    success = int(r.get("success") or 0)
    issued = int(r.get("issued") or 0)
    uniq = int(r.get("uniq") or 0)
    uniq_succ = int(r.get("uniq_succ") or 0)
    pct = lambda a, b: round(a * 100.0 / b, 1) if b else 0.0
    return {"total": total, "success": success, "issued": issued,
            "uniq": uniq, "dupes": max(0, total - uniq),
            "conv_merchant": pct(success, total),
            "conv_gate": pct(success, issued),
            "conv_intent": pct(uniq_succ, uniq)}


def get_merchant_conv_window(minutes: int) -> dict:
    """Конверсия в оплату по мерчу за окно (success/total — как видит мерч), + оборот.
    Возвращает {merchant: {'total','success','conv','amount'}}."""
    rows = _q(f"""
        SELECT {MERCHANT} AS merchant,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})), 0) AS amount
        FROM {INV} i {MERJ}
        WHERE i.created_at >= now() - interval '{int(minutes)} minutes' AND {NOTEST_FULL}
        GROUP BY 1
    """)
    out = {}
    for r in rows:
        total = int(r["total"] or 0)
        success = int(r["success"] or 0)
        out[r["merchant"]] = {"total": total, "success": success,
                              "conv": round(success * 100.0 / total, 1) if total else 0.0,
                              "amount": float(r["amount"] or 0)}
    return out


def get_gate_concentration(hours: int = 24) -> dict:
    """Концентрация приёма (успехов) по гейтам за N часов — для алерта single-point-of-failure.
    Возвращает {'top_gate', 'top_ok', 'total_ok', 'share'} (share — доля топ-гейта в %)."""
    rows = _q(f"""
        SELECT COALESCE(g.iname, '(нет)') AS gate,
               COUNT(*) FILTER (WHERE {SUCC}) AS ok
        FROM {INV} i {MERJ}
        LEFT JOIN pricing_terminal    t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE i.created_at >= now() - interval '{int(hours)} hours' AND {NOTEST_FULL}
        GROUP BY 1 ORDER BY ok DESC
    """)
    rows = [r for r in rows if (r.get("ok") or 0) > 0]
    total = sum(int(r["ok"]) for r in rows)
    if not rows or total == 0:
        return {"top_gate": None, "top_ok": 0, "total_ok": 0, "share": 0.0}
    top = rows[0]
    return {"top_gate": top["gate"], "top_ok": int(top["ok"]), "total_ok": total,
            "share": round(int(top["ok"]) * 100.0 / total, 1)}


def get_limbo_by_endpoint(minutes: int = 180) -> list:
    """«Деньги в подвисе» по эндпоинтам за окно: рек выдан (deeplink есть), но заявка не оплачена (failed).
    Разрез по валютам — суммировать RUB и TRY/UZS в один порог нельзя.
    Возвращает [{'merchant','endpoint','cur','cnt','amount'}] sorted by amount desc."""
    return _q(f"""
        SELECT {MERCHANT} AS merchant, {ENDPOINT} AS endpoint,
               i.currency_code_id AS cur,
               COUNT(*) AS cnt,
               ROUND(COALESCE(SUM(i.price), 0)) AS amount
        FROM {INV} i {MERJ} {PRODJ}
        WHERE i.created_at >= now() - interval '{int(minutes)} minutes' AND {NOTEST_FULL}
          AND i.payment_status_id = 4
          AND (i.data->'beneficiary'->>'deeplink') IS NOT NULL
        GROUP BY 1, 2, 3
        HAVING COALESCE(SUM(i.price), 0) > 0
        ORDER BY amount DESC
    """)


def get_failure_breakdown(merchant: str, minutes: int, endpoints=None) -> list:
    """ПОЛНАЯ разбивка отказов (payment_status_id=4) за окно по код×гейт — БЕЗ LIMIT,
    чтобы суммы/доли сходились на 100%. endpoints=None → весь мерч; список имён продуктов → только они.
    Возвращает [{'code', 'gate', 'n'}] (категоризация — на стороне jobs.py)."""
    where = [f"i.created_at >= now() - interval '{int(minutes)} minutes'", NOTEST_FULL,
             f"{MERCHANT} = %(m)s", "i.payment_status_id = 4"]
    params = {"m": merchant}
    if endpoints:
        where.append("p.name = ANY(%(eps)s)")
        params["eps"] = list(endpoints)
    rows = _q(f"""
        SELECT COALESCE(i.last_payment_error_code, '(нет кода)') AS code,
               left(COALESCE(NULLIF(i.last_payment_error_orig,''), i.last_payment_error, ''), 60) AS err,
               COALESCE(g.iname, '(нет гейта)') AS gate,
               COUNT(*) AS n
        FROM {INV} i {MERJ} {PRODJ}
        LEFT JOIN pricing_terminal    tt ON tt.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g  ON g.id = tt.payment_gate_id
        WHERE {' AND '.join(where)}
        GROUP BY 1, 2, 3
        ORDER BY n DESC
    """, params)
    return [{"code": r.get("code"), "err": r.get("err") or "", "gate": r.get("gate"),
             "n": int(r.get("n") or 0)} for r in rows]


def get_volume_compare(minutes=60):
    """Объём заявок: окно последних N мин vs то же окно 24ч назад (apples-to-apples)."""
    m = int(minutes)
    r = _q(f"""
        SELECT
          COUNT(*) FILTER (WHERE i.created_at >= now() - interval '{m} minutes') AS now_win,
          COUNT(*) FILTER (WHERE i.created_at >= now() - interval '{m} minutes' - interval '1 day'
                            AND i.created_at <  now() - interval '1 day') AS yest_win
        FROM {INV} i
        WHERE {NOTEST_FULL}
          AND i.created_at >= now() - interval '{m} minutes' - interval '1 day'
    """)
    row = r[0] if r else {}
    return {"now_win": (row.get("now_win") or 0), "yest_win": (row.get("yest_win") or 0)}


def get_window_summary(minutes):
    """Сводка за окно последних N минут (без тест-мерчантов): всего/выдача/успех/конв/оборот."""
    m = int(minutes)
    r = _q(f"""
        SELECT SUM(total) AS total, SUM(success) AS success,
               SUM(eff_issued) AS issued, SUM(paid) AS amount
        FROM (
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE {SUCC}) AS success,
                   ({EISS}) AS eff_issued,
                   COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})),0) AS paid
            FROM {INV} i {REQJ} {MERJ}
            WHERE i.created_at >= now() - interval '{m} minutes' AND {NOTEST_FULL}
            GROUP BY i.merchant_id, i.primary_product_id
        ) t
    """)
    row = r[0] if r else {}
    tot = int(row.get("total") or 0)
    su = int(row.get("success") or 0)
    iss = int(row.get("issued") or 0)
    amts = _q(f"""
        SELECT i.currency_code_id AS cur,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})),0) AS a
        FROM {INV} i {REQJ} {MERJ}
        WHERE i.created_at >= now() - interval '{m} minutes' AND {NOTEST_FULL}
        GROUP BY 1 HAVING COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}),0) > 0
    """)
    amounts = {x["cur"]: float(x["a"] or 0) for x in amts}
    return {"total": tot, "success": su, "issued": iss,
            "conv": round(100.0 * su / iss, 1) if iss else 0,
            "amount": float(row.get("amount") or 0),
            "amounts": amounts}


def get_window_by_merchant(minutes=60):
    """Разбивка за скользящее окно последних N минут по мерчантам+валюте (для кнопки «Последний час»)."""
    m = int(minutes)
    rows = _q(f"""
        SELECT {MERCHANT} AS merchant, i.currency_code_id AS currency,
               COUNT(*) AS total,
               ({EISS}) AS with_req,
               COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})),0) AS amount,
               ROUND(100.0*COUNT(*) FILTER (WHERE {SUCC})/NULLIF(({EISS}),0),1) AS conv
        FROM {INV} i {REQJ} {MERJ}
        WHERE i.created_at >= now() - interval '{m} minutes' AND {NOTEST_FULL}
        GROUP BY 1, 2 ORDER BY total DESC
    """)
    out = []
    for r in rows:
        out.append({"merchant": r["merchant"], "currency": r.get("currency") or "RUB",
                    "total": int(r["total"]),
                    "with_req": int(r["with_req"] or 0), "success": int(r["success"] or 0),
                    "amount": float(r["amount"] or 0), "conv": float(r["conv"] or 0)})
    return out


def get_day_overview(day_offset=0):
    """Обзор за день (0=сегодня, 1=вчера, 2=позавчера), МСК, без тестов: всего/выдача/успех/конв/оборот."""
    o = int(day_offset)
    r = _q(f"""
        SELECT SUM(total) AS total, SUM(success) AS success,
               SUM(eff_issued) AS issued, SUM(paid) AS amount
        FROM (
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE {SUCC}) AS success,
                   ({EISS}) AS eff_issued,
                   COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})),0) AS paid
            FROM {INV} i {REQJ} {MERJ}
            WHERE (i.created_at AT TIME ZONE 'Europe/Moscow')::date
                  = (now() AT TIME ZONE 'Europe/Moscow')::date - {o}
              AND {NOTEST_FULL}
            GROUP BY i.merchant_id, i.primary_product_id
        ) t
    """)
    row = r[0] if r else {}
    tot = int(row.get("total") or 0)
    su = int(row.get("success") or 0)
    iss = int(row.get("issued") or 0)
    return {"total": tot, "success": su, "issued": iss,
            "conv": round(100.0 * su / iss, 1) if iss else 0,
            "amount": float(row.get("amount") or 0)}


def get_yesterday_by_merchant_endpoint():
    """Вчерашний разбор по эндпоинтам (для топ-проблем в дайджесте)."""
    return _build_endpoint_tree(YESTERDAY)


def get_recent_routing_by_endpoint(minutes=20):
    """Заявки с РЕАЛЬНОЙ ошибкой маршрутизации за окно, по эндпоинтам.
    Считаем: ROUTING_ERROR, либо финально провалившуюся (failed) заявку, которая
    так и не получила гейт И не дошла до init гейта И отвалилась НЕ по клиентской причине.
    Исключаем «в процессе» (new/pending — гейт ещё не присвоен, ложный «нет маршрута»)
    и клиентские отвалы (SESSION_EXPIRED/CANCELLED/validation_error/COMMON). Грейс 2 мин —
    чтобы только что созданные заявки, которые ещё маршрутизируются, не считались."""
    return _q(f"""
        SELECT {ENDPOINT} AS endpoint, {MERCHANT} AS merchant, COUNT(*) AS errs
        FROM {INV} i {MERJ} {PRODJ}
        WHERE i.created_at >= now() - interval '{int(minutes)} minutes'
          AND i.created_at <= now() - interval '2 minutes'
          AND {NOTEST_FULL}
          AND (
            i.last_payment_error_code = 'ROUTING_ERROR'
            OR (
              i.primary_payment_gate_id IS NULL
              AND i.payment_status_id = 4
              AND NOT (i.data ? 'init_result')
              AND COALESCE(i.last_payment_error_code, '') NOT IN
                  ('SESSION_EXPIRED', 'CANCELLED', 'validation_error', 'COMMON', 'PAYMENT_SYSTEM_ERROR')
            )
          )
          AND COALESCE(p.is_available_for_invoice, false) = true
        GROUP BY 1,2 ORDER BY errs DESC
    """)


def get_gate_latency_degradation(recent_min=30, base_min=360):
    """Деградация скорости подтверждения по гейтам: медиана времени до успеха (сек),
    свежее окно vs базовое. Адаптивно (у каждого гейта своя норма)."""
    rm = int(recent_min)
    bm = int(base_min)
    return _q(f"""
        SELECT COALESCE(g.iname,'(нет гейта)') AS gate,
               COUNT(*) FILTER (WHERE t.recent) AS recent_succ,
               ROUND((percentile_cont(0.5) WITHIN GROUP (ORDER BY t.lat) FILTER (WHERE t.recent))::numeric,0) AS recent_med,
               ROUND((percentile_cont(0.5) WITHIN GROUP (ORDER BY t.lat))::numeric,0) AS base_med
        FROM (
            SELECT i.primary_payment_gate_id AS gid,
                   EXTRACT(EPOCH FROM (i.payment_status_updated_at - i.created_at)) AS lat,
                   (i.created_at >= now() - interval '{rm} minutes') AS recent
            FROM {INV} i
            LEFT JOIN pricing_terminal tm ON tm.id = i.primary_terminal_id
            LEFT JOIN pricing_paymentgate gg ON gg.id = i.primary_payment_gate_id
            WHERE i.created_at >= now() - interval '{bm} minutes' AND {NOTEST_FULL}
              AND {SUCC} AND i.payment_status_updated_at IS NOT NULL
              {_gate_alert_excl('gg','tm')}
        ) t
        LEFT JOIN pricing_paymentgate g ON g.id = t.gid
        GROUP BY 1 ORDER BY recent_succ DESC
    """)


def get_volume_by_endpoint(minutes=60):
    """Объём по эндпоинтам: окно последних N мин vs то же окно 24ч назад."""
    m = int(minutes)
    return _q(f"""
        SELECT {ENDPOINT} AS endpoint, {MERCHANT} AS merchant,
          COUNT(*) FILTER (WHERE i.created_at >= now() - interval '{m} minutes') AS now_win,
          COUNT(*) FILTER (WHERE i.created_at >= now() - interval '{m} minutes' - interval '1 day'
                            AND i.created_at <  now() - interval '1 day') AS yest_win
        FROM {INV} i {MERJ} {PRODJ}
        WHERE {NOTEST_FULL} AND i.created_at >= now() - interval '{m} minutes' - interval '1 day'
          AND COALESCE(p.is_available_for_invoice, false) = true
        GROUP BY 1,2
    """)


# ── Производные (как в оригинале, поверх базовых) ──────────────
def get_today_summary():
    data = get_conversion_today()
    for row in data:
        if "ИТОГО" in str(row.get("Мерчант", "")):
            return {
                "total_orders": row.get("Всего", 0) or 0,
                "successful_orders": row.get("Успешных", 0) or 0,
                "total_amount": row.get("Сумма", 0) or 0,
                "successful_amount": row.get("Сумма успешных", 0) or 0,
                "conversion": row.get("Конверсия %", 0) or 0,
            }
    return {"total_orders": 0, "successful_orders": 0, "total_amount": 0,
            "successful_amount": 0, "conversion": 0}


def get_merchants_today():
    return [r for r in get_conversion_today() if "ИТОГО" not in str(r.get("Мерчант", ""))]


def get_dashboard_summary():
    today = get_today_summary()
    return {
        "merchants_count": len(get_merchants_today()),
        "total_orders": today.get("total_orders", 0),
        "successful_orders": today.get("successful_orders", 0),
        "total_amount": today.get("total_amount", 0),
        "successful_amount": today.get("successful_amount", 0),
        "conversion": today.get("conversion", 0),
        "rates_count": 0,
    }


def search_merchant(query: str):
    q = (query or "").lower().strip()
    return [m for m in get_conversion_today()
            if q in str(m.get("Мерчант", "")).lower() and "ИТОГО" not in str(m.get("Мерчант", ""))]


def get_merchant_detailed(merchant_name: str):
    result = {"name": merchant_name, "today": None, "rates": []}
    for m in get_conversion_today():
        if str(m.get("Мерчант", "")).lower() == merchant_name.lower():
            result["today"] = m
            break
    return result


# ── «Последние реки мерча» (drill-down) ──────────────────────────
def get_active_merchants(days: int = 7) -> list:
    """Список мерчей, у которых был трафик за последние N дней — для drill-down.
    Возвращает [{'iname': str, 'name': str, 'cnt': int}] sorted by cnt desc."""
    return _q(f"""
        SELECT mr.iname AS iname,
               COALESCE(mr.brand_name, mr.iname) AS name,
               COUNT(*) AS cnt
        FROM {INV} i {MERJ}
        WHERE i.created_at >= now() - interval '{int(days)} days' AND {NOTEST_FULL}
          AND mr.iname IS NOT NULL
        GROUP BY mr.iname, mr.brand_name
        HAVING COUNT(*) > 0
        ORDER BY cnt DESC, name ASC
    """)


def get_endpoints_for_merchant(merchant_iname: str, days: int = 7) -> list:
    """Эндпоинты (= продукты), на которые шёл трафик у мерча за N дней.
    [{'endpoint': str, 'cnt': int}] sorted by cnt desc."""
    return _q(f"""
        SELECT {ENDPOINT} AS endpoint, COUNT(*) AS cnt
        FROM {INV} i {MERJ} {PRODJ}
        WHERE i.created_at >= now() - interval '{int(days)} days' AND {NOTEST_FULL}
          AND mr.iname = %s AND p.name IS NOT NULL
        GROUP BY p.name
        ORDER BY cnt DESC, p.name ASC
    """, (merchant_iname,))


def get_endpoint_turnover(merchant_iname: str, endpoint: str, period: str = "d7") -> dict:
    """Оборот (сумма успешных оплат) и конверсия в успех по мерчу×эндпоинту за период.
    period: 'd0' = сегодня (МСК), 'd7' = 7 дней, 'd30' = 30 дней (скользящие окна для d7/d30).
    Возвращает {'total', 'success', 'conv', 'amounts': {currency: turnover}}."""
    if period == "d0":
        where_time = ("(i.created_at AT TIME ZONE 'Europe/Moscow')::date "
                      "= (now() AT TIME ZONE 'Europe/Moscow')::date")
    else:
        days = {"d7": 7, "d30": 30}.get(period, 7)
        where_time = f"i.created_at >= now() - interval '{days} days'"
    rows = _q(f"""
        SELECT i.currency_code_id AS cur,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS success,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})), 0) AS turnover
        FROM {INV} i {MERJ} {PRODJ}
        WHERE {where_time} AND {NOTEST_FULL}
          AND mr.iname = %s AND p.name = %s
        GROUP BY i.currency_code_id
    """, (merchant_iname, endpoint))
    total = sum(int(r["total"] or 0) for r in rows)
    success = sum(int(r["success"] or 0) for r in rows)
    amounts = {(r["cur"] or "RUB"): float(r["turnover"] or 0)
               for r in rows if (r["turnover"] or 0) > 0}
    conv = (success / total * 100) if total else 0.0
    return {"total": total, "success": success, "conv": conv, "amounts": amounts}


def get_last_orders(merchant_iname: str, endpoint: str, limit: int = 10, kind: str = "all") -> list:
    """Последние N ордеров мерча по эндпоинту.
    kind: 'all' — без фильтра; 'issued' — рек выдан (есть deeplink);
          'not_issued' — рек НЕ выдан (нет deeplink).
    Возвращает {id, created_at_msk, price, amount_paid, status_id, status_label,
                deeplink_present, gate, error_code, error_orig}."""
    kind_filter = ""
    if kind == "issued":
        kind_filter = "AND (i.data->'beneficiary'->>'deeplink') IS NOT NULL"
    elif kind == "not_issued":
        kind_filter = "AND (i.data->'beneficiary'->>'deeplink') IS NULL"
    return _q(f"""
        SELECT i.id::text AS id,
               (i.created_at AT TIME ZONE 'Europe/Moscow')::timestamp AS created_at_msk,
               i.price, i.amount_paid, i.payment_status_id AS status_id,
               COALESCE(i.last_payment_error_code, '') AS error_code,
               COALESCE(i.last_payment_error_orig, '') AS error_orig,
               ((i.data->'beneficiary'->>'deeplink') IS NOT NULL) AS deeplink_present,
               COALESCE(g.iname, '—') AS gate
        FROM {INV} i {MERJ} {PRODJ}
        LEFT JOIN pricing_terminal    t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE {NOTEST_FULL}
          AND mr.iname = %s AND p.name = %s
          {kind_filter}
        ORDER BY i.created_at DESC
        LIMIT %s
    """, (merchant_iname, endpoint, int(limit)))


# ── Алерты (логика как в оригинале, поверх базовых функций) ────
def check_alerts():
    alerts = []
    try:
        dashboard = get_main_dashboard()
        compare = get_compare_yesterday()
        canceled = get_canceled_analysis()

        conv_today = float(dashboard.get('Конверсия в оплату %', 0) or 0)
        conv_yesterday = 0.0
        for row in compare:
            if row.get("День") == "Вчера":
                conv_yesterday = float(row.get("Конверсия %", 0) or 0)
                break
        if conv_yesterday > 0 and conv_today < conv_yesterday * 0.8:
            drop = ((conv_yesterday - conv_today) / conv_yesterday) * 100
            alerts.append({"type": "conversion_drop", "severity": "high",
                           "title": "📉 Падение конверсии",
                           "message": f"Конверсия упала на {drop:.1f}% (было {conv_yesterday:.2f}%, стало {conv_today:.2f}%)"})

        total_canceled = sum(c.get("Количество", 0) or 0 for c in canceled)
        total_orders = dashboard.get('Всего заявок', 0) or 0
        if total_orders > 0:
            cancel_rate = (total_canceled / total_orders) * 100
            if cancel_rate > 30:
                alerts.append({"type": "high_cancellation", "severity": "medium",
                               "title": "❌ Много отказов",
                               "message": f"Отказов {cancel_rate:.1f}% ({total_canceled:,} из {total_orders:,})"})

        # скользящие 60 минут, а не текущий календарный час: в hh:05 в "часе" всего
        # 5 минут данных и "0 успешных из N" срабатывал ложно
        roll = _q(f"""
            SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE {SUCC}) AS succ
            FROM {INV} i
            WHERE i.created_at >= now() - interval '60 minutes' AND {NOTEST_FULL}
        """)
        if roll:
            r0 = roll[0]
            if int(r0.get("succ") or 0) == 0 and int(r0.get("total") or 0) > 10:
                alerts.append({"type": "no_success", "severity": "high",
                               "title": "🚨 Нет успешных",
                               "message": f"За последние 60 минут 0 успешных из {int(r0.get('total') or 0)} заявок"})
    except Exception as e:
        alerts.append({"type": "error", "severity": "low",
                       "title": "⚠️ Ошибка проверки", "message": str(e)[:100]})
    return alerts


# ── Финансовые алерты: нулевая ставка / отрицательная маржа ──
def get_zero_fee_success(minutes: int = 60) -> list:
    """Успешные операции по продуктам БЕЗ активной ненулевой ставки fee_from_merchant
    (ставка не проставлена или 0/0) — комиссия с мерча не берётся, прямая потеря денег.
    Возвращает [{merchant, product, cur, n, vol}]."""
    return _q(f"""
        SELECT {MERCHANT} AS merchant, p.name AS product,
               i.currency_code_id AS cur, COUNT(*) AS n,
               ROUND(COALESCE(SUM(i.amount_paid), 0)) AS vol
        FROM {INV} i {MERJ} {PRODJ}
        WHERE i.payment_status_id IN (3,6)
          AND i.created_at > now() - interval '{int(minutes)} minutes'
          AND {NOTEST_FULL}
          AND NOT EXISTS (
            SELECT 1 FROM pricing_productcommission pc
            JOIN pricing_commissiontype ct ON ct.id = pc.commission_type_id
            WHERE pc.product_id = p.id AND ct.iname = 'fee_from_merchant'
              AND pc.commission_object = 'invoice'
              AND (pc.start_date IS NULL OR pc.start_date <= now())
              AND (pc.end_date IS NULL OR pc.end_date > now())
              AND (pc.percent <> 0 OR pc.fix_amount <> 0))
        GROUP BY 1, 2, 3
        ORDER BY n DESC
    """)


def get_negative_margin(minutes: int = 60) -> list:
    """Успешные операции, где ставка мерча (fee_from_merchant) МЕНЬШЕ ставки гейта
    (payment_gate_fee на терминале) — работаем в минус. Маржа считается по факту:
    amount_paid*(merch% - gate%)/100 + (merch_fix - gate_fix), суммарно по паре продукт×терминал.
    Возвращает [{merchant, product, terminal, cur, n, merch_pct, gate_pct, margin}]."""
    return _q(f"""
        WITH mc AS (
          SELECT DISTINCT ON (pc.product_id) pc.product_id, pc.percent mp, pc.fix_amount mf
          FROM pricing_productcommission pc
          JOIN pricing_commissiontype ct ON ct.id = pc.commission_type_id
          WHERE ct.iname = 'fee_from_merchant' AND pc.commission_object = 'invoice'
            AND (pc.start_date IS NULL OR pc.start_date <= now())
            AND (pc.end_date IS NULL OR pc.end_date > now())
          ORDER BY pc.product_id, pc.start_date DESC NULLS LAST),
        tcm AS (
          SELECT DISTINCT ON (t.terminal_id) t.terminal_id, t.percent tp, t.fix_amount tf
          FROM pricing_terminalcommission t
          JOIN pricing_commissiontype ct ON ct.id = t.commission_type_id
          WHERE ct.iname = 'payment_gate_fee'
            AND (t.start_date IS NULL OR t.start_date <= now())
            AND (t.end_date IS NULL OR t.end_date > now())
          ORDER BY t.terminal_id, (t.commission_object='invoice') DESC, t.start_date DESC NULLS LAST)
        SELECT {MERCHANT} AS merchant, p.name AS product,
               COALESCE(tt.name, tt.iname, '(?)') AS terminal,
               i.currency_code_id AS cur, COUNT(*) AS n,
               ROUND(COALESCE(mc.mp,0)::numeric, 2) AS merch_pct,
               ROUND(COALESCE(tcm.tp,0)::numeric, 2) AS gate_pct,
               ROUND(SUM(i.amount_paid*(COALESCE(mc.mp,0)-COALESCE(tcm.tp,0))/100.0
                         + COALESCE(mc.mf,0) - COALESCE(tcm.tf,0))) AS margin
        FROM {INV} i {MERJ} {PRODJ}
        LEFT JOIN pricing_terminal tt ON tt.id = i.primary_terminal_id
        LEFT JOIN mc  ON mc.product_id  = i.primary_product_id
        LEFT JOIN tcm ON tcm.terminal_id = i.primary_terminal_id
        WHERE i.payment_status_id IN (3,6)
          AND i.created_at > now() - interval '{int(minutes)} minutes'
          AND {NOTEST_FULL}
        GROUP BY 1, 2, 3, 4, 6, 7
        HAVING SUM(i.amount_paid*(COALESCE(mc.mp,0)-COALESCE(tcm.tp,0))/100.0
                   + COALESCE(mc.mf,0) - COALESCE(tcm.tf,0)) < 0
        ORDER BY margin
    """)


def get_echo_mk_volume_since(since_iso=None) -> dict:
    """Оплаченный оборот Echo по методу МК (P2P_MC / Sigma MK,
    эндпоинты Echo_RUB_P2P_MC_*) С МОМЕНТА since_iso (ISO-время; None → за всё время).
    Для счётчика-накопителя порогового пуш-алерта: копим оборот с
    последнего сброса, пуш по достижении порога, кнопка «возмещение отдано» обнуляет счётчик.
    Возвращает {n, vol, cur}."""
    cond, params = "", None
    if since_iso:
        cond = "AND i.created_at > %s::timestamptz"
        params = (since_iso,)
    rows = _q(f"""
        SELECT COUNT(*) AS n,
               ROUND(COALESCE(SUM(i.amount_paid), 0)) AS vol,
               MAX(i.currency_code_id) AS cur
        FROM {INV} i {PRODJ}
        WHERE i.payment_status_id IN (3,6)
          AND p.name ILIKE '%%Echo_RUB_P2P_MC%%'
          AND {NOTEST_FULL}
          {cond}
    """, params)
    r = rows[0] if rows else {}
    return {"n": int(r.get("n") or 0), "vol": float(r.get("vol") or 0),
            "cur": (r.get("cur") or "RUB")}


def get_vega_balance_since(since_iso=None) -> dict:
    """Оплаченный оборот (баланс) провайдера Vega (gate-vega, NSPK) С МОМЕНТА since_iso
    (ISO-время; None → за всё время). Для порогового пуш-счётчика баланса:
    копим с последнего сброса, пуш при достижении порога, кнопка обнуляет. Возвращает {n, vol, cur}."""
    cond, params = "", None
    if since_iso:
        cond = "AND i.created_at > %s::timestamptz"
        params = (since_iso,)
    rows = _q(f"""
        SELECT COUNT(*) AS n,
               ROUND(COALESCE(SUM(i.amount_paid), 0)) AS vol,
               MAX(i.currency_code_id) AS cur
        FROM {INV} i
        LEFT JOIN pricing_terminal    t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE i.payment_status_id IN (3,6)
          AND COALESCE(g.iname,'') = 'gate-vega'
          AND {NOTEST_FULL}
          {cond}
    """, params)
    r = rows[0] if rows else {}
    return {"n": int(r.get("n") or 0), "vol": float(r.get("vol") or 0),
            "cur": (r.get("cur") or "RUB")}


def get_requisite_flags(days: int = 3, min_issued: int = 12) -> list:
    """Реквизиты с min_issued+ выдач за days дней: хронология успехов для детекта «сдохших»
    (методология: зачёт от 12 выдач, стрики подряд, успех в последних 3).
    flags — список 0/1 по времени. Возвращает [{req, bank, merchants, total, succ, flags}]."""
    rows = _q(f"""
        SELECT {REQKEY} AS req,
               MAX(COALESCE(i.data->'beneficiary'->>'bank_name','')) AS bank,
               string_agg(DISTINCT COALESCE(mr.brand_name, mr.iname), ', ') AS merchants,
               string_agg(DISTINCT g.iname, ', ') FILTER (WHERE g.iname IS NOT NULL) AS gates,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) AS succ,
               array_agg((i.payment_status_id IN (3,6))::int ORDER BY i.created_at) AS flags
        FROM {INV} i {MERJ}
        LEFT JOIN pricing_terminal    t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE i.created_at > now() - interval '{int(days)} days'
          AND {NOTEST_FULL}
          AND {REQKEY} IS NOT NULL
        GROUP BY 1
        HAVING COUNT(*) >= {int(min_issued)}
    """)
    return [{"req": r["req"], "bank": r.get("bank") or "", "merchants": r.get("merchants") or "",
             "gates": r.get("gates") or "", "total": int(r["total"]), "succ": int(r["succ"]),
             "flags": list(r.get("flags") or [])} for r in rows]


def get_requisite_recent(req: str, minutes: int = 60) -> dict:
    """Активность по конкретному реквизиту за окно: всего/успехов/последняя заявка.
    Для кнопки «проверить сейчас» и для авто-удаления протухших алертов."""
    rows = _q(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) AS succ,
               COUNT(*) FILTER (WHERE i.payment_status_id = 4) AS failed,
               MAX(i.created_at) AS last_at
        FROM {INV} i
        WHERE {REQKEY} = %(req)s
          AND i.created_at > now() - interval '{int(minutes)} minutes'
          AND {NOTEST_FULL}
    """, {"req": req})
    r = rows[0] if rows else {}
    return {"total": int(r.get("total") or 0), "succ": int(r.get("succ") or 0),
            "failed": int(r.get("failed") or 0), "last_at": r.get("last_at")}


def get_day_amounts_by_cur(day_offset: int = 0) -> list:
    """Оборот (сумма успешных оплат) за день по валютам — для дайджеста,
    чтобы не складывать RUB и TRY в одно число. [{cur, amount}] по убыванию."""
    return _q(f"""
        SELECT i.currency_code_id AS cur,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {SUCC})), 0) AS amount
        FROM {INV} i
        WHERE (i.created_at AT TIME ZONE 'Europe/Moscow')::date
              = (now() AT TIME ZONE 'Europe/Moscow')::date - {int(day_offset)}
          AND {NOTEST_FULL}
        GROUP BY 1
        HAVING COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}), 0) > 0
        ORDER BY amount DESC
    """)


# ── Мониторинг выплат / светофор / гейт-базлайны / новые эндпоинты / недельный тренд ──
_WNOTEST = ("w.is_test = false AND w.merchant_id NOT IN ("
            "SELECT id FROM pricing_merchant "
            "WHERE iname ILIKE '%%test%%' OR COALESCE(brand_name,'') ILIKE '%%test%%')")


def get_stuck_withdrawals(hours: int = 2) -> list:
    """Выплаты в нефинальном статусе (не done/rejected) старше N часов, по мерчу."""
    return _q(f"""
        SELECT COALESCE(mr.brand_name, mr.iname, '—') AS merchant, w.status,
               w.currency_code_id AS cur, COUNT(*) AS n, ROUND(COALESCE(SUM(w.amount),0)) AS amount,
               ROUND(EXTRACT(EPOCH FROM (now() - MIN(w.created_at)))/3600, 1) AS oldest_h
        FROM pricing_withdrawalrequest w
        LEFT JOIN pricing_merchant mr ON mr.id = w.merchant_id
        WHERE {_WNOTEST} AND w.status NOT IN ('done', 'rejected', 'cancelled')
          AND w.created_at < now() - interval '{int(hours)} hours'
          AND w.created_at > now() - interval '7 days'
        GROUP BY 1, 2, 3 ORDER BY n DESC
    """)


def get_withdrawal_rejects(minutes: int = 120, min_n: int = 3) -> list:
    """Серии reject'ов выплат за окно, по мерчу."""
    return _q(f"""
        SELECT COALESCE(mr.brand_name, mr.iname, '—') AS merchant,
               w.currency_code_id AS cur, COUNT(*) AS n, ROUND(COALESCE(SUM(w.amount),0)) AS amount
        FROM pricing_withdrawalrequest w
        LEFT JOIN pricing_merchant mr ON mr.id = w.merchant_id
        WHERE {_WNOTEST} AND w.status = 'rejected'
          AND w.created_at > now() - interval '{int(minutes)} minutes'
        GROUP BY 1, 2 HAVING COUNT(*) >= {int(min_n)} ORDER BY n DESC
    """)


def get_gate_baselines(days: int = 7) -> dict:
    """7-дневная норма конверсии по гейтам: {gate: {'conv', 'total'}}."""
    rows = _q(f"""
        SELECT COALESCE(g.iname, '(нет гейта)') AS gate, COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {SUCC}) AS succ
        FROM {INV} i
        LEFT JOIN pricing_terminal t ON t.id = i.primary_terminal_id
        LEFT JOIN pricing_paymentgate g ON g.id = t.payment_gate_id
        WHERE i.created_at > now() - interval '{int(days)} days' AND {NOTEST_FULL}
        GROUP BY 1
    """)
    return {r["gate"]: {"conv": round(int(r["succ"])*100.0/int(r["total"]), 1) if int(r["total"]) else 0,
                        "total": int(r["total"])} for r in rows}


def get_traffic_light(minutes: int = 180) -> dict:
    """Светофор: терминалы и эндпоинты за окно vs то же окно вчера. {'terminals': [...], 'endpoints': [...]}."""
    def block(group_expr, join):
        return _q(f"""
            SELECT {group_expr} AS name,
                   COUNT(*) FILTER (WHERE i.created_at >= now() - interval '{int(minutes)} minutes') AS t_now,
                   COUNT(*) FILTER (WHERE i.created_at >= now() - interval '{int(minutes)} minutes' AND {SUCC}) AS s_now,
                   COUNT(*) FILTER (WHERE i.created_at <  now() - interval '1 day'
                                     AND i.created_at >= now() - interval '1 day' - interval '{int(minutes)} minutes') AS t_yest,
                   COUNT(*) FILTER (WHERE i.created_at <  now() - interval '1 day'
                                     AND i.created_at >= now() - interval '1 day' - interval '{int(minutes)} minutes' AND {SUCC}) AS s_yest
            FROM {INV} i {join}
            WHERE i.created_at >= now() - interval '1 day' - interval '{int(minutes)} minutes'
              AND {NOTEST_FULL}
            GROUP BY 1 HAVING COUNT(*) FILTER (WHERE i.created_at >= now() - interval '{int(minutes)} minutes') > 0
            ORDER BY t_now DESC
        """)
    terminals = block("COALESCE(t.name, t.iname, '(нет терминала)')",
                      "LEFT JOIN pricing_terminal t ON t.id=i.primary_terminal_id")
    endpoints = block("COALESCE(p.name, '(нет эндпоинта)')", PRODJ)
    return {"terminals": terminals, "endpoints": endpoints}


def get_new_endpoints(hours: int = 48) -> list:
    """Эндпоинты, созданные за последние N часов и уже получившие трафик: статистика за час и всего."""
    return _q(f"""
        SELECT p.name AS endpoint, {MERCHANT} AS merchant,
               COUNT(*) AS total, COUNT(*) FILTER (WHERE {SUCC}) AS succ,
               COUNT(*) FILTER (WHERE i.created_at > now() - interval '60 minutes') AS t_hour,
               COUNT(*) FILTER (WHERE i.created_at > now() - interval '60 minutes' AND {SUCC}) AS s_hour
        FROM {INV} i {MERJ} {PRODJ}
        WHERE p.created_at > now() - interval '{int(hours)} hours' AND {NOTEST_FULL}
        GROUP BY 1, 2 ORDER BY total DESC
    """)


def get_merchant_weekly(weeks: int = 3) -> list:
    """Понедельный срез по мерчам за N полных недель (МСК): для тренд-отчёта."""
    return _q(f"""
        SELECT {MERCHANT} AS merchant,
               date_trunc('week', i.created_at AT TIME ZONE 'Europe/Moscow')::date AS wk,
               COUNT(*) AS total, COUNT(*) FILTER (WHERE {SUCC}) AS succ,
               ROUND(COALESCE(SUM(i.amount_paid) FILTER (WHERE {SUCC}),0)) AS vol
        FROM {INV} i {MERJ}
        WHERE i.created_at >= date_trunc('week', now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow'
              - interval '{int(weeks)} weeks'
          AND i.created_at < date_trunc('week', now() AT TIME ZONE 'Europe/Moscow') AT TIME ZONE 'Europe/Moscow'
          AND {NOTEST_FULL}
        GROUP BY 1, 2 ORDER BY 1, 2
    """)


def get_spam_users(minutes: int = 60, min_orders: int = 5) -> list:
    """Юзеры-«плохиши»: min_orders+ заявок за окно и ноль оплат — кандидаты на блок у мерча.
    Возвращает merchant_order примеры (их ID, можно пересылать мерчу)."""
    return _q(f"""
        SELECT {MERCHANT} AS merchant, i.customer_id::text AS cust,
               COUNT(*) AS n,
               COUNT(*) FILTER (WHERE i.payment_status_id IN (1,2,5)) AS pending,
               string_agg(DISTINCT p.name, ', ') AS endpoints,
               COUNT(DISTINCT {REQKEY}) AS req_count,
               COALESCE(ROUND(SUM(i.price)),0) AS vol,
               (array_agg(i.merchant_order ORDER BY i.created_at DESC))[1:4] AS orders
        FROM {INV} i {MERJ} {PRODJ}
        WHERE i.created_at > now() - interval '{int(minutes)} minutes' AND {NOTEST_FULL}
        GROUP BY 1, 2
        HAVING COUNT(*) >= {int(min_orders)}
           AND COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) = 0
        ORDER BY n DESC
    """)


def get_user_recent(cust: str, minutes: int = 60) -> dict:
    """Активность клиента за окно — для кнопки «проверить» и авто-удаления спам-алертов."""
    rows = _q(f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) AS succ,
               COUNT(*) FILTER (WHERE i.payment_status_id = 4) AS failed,
               MAX(i.created_at) AS last_at
        FROM {INV} i
        WHERE i.customer_id::text = %(c)s
          AND i.created_at > now() - interval '{int(minutes)} minutes'
          AND {NOTEST_FULL}
    """, {"c": cust})
    r = rows[0] if rows else {}
    return {"total": int(r.get("total") or 0), "succ": int(r.get("succ") or 0),
            "failed": int(r.get("failed") or 0), "last_at": r.get("last_at")}


def get_merchant_test_invoices(minutes: int = 5) -> list:
    """Тестовые заявки от РЕАЛЬНЫХ мерчей (мерч прокидывает тест на интеграции) за окно.
    Наши внутренние тех-тест-аккаунты (acme/bdm/internal/qa/techsupp) исключены,
    чтобы не флудить своими прогонами. Ловим: is_test=true ИЛИ продукт с 'test' в имени."""
    return _q(f"""
        SELECT i.id::text AS id, COALESCE(mr.brand_name, mr.iname, '—') AS merch,
               p.name AS endpoint, i.currency_code_id AS cur, ROUND(i.price) AS amt,
               COALESCE(s.iname, '—') AS status,
               to_char(i.created_at AT TIME ZONE 'Europe/Moscow', 'HH24:MI:SS') AS t
        FROM {INV} i
        JOIN pricing_product p ON p.id = i.primary_product_id
        JOIN pricing_merchant mr ON mr.id = i.merchant_id
        LEFT JOIN pricing_paymentstatus s ON s.id = i.payment_status_id
        WHERE i.created_at > now() - interval '{int(minutes)} minutes'
          AND (i.is_test = true OR p.name ILIKE '%%test%%')
          AND mr.iname NOT ILIKE '%%acme%%' AND mr.iname NOT ILIKE '%%bdm%%'
          AND mr.iname NOT ILIKE '%%internal%%' AND mr.iname NOT ILIKE '%%qa%%'
          AND mr.iname NOT ILIKE '%%techsupp%%'
        ORDER BY i.created_at
    """)
