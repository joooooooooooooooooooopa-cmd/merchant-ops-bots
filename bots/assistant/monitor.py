"""Проактивный мониторинг для личного ассистента: следит за приоритетными мерчами
и гейтами, и при проблеме шлёт владельцу алерт в ЛС. Детект — детерминированный
(без OpenAI), с троттлингом, чтобы не спамить."""
import os
import json
import time
import logging

import data
from config import PRIORITY, ESCALATE_MIN

logger = logging.getLogger(__name__)

STATE_FILE = os.getenv("MONITOR_STATE_FILE", "monitor_state.json")
THROTTLE = 3 * 3600          # не повторять один и тот же алерт чаще, чем раз в 3ч
KEY_COOLDOWN = {}            # переопределение кулдауна для отдельных ключей
DROP_PP = 15                 # просадка конверсии в п.п. vs базы 7д
MIN_REQ_2H = 20              # минимум выдач за 2ч, чтобы окно было значимым
TECH_PCT = 15                # порог доли тех/гейтовых ошибок
MIN_TOTAL_3H = 25            # минимум заявок за 3ч для tech-алерта
GATE_MIN_TOTAL = 30          # минимум заявок у гейта для алерта «гейт лёг»
GATE_LOW_CONV = 10           # конверсия гейта ниже — тревога


def _load():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    except Exception as e:
        logger.error(f"monitor state save: {e}")


def detect():
    """Возвращает список алертов: [{key, merchant, type, title, detail}]."""
    alerts = []

    # 1) приоритетные мерчанты — просадка конверсии и тех/гейт ошибки
    for name in PRIORITY:
        m = data.resolve_merchant(name)
        if not m:
            continue
        try:
            base = data.merchant_overview(m)["last7d"]["conv_pct"]
            w = data.window_conv(m, 120)
            if w["with_req"] >= MIN_REQ_2H and base > 0 and (base - w["conv"]) >= DROP_PP:
                alerts.append({
                    "key": f"conv:{m}", "merchant": m, "type": "conv",
                    "title": f"📉 {m}: конверсия просела",
                    "detail": f"за 2ч {w['conv']:.0f}% (база 7д {base:.0f}%) · {w['success']}/{w['with_req']} выдач",
                })
            beh = data.merchant_behavior(m, 3)
            tot = sum(e["total"] for e in beh)
            tech = sum(e["tech_our_gate"] for e in beh)
            if tot >= MIN_TOTAL_3H and tech * 100.0 / tot >= TECH_PCT:
                worst = max(beh, key=lambda e: e["tech_our_gate"])
                alerts.append({
                    "key": f"tech:{m}", "merchant": m, "type": "tech",
                    "title": f"⚙️ {m}: тех/гейтовые ошибки",
                    "detail": f"{tech * 100 // tot}% за 3ч ({tech}/{tot}) · хуже всего {worst['endpoint']} — это НАША/гейт сторона",
                })
        except Exception as e:
            logger.error(f"detect merchant {name}: {e}")

    # 1b) ВСЕ мерчи — резкая просадка конверсии (не только приоритетные)
    try:
        from config import ALL_MERCH_DROP_PP, ALL_MERCH_MIN_REQ
        pri_resolved = {data.resolve_merchant(p) for p in PRIORITY}
        for r in data.merchants_recent_vs_base(120):
            if r["merchant"] in pri_resolved:
                continue
            if (r["recent_wr"] >= ALL_MERCH_MIN_REQ and r["base_conv"] > 0
                    and (r["base_conv"] - r["recent_conv"]) >= ALL_MERCH_DROP_PP):
                alerts.append({
                    "key": f"mconv:{r['merchant']}", "merchant": r["merchant"], "type": "conv",
                    "title": f"📉 {r['merchant']}: конверсия просела",
                    "detail": f"за 2ч {r['recent_conv']:.0f}% (база 7д {r['base_conv']:.0f}%), {r['recent_wr']} выдач",
                })
    except Exception as e:
        logger.error(f"detect all-merchants: {e}")

    # 1c) ПРОВАЙДЕРЫ: просадка конв vs базы 7д + «перестал выдавать счёт»
    try:
        from config import PROVIDER_MIN_TOTAL, PROVIDER_DROP_PP, PROVIDER_NOACCT_SHARE
        base = data.gate_conv_baseline(7)
        for gw in data.gate_window(120):
            g = gw["gate"]
            if g in ("(нет гейта)",) or gw["total"] < PROVIDER_MIN_TOTAL:
                continue
            b = base.get(g, 0.0)
            if b >= 25 and (b - gw["conv"]) >= PROVIDER_DROP_PP:
                alerts.append({
                    "key": f"prov_conv:{g}", "merchant": g, "type": "gate",
                    "title": f"🏦 Провайдер {g}: конверсия просела",
                    "detail": f"за 2ч {gw['conv']:.0f}% (база 7д {b:.0f}%), {gw['success']}/{gw['total']}",
                })
            if gw["failed"] >= PROVIDER_MIN_TOTAL and gw["no_acct_share"] >= PROVIDER_NOACCT_SHARE:
                alerts.append({
                    "key": f"prov_noacct:{g}", "merchant": g, "type": "gate",
                    "title": f"🏦 Провайдер {g}: НЕ выдаёт счёт/реквизит",
                    "detail": f"за 2ч {gw['no_acct_share']:.0f}% фейлов без счёта ({gw['failed_no_acct']}/{gw['failed']}) — провайдерская проблема выдачи",
                })
    except Exception as e:
        logger.error(f"detect providers: {e}")

    # 1d) АНТИФРОД: высокий % реза (ROUTING_ERROR)
    try:
        from config import ANTIFRAUD_MIN_TOTAL, ANTIFRAUD_RATE
        af = data.antifraud_window(120)
        if af["total"] >= ANTIFRAUD_MIN_TOTAL and af["rate"] >= ANTIFRAUD_RATE:
            top = af["by_merchant"][0] if af["by_merchant"] else None
            extra = f"; хуже всего {top['merchant']} ({top['rate']:.0f}%)" if top else ""
            alerts.append({
                "key": "antifraud", "merchant": "антифрод", "type": "conv",
                "title": f"🛡 Антифрод режет {af['rate']:.0f}% за 2ч",
                "detail": f"{af['rejected']}/{af['total']} зарезано (ROUTING_ERROR){extra} — проверить правила/трафик",
            })
    except Exception as e:
        logger.error(f"detect antifraud: {e}")

    # 2) гейты — резкое падение конверсии (возможно, гейт лёг)
    try:
        for g in data.gate_health(45):
            if (g["gate"] not in ("(нет гейта)",) and g["with_req"] >= GATE_MIN_TOTAL
                    and g["conv_pct"] < GATE_LOW_CONV):
                alerts.append({
                    "key": f"gate:{g['gate']}", "merchant": g["gate"], "type": "gate",
                    "title": f"🛑 Гейт {g['gate']}: конверсия рухнула",
                    "detail": f"{g['conv_pct']:.0f}% за 45м · {g['success']}/{g['with_req']} — похоже, гейт лёг (к Head Support)",
                })
    except Exception as e:
        logger.error(f"detect gates: {e}")

    return alerts


def process():
    """Возвращает (to_send, recovered, escalations):
    - новые алерты (троттлинг),
    - восстановившиеся (были посланы и пропали),
    - эскалации (тех/гейт-проблема висит дольше ESCALATE_MIN и не решена)."""
    st = _load()
    last = st.get("last_sent", {})
    open_problems = st.get("open", {})
    now = time.time()

    detected = detect()
    detected_map = {a["key"]: a for a in detected}
    to_send, escalations = [], []

    for key, a in detected_map.items():
        cooldown = KEY_COOLDOWN.get(key, THROTTLE)
        if now - float(last.get(key, 0)) >= cooldown:
            to_send.append(a)
            last[key] = now
            if key not in open_problems:
                open_problems[key] = {"title": a["title"], "merchant": a["merchant"],
                                      "type": a["type"], "first_ts": now, "escalated": False}
        if key in open_problems:
            op = open_problems[key]
            if (not op.get("escalated") and op.get("type") in ("tech", "gate")
                    and now - float(op.get("first_ts", now)) >= ESCALATE_MIN * 60):
                op["escalated"] = True
                escalations.append({**op, "detail": a["detail"],
                                    "minutes": int((now - op["first_ts"]) // 60)})

    recovered = []
    for key in list(open_problems.keys()):
        if key not in detected_map:
            rec = open_problems.pop(key)
            recovered.append(rec)
            last.pop(key, None)

    _save({"last_sent": last, "open": open_problems})
    return to_send, recovered, escalations
