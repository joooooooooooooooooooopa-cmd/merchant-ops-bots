"""Ежедневный отчёт «позавчера → вчера» (00:00 МСК) — раздельно по валютам/гео.
Без прибыли. Метрики бьются: Σ методов = успешные (по кол-ву и сумме) по каждой валюте.
Источник — read-only реплика Core через metabase._q."""
from datetime import datetime, timezone, timedelta

import metabase as mb

MSK = timezone(timedelta(hours=3))
_RU_MONTH = {1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "мая", 6: "июн",
             7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек"}
SYM = {"RUB": "₽", "TRY": "₺", "KZT": "₸", "UZS": "soʻm", "TJS": "SM",
       "AZN": "₼", "USD": "$", "EUR": "€", "AMD": "֏"}
FLAG = {"RUB": "🇷🇺", "TRY": "🇹🇷", "KZT": "🇰🇿", "UZS": "🇺🇿", "TJS": "🇹🇯",
        "AZN": "🇦🇿", "USD": "🇺🇸", "EUR": "🇪🇺", "AMD": "🇦🇲"}
MLAB = {"sbp": "SBP", "nspk": "NSPK", "sbp_nspk": "NSPK", "qr_nspk": "QR NSPK",
        "card": "Card", "c2c": "C2C", "to_card_number": "Card", "havale": "Havale",
        "sim": "SIM", "alfa": "ALFA", "qr_pay": "QR", "p2pcard": "P2P Card", "p2p_card": "P2P Card"}

_INV = "pricing_invoice i"
_REQJ = ("LEFT JOIN (SELECT DISTINCT invoice_id FROM pricing_invoicerequisite) reqj "
         "ON reqj.invoice_id=i.id")
_PRODJ = "LEFT JOIN pricing_product p ON p.id=i.primary_product_id"
_SUCC = "i.payment_status_id IN (3,6)"
_SIG = "(reqj.invoice_id IS NOT NULL OR COALESCE(i.data->'route_result'->>'status'='success',false))"
_ISS = f"({_SIG} OR i.payment_status_id IN (3,6))"
_MEXPR = ("COALESCE((SELECT pr2.requisite_type FROM pricing_invoicerequisite ir2 "
          "JOIN pricing_paymentrequisite pr2 ON pr2.id=ir2.requisite_id "
          "WHERE ir2.invoice_id=i.id LIMIT 1), "
          "i.data->'route_result'->'terminal_data'->>'provider_payment_type')")
_PRODM = ("lower(regexp_replace(regexp_replace(p.name,"
          "'^[^_ ]+[_ ](?:RUB|TRY|KZT|UZS|EUR|USD|AMD|AZN|TJS)[_ ]',''),"
          "'([_ ](?:FTD|STD))?[_ ](?:In|Out|in|ln|IN|OUT)$',''))")
_METHOD = f"lower(COALESCE({_MEXPR}, NULLIF({_PRODM},''),'(?)'))"
# Полный фильтр тестов (флаг + блок тест-мерчей по имени) — единый с алертами/дашбордом.
# Раньше здесь был только is_test=false, и BDM_Test/internal-test протекали в дейли.
_NOTEST = mb.NOTEST_FULL
_WNOTEST = ("w.is_test=false AND w.merchant_id NOT IN ("
            "SELECT id FROM pricing_merchant "
            "WHERE iname ILIKE '%%test%%' OR COALESCE(brand_name,'') ILIKE '%%test%%')")


def _day(d):
    return f"(i.created_at AT TIME ZONE 'Europe/Moscow')::date = DATE '{d}'"


def _mlabel(m):
    return MLAB.get(m, "Прочее")


def _daydata(d):
    """Данные за день d (YYYY-MM-DD, МСК): по валютам + методы (closed) + выплаты."""
    cur = {r["cur"]: r for r in mb._q(f"""
        SELECT i.currency_code_id cur, COUNT(*) total,
               COUNT(*) FILTER (WHERE {_SUCC}) success,
               COUNT(*) FILTER (WHERE {_ISS}) issued,
               COUNT(*) FILTER (WHERE {_SIG}) sig,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {_SUCC})),0) paid,
               COALESCE(ROUND(SUM(i.price)),0) oborot
        FROM {_INV} {_REQJ} WHERE {_day(d)} AND {_NOTEST} GROUP BY 1""")}
    meth = {}
    for r in mb._q(f"""
        SELECT i.currency_code_id cur, {_METHOD} m,
               COUNT(*) FILTER (WHERE {_SUCC}) cnt,
               COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE {_SUCC})),0) paid
        FROM {_INV} {_REQJ} {_PRODJ} WHERE {_day(d)} AND {_NOTEST} GROUP BY 1,2"""):
        b = meth.setdefault(r["cur"], {})
        lab = _mlabel(r["m"])
        x = b.setdefault(lab, {"cnt": 0, "paid": 0})
        x["cnt"] += r["cnt"]
        x["paid"] += int(r["paid"])
    wd = {r["cur"]: r for r in mb._q(f"""
        SELECT w.currency_code_id cur,
               COUNT(*) FILTER (WHERE w.status='done') closed,
               COALESCE(ROUND(SUM(w.amount) FILTER (WHERE w.status='done')),0) amount
        FROM pricing_withdrawalrequest w
        WHERE {_WNOTEST} AND (w.created_at AT TIME ZONE 'Europe/Moscow')::date = DATE '{d}'
        GROUP BY 1""")}
    return cur, meth, wd


def _fmt(n):
    return f"{int(round(n)):,}".replace(",", " ")


def _money(n, c):
    return _fmt(n) + " " + SYM.get(c, c)


def _pct(o, n):
    if o == 0:
        return "(новый)" if n else "(—)"
    d = round((n - o) / o * 100)
    return f"({'+' if d >= 0 else ''}{d}%) {'✅' if n >= o else '⬇️'}"


def _pp(o, n):
    d = round(n - o, 2)
    return f"({'+' if d >= 0 else ''}{d:.2f} пп) {'✅' if d >= 0 else '⬇️'}"


def _cs(r):
    eiss = r["issued"] if r["sig"] > 0 else r["total"]
    return round(100 * r["success"] / eiss, 2) if eiss else 0


def _ci(r):
    return round(100 * r["issued"] / r["total"], 2) if r["total"] else 0


def _date_label(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.day} {_RU_MONTH[dt.month]}"


def build_report(old_date, new_date):
    co, mo, wo = _daydata(old_date)
    cn, mn, wn = _daydata(new_date)

    def tot(cd, k):
        return sum(r[k] for r in cd.values())

    L = [f"📊 <b>Статистика ({_date_label(old_date)} → {_date_label(new_date)}, МСК)</b>", ""]
    L.append(f"📊 Всего заявок: {_fmt(tot(co,'total'))} → {_fmt(tot(cn,'total'))} {_pct(tot(co,'total'),tot(cn,'total'))}")
    L.append(f"📋 Выдано: {_fmt(tot(co,'issued'))} → {_fmt(tot(cn,'issued'))} {_pct(tot(co,'issued'),tot(cn,'issued'))}")
    L.append(f"✅ Успешных: {_fmt(tot(co,'success'))} → {_fmt(tot(cn,'success'))} {_pct(tot(co,'success'),tot(cn,'success'))}")

    allc = sorted(set(co) | set(cn), key=lambda c: -(cn.get(c, {}).get("total", 0) + co.get(c, {}).get("total", 0)))
    succ = lambda c: co.get(c, {}).get("success", 0) + cn.get(c, {}).get("success", 0)
    ttl = lambda c: co.get(c, {}).get("total", 0) + cn.get(c, {}).get("total", 0)
    major = [c for c in allc if succ(c) > 0 or ttl(c) >= 10]
    minor = [c for c in allc if c not in major and ttl(c) > 0]
    z = {"total": 0, "success": 0, "issued": 0, "sig": 0, "paid": 0, "oborot": 0}
    for c in major:
        o = co.get(c, z)
        n = cn.get(c, z)
        L.append(f"\n{FLAG.get(c,'🏳️')} <b>{c}</b>")
        L.append(f"  📊 Заявки: {_fmt(o['total'])} → {_fmt(n['total'])} {_pct(o['total'],n['total'])}")
        L.append(f"  ✅ Успешных: {_fmt(o['success'])} → {_fmt(n['success'])} {_pct(o['success'],n['success'])}")
        L.append(f"  🎯 Конв. в успех: {_cs(o):.2f}% → {_cs(n):.2f}% {_pp(_cs(o),_cs(n))}")
        L.append(f"  🔄 Конв. в выдачу: {_ci(o):.2f}% → {_ci(n):.2f}% {_pp(_ci(o),_ci(n))}")
        L.append(f"  📈 Заявлено (сумма всех заявок): {_money(o['oborot'],c)} → {_money(n['oborot'],c)} {_pct(o['oborot'],n['oborot'])}")
        L.append(f"  💵 Оборот (успешные оплаты): {_money(o['paid'],c)} → {_money(n['paid'],c)} {_pct(o['paid'],n['paid'])}")
        labs = sorted(set(mo.get(c, {})) | set(mn.get(c, {})),
                      key=lambda l: -(mn.get(c, {}).get(l, {}).get("paid", 0)))
        for l in labs:
            mp = mo.get(c, {}).get(l, {"cnt": 0, "paid": 0})
            np_ = mn.get(c, {}).get(l, {"cnt": 0, "paid": 0})
            if mp["cnt"] == 0 and np_["cnt"] == 0:
                continue
            L.append(f"     • {l}: {_money(mp['paid'],c)}({mp['cnt']}) → {_money(np_['paid'],c)}({np_['cnt']}) {_pct(mp['paid'],np_['paid'])}")
    if minor:
        parts = [f"{c} {co.get(c,{}).get('total',0)}→{cn.get(c,{}).get('total',0)}" for c in minor]
        L.append("\n🌐 <b>Прочие гео</b> (0 успешных): " + " · ".join(parts))

    wcurs = sorted([c for c in (set(wo) | set(wn))
                    if wo.get(c, {}).get("closed", 0) or wn.get(c, {}).get("closed", 0)])
    if wcurs:
        L.append("\n💳 <b>Выплаты (closed):</b>")
        for c in wcurs:
            o = wo.get(c, {"closed": 0, "amount": 0})
            n = wn.get(c, {"closed": 0, "amount": 0})
            L.append(f"  {FLAG.get(c,'🏳️')} {c}: {o['closed']} → {n['closed']} шт · "
                     f"{_money(o['amount'],c)} → {_money(n['amount'],c)}")
    return "\n".join(L)


def build_default():
    """Отчёт «позавчера → вчера» по МСК (для джоба 00:00 и ручной кнопки)."""
    today = datetime.now(MSK).date()
    new = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    old = (today - timedelta(days=2)).strftime("%Y-%m-%d")
    return build_report(old, new)
