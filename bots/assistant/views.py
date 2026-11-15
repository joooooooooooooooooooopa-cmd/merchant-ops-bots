"""Детерминированные вьюхи (без LLM) для кнопочных действий — мгновенно и бесплатно."""
from html import escape


def rub(v):
    try:
        return f"{int(v):,}".replace(",", " ") + "₽"
    except (TypeError, ValueError):
        return "—"


def icon(c):
    return "🟢" if c >= 40 else ("🟡" if c >= 25 else ("🟠" if c >= 10 else "🔴"))


def overview(m, ov):
    t, y, w = ov["today"], ov["yesterday"], ov["last7d"]
    lines = [f"📊 <b>{escape(m)} — обзор</b>", ""]
    lines.append(f"<b>Сегодня:</b> {icon(t['conv_pct'])} {t['conv_pct']}% · опл {t['success']}/{t['with_req']} · {rub(t['amount_rub'])}")
    lines.append(f"<b>Вчера:</b> {y['conv_pct']}% · {rub(y['amount_rub'])}")
    lines.append(f"<b>7 дней:</b> {w['conv_pct']}% · {rub(w['amount_rub'])}")
    eps = ov.get("by_endpoint_7d") or []
    if eps:
        lines.append("\n<b>Эндпоинты (7д):</b>")
        for e in eps[:7]:
            lines.append(f"  {icon(e['conv_pct'])} {escape(e['endpoint'])}: {e['conv_pct']}% ({e['success']}/{e['with_req']})")
    return "\n".join(lines)


def trend(m, tr):
    lines = [f"📈 <b>{escape(m)} — тренд конверсии</b>", ""]
    if not tr:
        return lines[0] + "\n\n— нет данных —"
    for d in tr:
        bar = "█" * max(1, int(d["conv_pct"] / 5))
        lines.append(f"<code>{d['day'][5:]}</code> {icon(d['conv_pct'])} {d['conv_pct']:>4}%  {bar}  {rub(d['amount_rub'])}")
    return "\n".join(lines)


def behavior(m, beh):
    lines = [f"🧭 <b>{escape(m)} — поведение (24ч)</b>", "",
             "<i>🟠 закрыл/протупил = трафик · ⚙️ тех = наше/гейт</i>", ""]
    if not beh:
        return lines[0] + "\n\n— нет данных —"
    for e in beh[:8]:
        lines.append(f"<b>{escape(e['endpoint'])}</b> — {e['total']} заявок")
        lines.append(f"  ✅ {e['paid_pct']:.0f}% · 🟠 юзер {e['user_abandon_pct']:.0f}% "
                     f"(закрыл {e['user_closed_form']}/протупил {e['user_idle_timeout']}) · "
                     f"⚙️ тех {e['tech_pct']:.0f}% · 🛑 фрод {e['antifraud']}")
    return "\n".join(lines)


def limbo(m, lb):
    lines = [f"💸 <b>{escape(m)} — деньги в подвисе ({lb['days']}д)</b>", "",
             f"Упущено: <b>{rub(lb['lost_rub'])}</b> · {lb['lost_count']} заявок выдано-не-оплачено", ""]
    for e in (lb.get("by_endpoint") or [])[:7]:
        lines.append(f"  • {escape(e['endpoint'])}: {rub(e['lost_rub'])} ({e['lost_count']})")
    lines.append("\n<i>Это упущенная выручка — аргумент в разговоре с мерчем по качеству трафика.</i>")
    return "\n".join(lines)


def rates(m, rs):
    lines = [f"💳 <b>{escape(m)} — ставки</b>", ""]
    if not rs:
        return lines[0] + "\n\n— ставки не найдены —"
    for r in rs:
        s = f"<b>{r['percent']:g}%</b>"
        if r.get("fix"):
            s += f" + {r['fix']:g}₽"
        band = ""
        if r.get("min") or r.get("max"):
            lo = f"{int(r['min']):,}".replace(",", " ") if r.get("min") else "0"
            hi = f"{int(r['max']):,}".replace(",", " ") if r.get("max") else "∞"
            band = f"  💰 {lo}–{hi}₽"
        lines.append(f"  • {escape(r['endpoint'])}: {s}{band}")
    return "\n".join(lines)


def benchmark(m, b):
    gp = b["gap_vs_platform_pp"]; gt = b["gap_vs_target_pp"]
    lines = [f"📊 <b>{escape(m)} — бенчмарк (7д)</b>", "",
             f"Конверсия: <b>{b['merchant_conv_7d']}%</b>",
             f"Средняя по платформе: {b['platform_avg_conv_7d']}%  "
             f"({'➕' if gp >= 0 else '➖'}{abs(gp)} п.п.)",
             f"Цель {b['target_conv']:g}%: {'➕' if gt >= 0 else '➖'}{abs(gt)} п.п.",
             f"Место в рейтинге: <b>{b['rank']}</b> из {b['of_merchants']}"]
    return "\n".join(lines)


def withdrawals(m, wd):
    lines = [f"📤 <b>{escape(m)} — выплаты ({wd['days']}д)</b>", ""]
    if not wd.get("by_status"):
        return lines[0] + "\n\n— выплат нет —"
    for s in wd["by_status"]:
        lines.append(f"  {escape(str(s['status']))}: {s['count']} · {s['amount']} {escape(str(s['currency'] or ''))}")
    if wd.get("top_reject_errors"):
        lines.append("\n<b>Причины отклонений:</b>")
        for e in wd["top_reject_errors"]:
            lines.append(f"  • {escape(str(e['code']))}: {e['count']}")
    return "\n".join(lines)


def notes(m, ns):
    lines = [f"🗒 <b>Заметки — {escape(m)}</b>", ""]
    if not ns:
        return lines[0] + "\n\n— заметок нет. Напиши: «запиши по " + escape(m) + ": …»"
    for n in ns:
        lines.append(f"  <code>{n['when']}</code> {escape(n['text'])}")
    return "\n".join(lines)


def order_card(rows):
    if not rows:
        return "❌ Не найдено по этому ID."
    out = []
    for r in rows[:5]:
        head = "📤 Выплата" if r["kind"] == "выплата" else "📥 Заявка"
        cur = (" " + r["currency"]) if r.get("currency") else "₽"
        lines = [f"{head}  <code>{escape(str(r.get('external_id') or r.get('merchant_order') or r.get('id')))}</code>",
                 f"🕐 {r.get('created_msk')} · {escape(str(r.get('status') or '—'))}",
                 f"💰 {r.get('amount')}{cur} · опл {r.get('paid')}{cur}",
                 f"🏪 {escape(str(r.get('merchant') or '—'))} · {escape(str(r.get('endpoint') or '—'))}"]
        if r.get("gate"):
            lines.append(f"🏦 {escape(str(r['gate']))} / {escape(str(r.get('terminal') or '—'))}")
        if r.get("err_code") or r.get("err_msg"):
            lines.append(f"❗ {escape(str(r.get('err_code') or ''))} — {escape(str(r.get('err_msg') or '')[:120])}")
        out.append("\n".join(lines))
    return "\n\n".join(out)


def integration(items):
    lines = ["🧩 <b>Интеграционный трекер</b>", ""]
    if not items:
        return lines[0] + "\n\n— список пуст. Добавь кнопкой ➕ или «добавь X на интеграцию»."
    for it in items:
        d = it["days_since_contact"]
        flag = "🔴" if (d is None or d >= 3) else ("🟡" if d >= 1 else "🟢")
        dc = "не связывались" if d is None else f"{d:g} дн. назад"
        st = f" · {escape(it['stage'])}" if it.get("stage") else ""
        lines.append(f"{flag} <b>{escape(it['merchant'])}</b>{st} — контакт: {dc}")
        if it.get("note"):
            lines.append(f"    <i>{escape(it['note'])}</i>")
    return "\n".join(lines)
