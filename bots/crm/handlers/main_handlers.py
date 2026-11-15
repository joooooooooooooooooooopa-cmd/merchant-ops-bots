import asyncio
import io
import json
import html
import logging
import zipfile
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes

from handlers.utils import (
    escape, is_authenticated, is_admin, MSK,
    BTN_TODAY, BTN_CANCELED, BTN_ALERTS, BTN_CONVERSION, BTN_LOW_CONVERSION,
    BTN_BAD_CLIENTS, BTN_SEARCH, BTN_EXPORT, BTN_RATES, BTN_ADD_MERCHANT,
    BTN_EDIT_MERCHANT, BTN_INCIDENTS, BTN_NEW_INCIDENT, BTN_UPLOAD_EXPORT,
    BTN_ACTIVE_SUMMARY, BTN_METHOD_OVERVIEW, BTN_ENDPOINT_OVERVIEW, BTN_ENDPOINT_DETAIL, BTN_SNAPSHOT, BTN_ACTIVE_REQ, BTN_BEHAVIOR, BTN_LASTHOUR, BTN_DAILY, BTN_LAST_REQS, BTN_TURNOVER, BTN_CONV_VIEWS, BTN_GATE_LIMITS, BTN_ADMIN, calculate_merchant_score, format_money,
)
from handlers.keyboards import (
    kb_main, kb_cancel, kb_export, kb_admin, kb_refresh, kb_merchant_card, kb_active_req, kb_behavior,
    kb_epd_picker, kb_epd_view, kb_epd_all, kb_epd_hourly,
    kb_lr_merchants, kb_lr_endpoints, kb_lr_count, kb_lr_kind, kb_lr_list,
    kb_tov_merchants, kb_tov_endpoints, kb_tov_period, kb_tov_result, TOV_PERIODS,
    kb_cv_merchants, kb_cv_result,
)
from handlers.formatters import (
    format_today, format_canceled, format_alerts, format_bad_clients,
    format_conversion_detailed, format_rates,
    export_merchants_csv, export_methods_csv, export_hourly_csv,
    export_conv_history_csv, export_incidents_csv,
)
from handlers.jobs import (
    CONVERSION_THRESHOLD, MIN_ORDERS_WITH_REQ_FOR_ALERT, MIN_METHOD_REQ_FOR_ALERT,
    EXCLUDED_MERCHANTS,
    get_greeting_by_time, get_merchants_status_map, get_template_variant,
)
from handlers.crm import (
    handle_crm_text_state, add_merchant_start, edit_merchant_start,
    inc_create_start, show_incidents_list, format_unified_card,
    handle_alert_callback, handle_inc_callback, handle_medit_callback,
)

import metabase as mb
import database
import keys
import storage
import visualization
from config import BOT_TOKEN, ADMIN_IDS, WEBAPP_URL

logger = logging.getLogger(__name__)


def _split_message(text: str, max_len: int = 4096):
    """Split message into chunks for Telegram's 4096 char limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_pos = text.rfind('\n', 0, max_len)
        if split_pos == -1:
            split_pos = max_len
        chunks.append(text[:split_pos])
        text = text[split_pos:].lstrip('\n')
    return chunks


async def _generate_active_merchants_summary() -> str:
    """Generate summary of active merchants using AI."""
    merchants = mb.get_conversion_today()
    active = [m for m in merchants if (m.get("С реквизитами") or 0) > 0 and "ИТОГО" not in (m.get("Мерчант") or "")]
    if not active:
        return "Нет активных мерчантов с реквизитами."
    lines = ["📝 <b>СВОДКА ПО АКТИВНЫМ МЕРЧАНТАМ</b>", ""]
    for m in sorted(active, key=lambda x: x.get("Конверсия в оплату %") or 0):
        name = escape((m.get("Мерчант") or "?")[:25])
        conv = m.get("Конверсия в оплату %") or 0
        with_req = m.get("С реквизитами") or 0
        success = m.get("Успешных") or 0
        emoji = "🟢" if conv >= 40 else "🟡" if conv >= 25 else "🟠" if conv >= 15 else "🔴"
        lines.append(f"{emoji} <b>{name}</b>: {conv:.1f}% ({success}/{with_req})")
    return "\n".join(lines)


def _extract_text_messages(messages: list) -> list:
    """Extract text messages from Telegram Desktop JSON export format."""
    text_messages = []
    for msg in messages:
        if msg.get("type") != "message":
            continue
        raw_text = msg.get("text", "")
        if isinstance(raw_text, list):
            text = "".join(
                part if isinstance(part, str) else part.get("text", "")
                for part in raw_text
            )
        else:
            text = str(raw_text)

        if len(text) < 15:
            continue

        text_messages.append({
            "text": text,
            "sender_name": msg.get("from", "Unknown"),
            "sender_id": msg.get("from_id", ""),
            "telegram_msg_id": msg.get("id", 0),
            "date": msg.get("date", ""),
        })
    return text_messages


def _parse_sender_id(sid) -> int:
    """Parse sender_id from Telegram export (handles 'user123' strings and ints)."""
    if isinstance(sid, int):
        return sid
    if isinstance(sid, str):
        cleaned = sid.replace("user", "").replace("channel", "")
        try:
            return int(cleaned)
        except ValueError:
            return 0
    return 0


async def _process_single_chat(data: dict, status_msg, chat_name_fallback: str = "") -> str:
    """Process a single chat JSON export. Returns report text."""
    from scheduler import process_message
    import analyzer

    chat_name = data.get("name", chat_name_fallback)
    chat_id = data.get("id", 0)
    messages = data.get("messages", [])

    if not messages:
        return "⚠️ В файле нет сообщений."

    text_messages = _extract_text_messages(messages)
    if not text_messages:
        return "⚠️ В файле нет текстовых сообщений для анализа."

    await status_msg.edit_text(
        f"🔍 <b>Анализирую {len(text_messages)} сообщений из «{escape(chat_name)}»...</b>\n"
        f"Это может занять пару минут.",
        parse_mode="HTML"
    )

    updated = 0
    processed = 0
    merchants_found = set()

    for msg in text_messages:
        try:
            sender_id = _parse_sender_id(msg.get("sender_id", ""))
            result = await process_message(
                text=msg["text"],
                chat_name=chat_name,
                group_id=int(chat_id) if chat_id else 0,
                telegram_msg_id=msg["telegram_msg_id"],
                sender_id=sender_id,
                sender_name=msg["sender_name"],
            )
            processed += 1
            if result:
                updated += 1
                quick = analyzer.quick_analyze(msg["text"])
                if quick.get("merchant_name"):
                    merchants_found.add(quick["merchant_name"])
        except Exception as e:
            logger.error(f"Error processing message from export: {e}")
            continue

    merchants_list = ", ".join(merchants_found) if merchants_found else "—"
    report = (
        f"✅ <b>Экспорт обработан!</b>\n\n"
        f"📁 Чат: <b>{escape(chat_name)}</b>\n"
        f"💬 Сообщений: {len(text_messages)}\n"
        f"🔍 Обработано: {processed}\n"
        f"📝 Обновлено мерчантов: {updated}\n"
    )
    if merchants_found:
        report += f"\n🏢 Найдены мерчанты: {escape(merchants_list)}"
    report += "\n\n<i>Данные обновлены в CRM. Проверьте карточки мерчантов в Mini App.</i>"
    return report


async def _process_full_export(data: dict, status_msg) -> str:
    """Process full Telegram Desktop export with chats.list. Returns report text."""
    from scheduler import process_message, is_business_chat

    chats_data = data.get("chats", {})
    all_chats = chats_data.get("list", [])

    if not all_chats:
        return "⚠️ В выгрузке нет чатов."

    # Filter business chats
    business_chats = []
    for chat in all_chats:
        chat_name = chat.get("name", "")
        if is_business_chat(chat_name):
            msgs = chat.get("messages", [])
            if msgs:
                business_chats.append(chat)

    if not business_chats:
        return (
            f"⚠️ <b>Бизнес-чаты не найдены</b>\n\n"
            f"Всего чатов в выгрузке: {len(all_chats)}\n"
            f"Ни один не содержит признаков бизнес-чата (acme + business/integration).\n\n"
            f"<i>Проверьте, что экспортировали нужные чаты.</i>"
        )

    await status_msg.edit_text(
        f"📂 <b>Найдено {len(business_chats)} бизнес-чатов из {len(all_chats)}</b>\n"
        f"Начинаю обработку...",
        parse_mode="HTML"
    )

    total_updated = 0
    total_processed = 0
    total_messages = 0
    chat_results = []

    for i, chat in enumerate(business_chats, 1):
        chat_name = chat.get("name", f"Chat #{i}")
        chat_id = chat.get("id", 0)
        messages = chat.get("messages", [])
        text_messages = _extract_text_messages(messages)
        total_messages += len(text_messages)

        # Progress update (once per chat)
        try:
            await status_msg.edit_text(
                f"🔄 <b>Обработка [{i}/{len(business_chats)}]:</b>\n"
                f"«{escape(chat_name[:50])}» — {len(text_messages)} сообщ.",
                parse_mode="HTML"
            )
        except Exception:
            pass  # Rate limit — skip progress update

        chat_updated = 0
        chat_processed = 0

        for msg in text_messages:
            try:
                sender_id = _parse_sender_id(msg.get("sender_id", ""))
                result = await process_message(
                    text=msg["text"],
                    chat_name=chat_name,
                    group_id=int(chat_id) if chat_id else 0,
                    telegram_msg_id=msg["telegram_msg_id"],
                    sender_id=sender_id,
                    sender_name=msg["sender_name"],
                )
                chat_processed += 1
                if result:
                    chat_updated += 1
            except Exception as e:
                logger.error(f"Error processing msg in {chat_name}: {e}")
                continue

        total_updated += chat_updated
        total_processed += chat_processed

        if chat_updated > 0:
            chat_results.append(f"  • {escape(chat_name[:40])} — +{chat_updated} обновл.")

    # Final report
    report = (
        f"✅ <b>Полная выгрузка обработана!</b>\n\n"
        f"📂 Бизнес-чатов: <b>{len(business_chats)}</b> из {len(all_chats)}\n"
        f"💬 Сообщений: {total_messages}\n"
        f"🔍 Обработано: {total_processed}\n"
        f"📝 Обновлений мерчантов: {total_updated}\n"
    )

    if chat_results:
        report += f"\n<b>Чаты с обновлениями:</b>\n"
        report += "\n".join(chat_results[:30])  # Limit to 30 entries
        if len(chat_results) > 30:
            report += f"\n  ... и ещё {len(chat_results) - 30}"

    report += "\n\n<i>Данные обновлены в CRM. Проверьте карточки мерчантов в Mini App.</i>"
    return report


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    uid = update.effective_user.id if update.effective_user else None

    if not is_authenticated(update):
        context.user_data["mode"] = "auth"
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔐  <b>ACME ANALYTICS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Для доступа введите ключ:",
            parse_mode="HTML"
        )
        return

    context.user_data["mode"] = None

    try:
        data = mb.get_main_dashboard()
        text = format_today(data)
    except Exception as e:
        text = f"<b>Acme Analytics</b>\n\nОшибка загрузки: {str(e)[:50]}"

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_main(is_admin(update)))


def _snap_icon(c):
    return "🟢" if c >= 40 else ("🟡" if c >= 25 else ("🟠" if c >= 10 else "🔴"))


_CUR_SYM = {"RUB": "₽", "TRY": "₺", "KZT": "₸", "UZS": "soʻm", "TJS": "SM",
            "AZN": "₼", "USD": "$", "EUR": "€", "AMD": "֏"}


def _snap_money(a, currency="RUB"):
    return f"{int(a):,}".replace(",", " ") + " " + _CUR_SYM.get(currency or "RUB", currency or "")


def _snap_money_multi(amounts):
    """Сумма по валютам: '43 918 ₽ · 6 250 ₺'. amounts = {cur: sum}."""
    if not amounts:
        return _snap_money(0)
    return " · ".join(_snap_money(v, c) for c, v in sorted(amounts.items(), key=lambda kv: -kv[1]))


# ───────────── «Эндпоинты детально» — выбор мерча + периода ─────────────
_EPD_LABELS = {"h1": "за 1ч", "h3": "за 3ч", "h24": "за 24ч", "td": "сегодня"}
_EPD_MINUTES = {"h1": 60, "h3": 180, "h24": 1440}
_EPD_TL = {"FTD": "первичка (FTD)", "STD": "вторичка (STD)", "—": "прочее"}


def _epd_icon(c):
    if c >= 40:
        return "🟢"
    if c >= CONVERSION_THRESHOLD:
        return "🟡"
    if c >= 10:
        return "🟠"
    return "🔴"


def _epd_label(pcode):
    return _EPD_LABELS.get(pcode, "сегодня")


def _epd_data(pcode):
    """Дерево мерчант→эндпоинт за период. td = календарные сутки МСК, остальные — скользящие окна."""
    if pcode == "td":
        return mb.get_conv_by_merchant_endpoint_method()
    return mb.get_recent_by_merchant_endpoint(_EPD_MINUTES.get(pcode, 1440))


def _epd_active_merchants(data):
    """Мерчи с трафиком за период, по убыванию объёма, без тестовых."""
    from handlers import jobs as _jobs
    scored = []
    for m, eps in (data or {}).items():
        if _jobs._is_excluded_merchant(m):
            continue
        req = sum((e.get("with_req") or 0) for e in eps.values())
        tot = sum((e.get("total") or 0) for e in eps.values())
        if tot <= 0:
            continue
        scored.append((m, req, tot))
    scored.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [m for m, _, _ in scored]


def _epd_picker_text(pcode, merchants):
    return (f"📊 <b>Эндпоинты детально</b>  ·  <i>{_epd_label(pcode)}</i>\n\n"
            f"Выберите мерча — покажу все его методы/продукты и общую конверсию.\n"
            f"Период переключается кнопками ниже.\n\n"
            f"Активных мерчей: <b>{len(merchants)}</b>")


def _epd_merchant_text(name, eps, pcode):
    """Полная карточка одного мерча: канал → авг-тотал → первичка/вторичка → методы."""
    from handlers import jobs as _jobs
    channels = _jobs._aggregate_channels(eps or {})
    m_req = sum(n["with_req"] for n in channels.values())
    m_succ = sum(n["successful"] for n in channels.values())
    m_conv = round(m_succ * 100.0 / m_req, 1) if m_req else 0
    lines = [
        f"{_epd_icon(m_conv)} <b>{escape(name)}</b>  ·  <i>{_epd_label(pcode)}</i>",
        f"📊 Общая: <b>{m_conv:.1f}%</b> ({m_succ}/{m_req})",
    ]
    for ck, node in sorted(channels.items(), key=lambda kv: kv[1]["with_req"], reverse=True):
        lines.append("")
        lines.append(f"🔗 <code>{escape(ck)}</code>")
        lines.append(f"   📈 авг-тотал (FTD+STD): <b>{node['conv']:.1f}%</b> ({node['successful']}/{node['with_req']})")
        for t in node["tiers"]:
            if (t["with_req"] or 0) <= 0:
                continue
            lines.append(f"   {_epd_icon(t['conv'])} {_EPD_TL.get(t['tier'], t['tier'])}: <b>{t['conv']:.1f}%</b> ({t['successful']}/{t['with_req']})")
        for ms in sorted(node["methods"], key=lambda x: x["with_req"], reverse=True):
            if (ms["with_req"] or 0) <= 0:
                continue
            lines.append(f"      {_epd_icon(ms['conv_pct'])} {escape(ms['method'])}: <b>{ms['conv_pct']:.1f}%</b> ({ms['successful']}/{ms['with_req']})")
    if not channels:
        lines.append("")
        lines.append("<i>Нет данных за выбранный период.</i>")
    return "\n".join(lines)


def _epd_hourly_text(name, rows):
    """Почасовая разбивка по мерчу за сегодня (МСК): конверсия в опл = оплаты/выдача (EISS).
    Согласована с get_hourly_by_merchant — drill-down даёт ту же цифру что общий экран."""
    lines = [f"🕐 <b>{escape(name)}</b> — по часам (сегодня, МСК)",
             "<i>конверсия = оплаты/выдача</i>", ""]
    rows = rows or []
    tot_succ = tot_iss = tot_total = 0
    for r in rows:
        h = int(r.get("hour") or 0)
        succ = int(r.get("success") or 0)
        iss  = int(r.get("issued") or 0)
        total = int(r.get("total") or 0)
        conv = float(r.get("conv_pct") or 0)
        tot_succ += succ; tot_iss += iss; tot_total += total
        lines.append(f"{h:02d}ч: <b>{conv:.1f}%</b> ({succ}/{iss} опл/выд · всего {total}) {_epd_icon(conv)}")
    if not rows:
        lines.append("<i>Нет заявок сегодня.</i>")
    else:
        day_conv = round(100.0 * tot_succ / tot_iss, 1) if tot_iss else 0
        lines.append("")
        lines.append(f"📊 За день: <b>{day_conv:.1f}%</b> ({tot_succ}/{tot_iss} опл/выд · всего {tot_total})")
    return "\n".join(lines)


def _epd_all_text(data, pcode):
    """Компактная сводка по всем мерчам за период."""
    from handlers import jobs as _jobs
    rows = []
    for m, eps in (data or {}).items():
        if _jobs._is_excluded_merchant(m):
            continue
        ch = _jobs._aggregate_channels(eps)
        req = sum(n["with_req"] for n in ch.values())
        if req < 1:
            continue
        succ = sum(n["successful"] for n in ch.values())
        rows.append((m, round(succ * 100.0 / req, 1) if req else 0, succ, req))
    rows.sort(key=lambda x: x[3], reverse=True)
    out = [f"📊 <b>Все мерчи</b>  ·  <i>{_epd_label(pcode)}</i>", ""]
    if not rows:
        out.append("<i>Нет данных за период.</i>")
    for m, conv, succ, req in rows:
        out.append(f"{_epd_icon(conv)} <b>{escape(m)}</b>: {conv:.1f}% ({succ}/{req})")
    return "\n".join(out)


def _build_lasthour(minutes=60):
    """Скользящее окно: метрики за последние N минут (now − N), НЕ календарный час."""
    w = mb.get_window_summary(minutes)
    lines = [f"🕐 <b>Последний час (скользящий) — {datetime.now(MSK).strftime('%H:%M')} МСК</b>",
             f"<i>Окно: последние {minutes} мин (now − {minutes}м), не календарный час.</i>", ""]
    lines.append(f"📊 Всего: <b>{w['total']}</b> · выдача {w['issued']} · опл {w['success']} · "
                 f"{_snap_icon(w['conv'])} <b>{w['conv']:.1f}%</b> · {_snap_money_multi(w.get('amounts') or {})}")
    try:
        rows = mb.get_window_by_merchant(minutes)
    except Exception:
        rows = []
    shown = [r for r in rows if r["with_req"] >= 1][:10]
    if shown:
        lines.append("")
        lines.append("<b>По мерчантам (скользящие 60 мин):</b>")
        for r in shown:
            lines.append(f"   {_snap_icon(r['conv'])} {escape(r['merchant'])}: <b>{r['conv']:.0f}%</b> "
                         f"({r['success']}/{r['with_req']}) · {_snap_money(r['amount'], r.get('currency'))}")
    else:
        lines.append("\n— за окно заявок не было —")
    return "\n".join(lines)


def _build_snapshot():
    """Компактный real-time снимок: 15м / 1ч / сегодня + активные гейты (20м)."""
    lines = [f"⚡ <b>Снимок — {datetime.now(MSK).strftime('%d.%m %H:%M')} МСК</b>", ""]
    for label, mins in [("15 мин", 15), ("1 час", 60), ("Сегодня", None)]:
        if mins is None:
            s = mb.get_today_summary()
            tot, su, cv, iss = s["total_orders"], s["successful_orders"], (s["conversion"] or 0), None
            money = _snap_money(s["successful_amount"] or 0)
        else:
            w = mb.get_window_summary(mins)
            tot, su, cv, iss = w["total"], w["success"], w["conv"], w["issued"]
            money = _snap_money_multi(w.get("amounts") or {})
        extra = f" · выдача {iss}" if iss is not None else ""
        lines.append(f"🕐 <b>{label}:</b> {tot} заявок{extra} · опл {su} · {_snap_icon(cv)} <b>{cv:.1f}%</b> · {money}")
    try:
        gates = mb.get_recent_gate_conversion(20)
    except Exception:
        gates = []
    act = []
    for g in gates:
        gate = g.get("gate") or "?"
        wr = int(g.get("with_req") or 0)
        sc = int(g.get("success") or 0)
        if gate == "(нет гейта)" or wr < 3:
            continue
        act.append((gate, round(sc * 100.0 / wr, 1) if wr else 0, sc, wr))
    if act:
        lines.append("")
        lines.append("🏦 <b>Активные гейты (20 мин):</b>")
        for gate, c, sc, wr in sorted(act, key=lambda x: -x[3])[:8]:
            lines.append(f"   {_snap_icon(c)} {escape(gate)}: <b>{c:.1f}%</b> ({sc}/{wr})")
    return "\n".join(lines)


def _ar_amt(v):
    try:
        return f"{int(v):,}".replace(",", " ") + "₽"
    except (TypeError, ValueError):
        return "—"


def _ar_band(mn, mx):
    lo = f"{int(mn):,}".replace(",", " ") if mn is not None else "0"
    hi = f"{int(mx):,}".replace(",", " ") if (mx is not None and int(mx) > 0) else "∞"
    return f"{lo}–{hi}₽"


def _ar_day(t):
    cur = int(t.get("current_day_amount") or 0)
    cnt = int(t.get("current_day_count") or 0)
    lim = int(t.get("limit_day_amount") or 0)
    base = f"{cur:,}".replace(",", " ") + f"₽ ({cnt} шт)"
    if lim > 0:
        pct = cur * 100 // lim
        base += " / лимит " + f"{lim:,}".replace(",", " ") + f"₽ ({pct}%)"
    return base


def _ar_ep(e):
    m = e.get("merchant") or "—"
    nm = (e.get("endpoint") or "").upper()
    tier = "FTD" if "FTD" in nm else ("STD" if "STD" in nm else "")
    return f"{m}{('·' + tier) if tier else ''}"


def _build_active_req(minutes=15):
    """Активные реки: пул реквизитов по провайдерам/терминалам за N мин + лимиты + эндпоинты."""
    data = mb.get_active_requisites(minutes)
    terms = data["terminals"]
    no_req = data["no_req"]

    tot_iss = sum(int(t["issuances"]) for t in terms)
    tot_suc = sum(int(t["success"]) for t in terms)
    tot_req = sum(int(t["reqs"]) for t in terms)
    conv = (tot_suc * 100.0 / tot_iss) if tot_iss else 0
    lbl = {15: "15 мин", 60: "1 час", 1440: "24 часа"}.get(minutes, f"{minutes} мин")

    lines = [f"🪪 <b>Активные реки — {lbl}</b> · {datetime.now(MSK).strftime('%H:%M')} МСК", ""]
    lines.append(f"Выдач: <b>{tot_iss}</b> · реквизитов(акт.): <b>{tot_req}</b> · "
                 f"успех: <b>{tot_suc}</b> · {_snap_icon(conv)} {conv:.1f}%")

    if not terms:
        lines.append("\n— за окно реквизиты не выдавались —")

    by_gate = {}
    for t in terms:
        by_gate.setdefault(t["gate"], []).append(t)
    for gate, tlist in sorted(by_gate.items(), key=lambda kv: -sum(int(x["issuances"]) for x in kv[1])):
        g_iss = sum(int(x["issuances"]) for x in tlist)
        g_suc = sum(int(x["success"]) for x in tlist)
        g_req = sum(int(x["reqs"]) for x in tlist)
        g_conv = (g_suc * 100.0 / g_iss) if g_iss else 0
        lines.append("")
        lines.append(f"🏦 <b>{escape(str(gate))}</b> — реков {g_req} · выдач {g_iss} · "
                     f"{_snap_icon(g_conv)} {g_conv:.0f}%")
        for t in sorted(tlist, key=lambda x: -int(x["issuances"])):
            iss, suc, rq = int(t["issuances"]), int(t["success"]), int(t["reqs"])
            c = (suc * 100.0 / iss) if iss else 0
            off = "" if t.get("t_avail") else " ⛔откл"
            lines.append(f"  💳 <b>{escape(str(t['terminal']))}</b>{off}")
            lines.append(f"     реков {rq} · выдач {iss} · успех {suc} · {_snap_icon(c)} {c:.1f}%")
            lines.append(f"     лимит: {_ar_band(t.get('min_amount'), t.get('max_amount'))} · "
                         f"сегодня: {_ar_day(t)}")
            eps = t.get("endpoints") or []
            if eps:
                parts = [f"{escape(_ar_ep(e))} {int(e['issuances'])}({int(e['success'])})" for e in eps[:5]]
                lines.append("     эндпоинты: " + " · ".join(parts))

    if no_req:
        tot_nr = sum(int(x["issuances"]) for x in no_req)
        parts = [f"{escape(str(x['gate']))} {int(x['issuances'])}" for x in no_req[:6]]
        lines.append("")
        lines.append(f"📡 <b>Без выдачи реквизита</b> (провайдерский роутинг): {tot_nr}")
        lines.append("   " + " · ".join(parts))

    return "\n".join(lines)


_BEH_CUR = {"RUB", "TRY", "KZT", "UZS", "EUR", "USD", "AMD", "AZN", "TJS"}


def _beh_name(d):
    """Различимое имя эндпоинта: мерчант · метод/коридор+tier (из имени продукта)."""
    prod = d.get("endpoint") or ""
    parts = prod.split("_")
    mid = [p for p in parts[1:]
           if p.upper() not in _BEH_CUR and p.lower() not in ("in", "out", "ln")]
    tag = "_".join(mid)[:22] if mid else prod[:22]
    return f"{d.get('merchant', '—')}·{tag}" if tag else (d.get("merchant") or "—")


def _build_behavior(hours=24):
    """Поведение/отвал юзеров по эндпоинтам: юзерский отвал (кого пушить) vs наша/гейт (чинить)."""
    data = mb.get_behavior_by_endpoint(hours)
    lbl = {24: "24 ч", 72: "3 дня", 168: "7 дней"}.get(hours, f"{hours} ч")
    head = f"🧭 <b>Поведение юзеров по эндпоинтам — {lbl}</b> · {datetime.now(MSK).strftime('%H:%M')} МСК"
    if not data:
        return head + "\n\n— нет данных за период —"
    T = sum(d["total"] for d in data)
    P = sum(d["paid"] for d in data)
    U = sum(d["user_bad"] for d in data)
    TE = sum(d["tech"] for d in data)
    AF = sum(d["antifraud"] for d in data)

    def pct(x):
        return round(x * 100.0 / T, 1) if T else 0

    lines = [head, ""]
    lines.append(f"Всего <b>{T}</b> · ✅ опл <b>{pct(P):.0f}%</b> · 🟠 юзер-отвал <b>{pct(U):.0f}%</b> · "
                 f"⚙️ тех {pct(TE):.0f}% · 🛑 фрод {pct(AF):.0f}%")
    lines.append("<i>🟠 закрыл форму / протупил = трафик мерча · ⚙️ COMMON/provider = наше/гейт</i>")

    push = sorted([d for d in data if d["user_bad"] >= 10], key=lambda d: -d["user_bad"])[:6]
    if push:
        lines.append("")
        lines.append("🟠 <b>Кого пушить (юзерский отвал):</b>")
        for d in push:
            lines.append(f"  <b>{escape(_beh_name(d))}</b> — отвал {d['user_pct']:.0f}% "
                         f"({d['user_bad']}/{d['total']}): закрыл {d['closed']} · протупил {d['idle']}")

    fix = sorted([d for d in data if d["tech"] >= 10], key=lambda d: -d["tech"])[:6]
    if fix:
        lines.append("")
        lines.append("⚙️ <b>Наша / гейтовая проблема (чинить):</b>")
        for d in fix:
            lines.append(f"  <b>{escape(_beh_name(d))}</b> — тех {d['tech_pct']:.0f}% ({d['tech']}) · "
                         f"фрод {d['antifraud']} · опл {d['paid_pct']:.0f}%")
    return "\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка сообщений"""
    txt = update.message.text.strip()
    mode = context.user_data.get("mode")
    uid = update.effective_user.id if update.effective_user else None

    # === АУТЕНТИФИКАЦИЯ ===
    if mode == "auth" or not is_authenticated(update):
        if keys.validate_key(txt, uid):
            storage.log_audit(uid, "login_ok", "bot")
            context.user_data["mode"] = None
            # Delete message with key for security
            try: await update.message.delete()
            except: pass
            try:
                data = mb.get_main_dashboard()
                text = "✅ <b>Доступ разрешён!</b>\n\n" + format_today(data)
            except:
                text = "✅ <b>Доступ разрешён!</b>\n\nВыберите раздел в меню."
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_main(is_admin(update)))
        else:
            storage.log_audit(uid, "login_fail", "bot: wrong key")
            await update.message.reply_text(
                "❌ <b>Ключ недействителен</b>\n\n"
                "Ключ неверный или уже использован.\n"
                "Получите новый ключ у администратора.",
                parse_mode="HTML"
            )
        return

    # === ОТМЕНА / ВОЗВРАТ ===
    if txt == "◀️ Вернуться в меню":
        context.user_data["mode"] = None
        context.user_data.pop('conv_state', None)
        await update.message.reply_text("Главное меню:", reply_markup=kb_main(is_admin(update)))
        return

    # === CRM ТЕКСТОВЫЕ СОСТОЯНИЯ ===
    if await handle_crm_text_state(update, context):
        return

    # === РЕЖИМ ПОИСКА ===
    if mode == "search":
        results = mb.search_merchant(txt)

        if not results:
            await update.message.reply_text(
                f"❌ Мерчант «<b>{txt}</b>» не найден\n\n"
                "Попробуйте другое название или вернитесь в меню.",
                parse_mode="HTML",
                reply_markup=kb_cancel()
            )
            return

        if len(results) == 1:
            merchant_name = results[0].get("Мерчант") or ""
            mb_data = mb.get_merchant_detailed(merchant_name)
            crm_list = database.search_merchants(merchant_name)
            crm_m = crm_list[0] if crm_list else None
            tags = database.get_merchant_tags(crm_m['id']) if crm_m else []
            score = calculate_merchant_score(crm_m) if crm_m else None
            trend = database.get_conversion_trend(merchant_name) if crm_m else 'stable'
            text = format_unified_card(mb_data, crm_m, tags, score, trend)
            context.user_data["mode"] = None
            kb_extra = kb_merchant_card(crm_m['id']) if crm_m else None
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_main(is_admin(update)))
            if kb_extra:
                await update.message.reply_text("Действия:", reply_markup=kb_extra)
        else:
            lines = [f"🔍 Найдено: <b>{len(results)}</b>", ""]
            for m in results[:10]:
                name = escape(m.get("Мерчант") or "?")
                conv = m.get("Конверсия %") or 0
                status = "🟢" if conv >= 1.5 else "🟡" if conv >= 0.8 else "🔴"
                lines.append(f"{status} {name} ({conv:.1f}%)")
            lines.append("")
            lines.append("Уточните название для точного поиска.")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_cancel())
        return

    # === КНОПКИ МЕНЮ ===
    try:
        if txt == BTN_TODAY:
            data = mb.get_main_dashboard()
            text = format_today(data)
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_refresh("refresh_today"))

        elif txt == BTN_SNAPSHOT:
            await update.message.reply_text(_build_snapshot(), parse_mode="HTML", reply_markup=kb_refresh("refresh_snapshot"))

        elif txt == BTN_LASTHOUR:
            await update.message.reply_text(_build_lasthour(60), parse_mode="HTML", reply_markup=kb_refresh("refresh_lasthour"))

        elif txt == BTN_DAILY:
            import daily_report
            await update.message.reply_text(daily_report.build_default(), parse_mode="HTML", reply_markup=kb_main(is_admin(update)))

        elif txt == BTN_LAST_REQS:
            await _lr_send_root(update.message)

        elif txt == BTN_TURNOVER:
            await _tov_send_root(update.message)

        elif txt == BTN_CONV_VIEWS:
            await _cv_send_root(update.message)

        elif txt == BTN_GATE_LIMITS:
            for chunk in _split_html(_build_gate_limits(), 3800):
                await update.message.reply_text(chunk, parse_mode="HTML")

        elif txt == BTN_ACTIVE_REQ:
            await update.message.reply_text(_build_active_req(15), parse_mode="HTML", reply_markup=kb_active_req(15))

        elif txt == BTN_BEHAVIOR:
            await update.message.reply_text(_build_behavior(24), parse_mode="HTML", reply_markup=kb_behavior(24))

        elif txt == BTN_CONVERSION:
            merchants = mb.get_conversion_today()
            text = format_conversion_detailed(merchants)
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_refresh("refresh_conversion"))

        elif txt == BTN_LOW_CONVERSION:
            merchants = mb.get_conversion_today()
            unpaid_orders = mb.get_unpaid_orders()
            try:
                hourly_by_merchant = mb.get_hourly_by_merchant()
            except Exception:
                hourly_by_merchant = {}
            try:
                conv_by_method = mb.get_conv_by_merchant_method()
            except Exception:
                conv_by_method = {}
            merchant_statuses = get_merchants_status_map()

            # Находим мерчантов с низкой конверсией (по общей)
            low_conv_merchants = []
            for m in merchants:
                name = m.get("Мерчант") or ""
                if "ИТОГО" in name:
                    continue
                if merchant_statuses.get(name) == 'disconnected':
                    continue
                if name in EXCLUDED_MERCHANTS:
                    continue

                total = m.get("Всего") or 0
                with_req = m.get("С реквизитами") or 0
                conv = m.get("Конверсия в оплату %") or 0

                if with_req >= MIN_ORDERS_WITH_REQ_FOR_ALERT and conv < CONVERSION_THRESHOLD:
                    low_conv_merchants.append({
                        "name": name,
                        "total": total,
                        "with_req": with_req,
                        "conv": conv,
                        "successful": m.get("Успешных") or 0,
                    })

            if not low_conv_merchants:
                await update.message.reply_text(
                    "✅ <b>Все мерчанты имеют нормальную конверсию</b>\n\n"
                    f"Порог: {CONVERSION_THRESHOLD}%\n"
                    f"Минимум заявок с реквизитами: {MIN_ORDERS_WITH_REQ_FOR_ALERT}",
                    parse_mode="HTML"
                )
            else:
                # Отправляем уведомление
                await update.message.reply_text(
                    f"🚨 <b>Найдено {len(low_conv_merchants)} мерчантов с низкой конверсией</b>\n\n"
                    f"Отправляю отдельное сообщение для каждого...",
                    parse_mode="HTML"
                )

                # Получаем приветствие
                greeting = get_greeting_by_time()

                # Для каждого мерчанта отправляем отдельное сообщение с картинкой
                for m in low_conv_merchants:
                    conv = m['conv']
                    name = m['name']
                    total = m['total']
                    with_req = m['with_req']
                    successful = m['successful']

                    # Выбираем шаблон
                    if conv < 10:
                        emoji = "🔴"
                        template_text = get_template_variant('critical')
                    elif conv < 20:
                        emoji = "⚠️"
                        template_text = get_template_variant('low')
                    else:
                        emoji = "💛"
                        template_text = get_template_variant('warning')

                    template = f"Коллеги, {greeting}!\n\n{template_text}"

                    lines = [
                        f"{emoji} <b>{escape(name)}</b>",
                        "",
                        f"📊 Конверсия в оплату: <b>{conv:.1f}%</b> (порог {CONVERSION_THRESHOLD}%)",
                        f"📋 С реквизитами: {with_req} → Оплатили: {successful}",
                    ]

                    # Разбивка по методам со статусом
                    merchant_methods = conv_by_method.get(name, [])
                    methods_with_req = [ms for ms in merchant_methods if (ms.get('with_req', 0) or 0) > 0]
                    if methods_with_req:
                        lines.append("")
                        lines.append("💳 <b>По методам:</b>")
                        for ms in sorted(methods_with_req, key=lambda x: x.get('with_req', 0), reverse=True):
                            m_name = ms.get('method', '?')
                            m_conv = ms.get('conv_pct', 0) or 0
                            m_req = ms.get('with_req', 0)
                            m_succ = ms.get('successful', 0)
                            if m_conv >= 40:
                                m_icon = "🟢"
                            elif m_conv >= CONVERSION_THRESHOLD:
                                m_icon = "🟡"
                            elif m_conv >= 10:
                                m_icon = "🟠"
                            else:
                                m_icon = "🔴"
                            lines.append(f"   {m_icon} {m_name}: <b>{m_conv:.1f}%</b> ({m_succ}/{m_req})")

                    lines.extend([
                        "",
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                        "",
                        template,
                    ])

                    # Добавляем примеры неоплаченных заявок
                    merchant_unpaid = unpaid_orders.get(name, [])
                    if merchant_unpaid and isinstance(merchant_unpaid, list):
                        lines.append("")
                        lines.append("📋 <b>Примеры неоплаченных заявок:</b>")
                        for item in merchant_unpaid[:10]:
                            if isinstance(item, dict):
                                ext = item.get('order_id', '?')
                                method = item.get('method', '')
                                method_str = f" · {method}" if method else ""
                                lines.append(f"• <code>{ext}</code>{method_str}")
                            else:
                                lines.append(f"• <code>{item}</code>")

                    lines.append("")
                    lines.append(f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")

                    text = "\n".join(lines)

                    # Генерируем картинку
                    try:
                        chart_image = visualization.generate_conversion_chart(
                            merchant_name=name,
                            total=total,
                            with_req=with_req,
                            successful=successful,
                            conv=conv,
                            hourly_data=hourly_by_merchant.get(name),
                            method_stats_pg=merchant_methods,
                        )
                        await update.message.reply_photo(
                            photo=chart_image,
                            caption=text,
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.error(f"Ошибка генерации визуализации для {name}: {e}")
                        await update.message.reply_text(text, parse_mode="HTML")

        elif txt == BTN_METHOD_OVERVIEW:
            await update.message.reply_text("⏳ Загружаю данные по методам...", parse_mode="HTML")
            try:
                conv_by_method = mb.get_conv_by_merchant_method()
            except Exception:
                conv_by_method = {}

            if not conv_by_method:
                await update.message.reply_text("❌ Нет данных по методам", parse_mode="HTML")
            else:
                # Только мерчанты с ≥10 выданных реквизитов суммарно
                for merchant_name in sorted(conv_by_method.keys()):
                    methods = conv_by_method[merchant_name]
                    methods_with_req = [ms for ms in methods if (ms.get('with_req', 0) or 0) > 0]
                    total_req_all = sum(ms.get('with_req', 0) or 0 for ms in methods_with_req)
                    if total_req_all < 10:
                        continue

                    total_req = sum(ms.get('with_req', 0) or 0 for ms in methods_with_req)
                    total_succ = sum(ms.get('successful', 0) or 0 for ms in methods_with_req)
                    overall_conv = round(total_succ * 100.0 / total_req, 1) if total_req > 0 else 0

                    if overall_conv >= 40:
                        m_emoji = "🟢"
                    elif overall_conv >= CONVERSION_THRESHOLD:
                        m_emoji = "🟡"
                    elif overall_conv >= 10:
                        m_emoji = "🟠"
                    else:
                        m_emoji = "🔴"

                    lines = [
                        f"{m_emoji} <b>{escape(merchant_name)}</b>",
                        f"📊 Общая: <b>{overall_conv:.1f}%</b> ({total_succ}/{total_req})",
                        "",
                    ]

                    for ms in sorted(methods_with_req, key=lambda x: x.get('with_req', 0), reverse=True):
                        m_name = ms.get('method', '?')
                        m_conv = ms.get('conv_pct', 0) or 0
                        m_req = ms.get('with_req', 0)
                        m_succ = ms.get('successful', 0)
                        if m_conv >= 40:
                            icon = "🟢"
                        elif m_conv >= CONVERSION_THRESHOLD:
                            icon = "🟡"
                        elif m_conv >= 10:
                            icon = "🟠"
                        else:
                            icon = "🔴"
                        lines.append(f"   {icon} {m_name}: <b>{m_conv:.1f}%</b> ({m_succ}/{m_req})")

                    text = "\n".join(lines)
                    await update.message.reply_text(text, parse_mode="HTML")

        elif txt == BTN_ENDPOINT_OVERVIEW:
            await update.message.reply_text("⏳ Проверяю каналы за 2ч (первичка+вторичка объединены)...", parse_mode="HTML")
            from handlers import jobs as _jobs
            try:
                problems = _jobs.collect_problems(_jobs.CONV_WINDOW_MIN, _jobs.CONV_MIN_ISSUED)
            except Exception as e:
                problems = []
                await update.message.reply_text(f"❌ Ошибка проверки: {str(e)[:80]}", parse_mode="HTML")
            if not problems:
                await update.message.reply_text(
                    f"✅ За последние 2ч нет каналов/эндпоинтов ниже порога ({CONVERSION_THRESHOLD}%, FTD {_jobs.FTD_THRESHOLD}%).",
                    parse_mode="HTML",
                )
            else:
                try:
                    unpaid = mb.get_unpaid_orders()
                except Exception:
                    unpaid = {}
                greeting = _jobs.get_greeting_by_time()
                await update.message.reply_text(
                    f"🔔 Проблем за 2ч: <b>{len(problems)}</b>", parse_mode="HTML"
                )
                for p in problems:
                    if p["kind"] == "channel":
                        txt2 = _jobs._format_channel_alert(p, greeting, unpaid, _jobs.CONV_WINDOW_MIN)
                    else:
                        txt2 = _jobs._format_endpoint_problem(p, greeting, unpaid, _jobs.CONV_WINDOW_MIN)
                    await update.message.reply_text(txt2, parse_mode="HTML")

        elif txt == BTN_ENDPOINT_DETAIL:
            pcode = "td"
            try:
                tree = _epd_data(pcode)
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка загрузки: {str(e)[:80]}", parse_mode="HTML")
                tree = {}
            merchants = _epd_active_merchants(tree)
            context.user_data["epd_m"] = merchants
            if not merchants:
                await update.message.reply_text("❌ Нет данных по эндпоинтам", parse_mode="HTML")
            else:
                await update.message.reply_text(
                    _epd_picker_text(pcode, merchants),
                    parse_mode="HTML", reply_markup=kb_epd_picker(merchants, pcode),
                )

        elif txt == BTN_CANCELED:
            canceled = mb.get_canceled_analysis()
            text = format_canceled(canceled)
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_refresh("refresh_canceled"))

        elif txt == BTN_ALERTS:
            alerts = mb.check_alerts()
            text = format_alerts(alerts)
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_refresh("refresh_alerts"))

        elif txt == BTN_BAD_CLIENTS:
            bad_clients = mb.get_bad_clients()
            text = format_bad_clients(bad_clients)
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_refresh("refresh_bad_clients"))

        elif txt == BTN_SEARCH:
            context.user_data["mode"] = "search"
            await update.message.reply_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔍  <b>ПОИСК МЕРЧАНТА</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Введите название мерчанта:",
                parse_mode="HTML",
                reply_markup=kb_cancel()
            )

        elif txt == BTN_EXPORT:
            await update.message.reply_text(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📤  <b>ВЫГРУЗКА ОТЧЁТОВ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "Выберите данные для экспорта:",
                parse_mode="HTML",
                reply_markup=kb_export()
            )

        elif txt == BTN_RATES:
            # Ставки/комиссии ведутся вручную в Google Sheets — отдаём ссылку,
            # вместо пустого ответа от stub `get_rates_by_method`.
            await update.message.reply_text(
                "💰 <b>Ставки и комиссии</b>\n\n"
                "Актуальный каталог ставок ведётся в Google Sheets:\n"
                "<a href=\"https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID\">📊 Открыть таблицу ставок</a>\n\n"
                "<i>(в таблице: ставки по мерчам/методам, лимиты, settlement, geo — RU/СНГ/Турция/Европа/Латам)</i>",
                parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=kb_main(is_admin(update))
            )

        elif txt == BTN_ADD_MERCHANT:
            await add_merchant_start(update, context)

        elif txt == BTN_EDIT_MERCHANT:
            await edit_merchant_start(update, context)

        elif txt == BTN_NEW_INCIDENT:
            await inc_create_start(update, context)

        elif txt == BTN_INCIDENTS:
            await show_incidents_list(update, context)

        elif txt == BTN_ADMIN:
            if not is_admin(update):
                await update.message.reply_text("⛔ Нет доступа")
                return
            stats = keys.get_stats()
            text = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🔐  <b>АДМИН-ПАНЕЛЬ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📊 <b>Статистика:</b>\n"
                f"   Всего ключей: {stats['total_keys']}\n"
                f"   Активных: {stats['active_keys']}\n"
                f"   Использовано: {stats['used_keys']}\n"
                f"   Пользователей: {stats['authorized_users']}\n\n"
                "Выберите действие:"
            )
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_admin())

        elif txt == BTN_UPLOAD_EXPORT:
            await update.message.reply_text(
                "📂 <b>Загрузка выгрузки чатов</b>\n\n"
                "Отправьте файл экспорта из Telegram Desktop:\n"
                "• <b>JSON</b> — экспорт одного чата\n"
                "• <b>ZIP</b> — полный экспорт всех чатов\n\n"
                "<i>Telegram Desktop → ⋮ → Export chat history → Format: JSON</i>\n"
                "<i>Или: Настройки → Продвинутые → Экспорт данных</i>",
                parse_mode="HTML"
            )

        elif txt == BTN_ACTIVE_SUMMARY:
            await update.message.reply_text("⏳ <b>Генерирую сводку по активным мерчантам...</b>", parse_mode="HTML")
            try:
                summary = await _generate_active_merchants_summary()
                for chunk in _split_message(summary):
                    await update.message.reply_text(chunk, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Active summary error: {e}")
                await update.message.reply_text(f"❌ Ошибка генерации сводки: {str(e)[:200]}", parse_mode="HTML")

        else:
            await update.message.reply_text(
                "Выберите раздел из меню ниже 👇",
                reply_markup=kb_main(is_admin(update))
            )

    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка: {str(e)[:100]}",
            reply_markup=kb_main(is_admin(update))
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка загруженных файлов — JSON/ZIP экспорт из Telegram Desktop"""
    if not is_authenticated(update):
        await update.message.reply_text("🔐 Требуется авторизация")
        return

    doc = update.message.document
    if not doc:
        return

    fname = (doc.file_name or "").lower()

    # Supported formats: .json and .zip
    if not fname.endswith(".json") and not fname.endswith(".zip"):
        await update.message.reply_text(
            "⚠️ <b>Неподдерживаемый формат</b>\n\n"
            "Отправьте <b>JSON</b> или <b>ZIP</b> файл из экспорта Telegram Desktop.\n"
            "<i>Telegram Desktop → Настройки → Продвинутые → Экспорт данных → JSON</i>",
            parse_mode="HTML"
        )
        return

    # Size limit (20 MB)
    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ Файл слишком большой (максимум 20 МБ)")
        return

    status_msg = await update.message.reply_text("⏳ <b>Загружаю и обрабатываю файл...</b>", parse_mode="HTML")

    try:
        # Download file
        file = await doc.get_file()
        file_bytes = await file.download_as_bytearray()

        # Parse JSON from file or ZIP
        if fname.endswith(".zip"):
            zip_buf = io.BytesIO(file_bytes)
            with zipfile.ZipFile(zip_buf, 'r') as zf:
                # Look for result.json inside ZIP
                json_name = None
                for name in zf.namelist():
                    if name.endswith("result.json") or name.endswith(".json"):
                        json_name = name
                        break
                if not json_name:
                    await status_msg.edit_text(
                        "⚠️ <b>В ZIP-архиве не найден JSON файл</b>\n\n"
                        "Ожидается <code>result.json</code> внутри архива.",
                        parse_mode="HTML"
                    )
                    return
                content = zf.read(json_name).decode("utf-8")
                data = json.loads(content)
        else:
            content = file_bytes.decode("utf-8")
            data = json.loads(content)

        # Detect format: single chat vs full export
        if "chats" in data and "list" in data.get("chats", {}):
            # Full export format
            report = await _process_full_export(data, status_msg)
        elif "messages" in data:
            # Single chat format
            report = await _process_single_chat(
                data, status_msg,
                chat_name_fallback=(doc.file_name or "").replace(".json", "")
            )
        else:
            report = (
                "⚠️ <b>Неверный формат файла</b>\n\n"
                "Файл не похож на экспорт из Telegram Desktop.\n"
                "Нужен файл <code>result.json</code> с полем <code>messages</code> или <code>chats</code>."
            )

        await status_msg.edit_text(report, parse_mode="HTML")

    except json.JSONDecodeError:
        await status_msg.edit_text(
            "⚠️ <b>Ошибка парсинга JSON</b>\n\n"
            "Файл повреждён или имеет неверный формат.\n"
            "Убедитесь что экспортируете в формате JSON (Machine-readable).",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error processing chat export: {e}")
        await status_msg.edit_text(
            f"❌ <b>Ошибка обработки</b>\n\n"
            f"<code>{html.escape(str(e)[:200])}</code>",
            parse_mode="HTML"
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка inline-кнопок"""
    if not is_authenticated(update):
        await update.callback_query.answer("🔐 Требуется авторизация")
        return

    query = update.callback_query
    data = query.data

    await query.answer("Обновляю...")

    try:
        # === ЭКСПОРТ ===
        if data.startswith("export_"):
            export_type = data.replace("export_", "")
            date_str = datetime.now(MSK).strftime('%Y-%m-%d')

            if export_type == "merchants":
                merchants = mb.get_conversion_today()
                file = export_merchants_csv(merchants)
                await query.message.reply_document(
                    document=file,
                    filename=f"merchants_{date_str}.csv",
                    caption="📊 Статистика мерчантов"
                )

            elif export_type == "methods":
                methods = mb.get_conversion_by_method()
                file = export_methods_csv(methods)
                await query.message.reply_document(
                    document=file,
                    filename=f"methods_{date_str}.csv",
                    caption="💳 Статистика методов оплаты"
                )

            elif export_type == "hourly":
                hours = mb.get_hourly_stats()
                file = export_hourly_csv(hours)
                await query.message.reply_document(
                    document=file,
                    filename=f"hourly_{date_str}.csv",
                    caption="🕐 Почасовая статистика"
                )

            elif export_type == "conv_history":
                file = export_conv_history_csv(days=7)
                await query.message.reply_document(
                    document=file,
                    filename=f"conv_history_{date_str}.csv",
                    caption="📈 История конверсии за 7 дней"
                )

            elif export_type == "incidents":
                file = export_incidents_csv(days=30)
                await query.message.reply_document(
                    document=file,
                    filename=f"incidents_{date_str}.csv",
                    caption="🚨 Инциденты за 30 дней"
                )

            elif export_type == "all":
                merchants = mb.get_conversion_today()
                methods = mb.get_conversion_by_method()
                hours = mb.get_hourly_stats()

                # ZIP архив со всеми отчётами
                zip_buffer = io.BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr(f"merchants_{date_str}.csv", export_merchants_csv(merchants).getvalue())
                    zf.writestr(f"methods_{date_str}.csv", export_methods_csv(methods).getvalue())
                    zf.writestr(f"hourly_{date_str}.csv", export_hourly_csv(hours).getvalue())
                    zf.writestr(f"conv_history_{date_str}.csv", export_conv_history_csv(7).getvalue())
                    zf.writestr(f"incidents_{date_str}.csv", export_incidents_csv(30).getvalue())
                zip_buffer.seek(0)
                await query.message.reply_document(
                    document=zip_buffer,
                    filename=f"crm_export_{date_str}.zip",
                    caption="📋 Все отчёты (ZIP)"
                )
            return

        # === АДМИН ПАНЕЛЬ ===
        if data == "admin_newkey":
            if not is_admin(update):
                await query.answer("⛔ Нет доступа")
                return
            key = keys.generate_key(name="", created_by=update.effective_user.id)
            storage.log_audit(update.effective_user.id, "key_created", f"bot: new key by admin")
            await query.answer("Ключ создан!")
            await query.message.reply_text(
                f"🔑 <b>Новый ключ доступа:</b>\n\n"
                f"<code>{key}</code>\n\n"
                f"<i>Отправьте этот ключ пользователю для доступа к боту.</i>",
                parse_mode="HTML"
            )
            return

        if data == "admin_keys":
            if not is_admin(update):
                await query.answer("⛔ Нет доступа")
                return
            all_keys = keys.get_all_keys()
            if not all_keys:
                await query.edit_message_text(
                    "📋 <b>Ключи доступа</b>\n\nНет созданных ключей",
                    parse_mode="HTML",
                    reply_markup=kb_admin()
                )
                return

            lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "📋  <b>КЛЮЧИ ДОСТУПА</b>", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]
            for k in all_keys[-15:]:
                status = "🟢" if k["active"] and not k["used_by"] else "🔵" if k["used_by"] else "🔴"
                used = f"→ {k['used_by']}" if k["used_by"] else "свободен"
                lines.append(f"{status} <code>{k['key']}</code>")
                lines.append(f"   {used}")

            await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_admin())
            return

        if data == "admin_users":
            if not is_admin(update):
                await query.answer("⛔ Нет доступа")
                return
            users = keys.get_authorized_users()
            if not users:
                await query.edit_message_text(
                    "👥 <b>Пользователи</b>\n\nНет авторизованных пользователей",
                    parse_mode="HTML",
                    reply_markup=kb_admin()
                )
                return

            lines = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "👥  <b>АВТОРИЗОВАННЫЕ</b>", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━", ""]
            for u in users:
                lines.append(f"👤 <code>{u['user_id']}</code>")
                lines.append(f"   🔑 {u['key'][:12]}...")
                if u.get('used_at'):
                    lines.append(f"   📅 {u['used_at'][:10]}")
                lines.append("")

            await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb_admin())
            return

        if data == "admin_stats":
            if not is_admin(update):
                await query.answer("⛔ Нет доступа")
                return
            stats = keys.get_stats()
            text = (
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "📊  <b>СТАТИСТИКА КЛЮЧЕЙ</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"🔑 Всего ключей: <b>{stats['total_keys']}</b>\n"
                f"🟢 Активных: <b>{stats['active_keys']}</b>\n"
                f"🔵 Использовано: <b>{stats['used_keys']}</b>\n"
                f"👥 Пользователей: <b>{stats['authorized_users']}</b>\n"
            )
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb_admin())
            return

        # === ОБНОВЛЕНИЕ ===

        # Специальная обработка для refresh_low_conversion (отправляет отдельные сообщения с картинками)
        if data == "refresh_low_conversion":
            await query.answer("Обновляю данные по конверсии...")

            merchants = mb.get_conversion_today()
            unpaid_orders = mb.get_unpaid_orders()
            try:
                hourly_by_merchant = mb.get_hourly_by_merchant()
            except Exception:
                hourly_by_merchant = {}
            try:
                conv_by_method = mb.get_conv_by_merchant_method()
            except Exception:
                conv_by_method = {}
            merchant_statuses = get_merchants_status_map()

            # Находим мерчантов с низкой конверсией (по общей)
            low_conv_merchants = []
            for m in merchants:
                name = m.get("Мерчант") or ""
                if "ИТОГО" in name:
                    continue
                if merchant_statuses.get(name) == 'disconnected':
                    continue
                if name in EXCLUDED_MERCHANTS:
                    continue

                total = m.get("Всего") or 0
                with_req = m.get("С реквизитами") or 0
                conv = m.get("Конверсия в оплату %") or 0

                if with_req >= MIN_ORDERS_WITH_REQ_FOR_ALERT and conv < CONVERSION_THRESHOLD:
                    low_conv_merchants.append({
                        "name": name, "total": total, "with_req": with_req,
                        "conv": conv, "successful": m.get("Успешных") or 0,
                    })

            if not low_conv_merchants:
                await query.message.reply_text(
                    "✅ <b>Все мерчанты имеют нормальную конверсию</b>\n\n"
                    f"Порог: {CONVERSION_THRESHOLD}%",
                    parse_mode="HTML"
                )
                return

            greeting = get_greeting_by_time()

            # Отправляем сообщения для каждого мерчанта
            for m in low_conv_merchants:
                conv = m['conv']
                name = m['name']
                total = m['total']
                with_req = m['with_req']
                successful = m['successful']

                if conv < 10:
                    emoji = "🔴"
                    template_text = get_template_variant('critical')
                elif conv < 20:
                    emoji = "⚠️"
                    template_text = get_template_variant('low')
                else:
                    emoji = "💛"
                    template_text = get_template_variant('warning')

                template = f"Коллеги, {greeting}!\n\n{template_text}"

                lines = [
                    f"{emoji} <b>{escape(name)}</b>",
                    "",
                    f"📊 Конверсия в оплату: <b>{conv:.1f}%</b>",
                    f"📋 С реквизитами: {with_req} → Оплатили: {successful}",
                ]

                merchant_methods = conv_by_method.get(name, [])
                methods_with_req = [ms for ms in merchant_methods if ms.get('with_req', 0) > 0]
                if methods_with_req:
                    lines.append("")
                    lines.append("💳 <b>По методам:</b>")
                    for ms in sorted(methods_with_req, key=lambda x: x.get('with_req', 0), reverse=True):
                        m_name = ms.get('method', '?')
                        m_conv = ms.get('conv_pct', 0) or 0
                        m_req = ms.get('with_req', 0)
                        m_succ = ms.get('successful', 0)
                        lines.append(f"   {m_name}: <b>{m_conv:.1f}%</b> ({m_succ}/{m_req})")

                lines.extend([
                    "",
                    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                    "",
                    template,
                ])

                merchant_unpaid = unpaid_orders.get(name, [])
                if merchant_unpaid and isinstance(merchant_unpaid, list):
                    lines.append("")
                    lines.append("📋 <b>Примеры неоплаченных заявок:</b>")
                    for item in merchant_unpaid[:10]:
                        if isinstance(item, dict):
                            ext = item.get('order_id', '?')
                            method = item.get('method', '')
                            method_str = f" · {method}" if method else ""
                            lines.append(f"• <code>{ext}</code>{method_str}")
                        else:
                            lines.append(f"• <code>{item}</code>")

                lines.append("")
                lines.append(f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")

                text = "\n".join(lines)

                try:
                    chart_image = visualization.generate_conversion_chart(
                        merchant_name=name,
                        total=total,
                        with_req=with_req,
                        successful=successful,
                        conv=conv,
                        hourly_data=hourly_by_merchant.get(name),
                        method_stats_pg=merchant_methods,
                    )
                    await query.message.reply_photo(
                        photo=chart_image,
                        caption=text,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"Ошибка генерации визуализации: {e}")
                    await query.message.reply_text(text, parse_mode="HTML")
            return

        # === CRM: АЛЕРТ КНОПКИ ===
        if data.startswith("alert_"):
            await handle_alert_callback(update, context, data)
            return

        # === CRM: ИНЦИДЕНТЫ ===
        if (data.startswith("inc_") or data.startswith("inc_type:") or
                data.startswith("inc_prio:") or data.startswith("inc_due:")):
            await handle_inc_callback(update, context, data)
            return

        # === CRM: МЕРЧАНТЫ (редактирование, история) ===
        if (data.startswith("medit_") or data.startswith("mset_") or data.startswith("mhist:")):
            await handle_medit_callback(update, context, data)
            return

        # === Активные реки: переключение окна / обновление ===
        if data.startswith("active_req:"):
            try:
                mins = int(data.split(":", 1)[1])
            except (ValueError, IndexError):
                mins = 15
            await query.edit_message_text(
                _build_active_req(mins), parse_mode="HTML",
                reply_markup=kb_active_req(mins),
            )
            return

        # === Последние реки мерча: drill-down ===
        if data.startswith("lr:"):
            await handle_lr_callback(query, data)
            return

        # === Оборот по эндпоинту: мерч → эндпоинт → период ===
        if data.startswith("tov:"):
            await handle_tov_callback(query, data)
            return

        # === Конверсии (наша/мерча/без дублей): мерч → период ===
        if data.startswith("cv:"):
            await handle_cv_callback(query, data)
            return

        # === Excel-отчёт по ошибкам мерча (merchant-clean) ===
        if data.startswith("er:"):
            await handle_er_callback(query, data)
            return

        # === Фрод-юзеры мерча (дубли без оплаты) ===
        if data.startswith("fr:"):
            await handle_fr_callback(query, data)
            return

        # === Эндпоинты детально: пикер мерчей / карточка мерча / сводка — с выбором периода ===
        if data.startswith("epd:"):
            parts = data.split(":")
            sub = parts[1] if len(parts) > 1 else "pick"
            if sub == "pick":
                pcode = parts[2] if len(parts) > 2 else "td"
                tree = _epd_data(pcode)
                merchants = _epd_active_merchants(tree)
                context.user_data["epd_m"] = merchants
                await query.edit_message_text(
                    _epd_picker_text(pcode, merchants), parse_mode="HTML",
                    reply_markup=kb_epd_picker(merchants, pcode),
                )
            elif sub == "all":
                pcode = parts[2] if len(parts) > 2 else "td"
                tree = _epd_data(pcode)
                await query.edit_message_text(
                    _epd_all_text(tree, pcode), parse_mode="HTML",
                    reply_markup=kb_epd_all(pcode),
                )
            elif sub == "v":
                try:
                    idx = int(parts[2])
                except (ValueError, IndexError):
                    idx = -1
                pcode = parts[3] if len(parts) > 3 else "td"
                merchants = context.user_data.get("epd_m", [])
                if idx < 0 or idx >= len(merchants):
                    await query.answer("Список устарел — откройте «📊 Эндпоинты детально» заново", show_alert=True)
                    return
                name = merchants[idx]
                tree = _epd_data(pcode)
                await query.edit_message_text(
                    _epd_merchant_text(name, tree.get(name, {}), pcode), parse_mode="HTML",
                    reply_markup=kb_epd_view(idx, pcode),
                )
            elif sub == "h":
                try:
                    idx = int(parts[2])
                except (ValueError, IndexError):
                    idx = -1
                pcode = parts[3] if len(parts) > 3 else "td"
                merchants = context.user_data.get("epd_m", [])
                if idx < 0 or idx >= len(merchants):
                    await query.answer("Список устарел — откройте «📊 Эндпоинты детально» заново", show_alert=True)
                    return
                name = merchants[idx]
                rows = mb.get_merchant_hourly(name)
                await query.edit_message_text(
                    _epd_hourly_text(name, rows), parse_mode="HTML",
                    reply_markup=kb_epd_hourly(idx, pcode),
                )
            return

        # === Поведение юзеров: переключение периода / обновление ===
        if data.startswith("behavior:"):
            try:
                hrs = int(data.split(":", 1)[1])
            except (ValueError, IndexError):
                hrs = 24
            await query.edit_message_text(
                _build_behavior(hrs), parse_mode="HTML",
                reply_markup=kb_behavior(hrs),
            )
            return

        # Остальные обработчики обновления
        refresh_handlers = {
            "refresh_snapshot": _build_snapshot,
            "refresh_lasthour": _build_lasthour,
            "refresh_today": lambda: format_today(mb.get_main_dashboard()),
            "refresh_conversion": lambda: format_conversion_detailed(mb.get_conversion_today()),
            "refresh_canceled": lambda: format_canceled(mb.get_canceled_analysis()),
            "refresh_alerts": lambda: format_alerts(mb.check_alerts()),
            "refresh_bad_clients": lambda: format_bad_clients(mb.get_bad_clients()),
        }

        if data in refresh_handlers:
            text = refresh_handlers[data]()
            await query.edit_message_text(
                text, parse_mode="HTML",
                reply_markup=kb_refresh(data)
            )

    except Exception as e:
        if "not modified" not in str(e).lower():
            try:
                await query.edit_message_text(f"❌ Ошибка: {str(e)[:100]}")
            except:
                pass


def check_config():
    """Проверка конфигурации перед запуском"""
    errors = []

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        errors.append("BOT_TOKEN не задан")

    if not ADMIN_IDS:
        errors.append("ADMIN_IDS не задан")

    from config import ANALYTICS_DB_URL
    if not ANALYTICS_DB_URL:
        errors.append("ANALYTICS_DB_URL не задан (read-only реплика Core)")

    from config import PASSWORD_SECRET
    if not PASSWORD_SECRET or PASSWORD_SECRET == "dev-fallback-secret-change-in-prod":
        errors.append("PASSWORD_SECRET не задан/дефолтный — задайте стойкий секрет в .env "
                      "(иначе хеши ключей доступа предсказуемы)")

    return errors


async def webapp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открыть CRM Mini App"""
    if not WEBAPP_URL:
        await update.message.reply_text("⚠️ Mini App не настроен (нет WEBAPP_URL)")
        return
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("Открыть CRM", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await update.message.reply_text("CRM панель:", reply_markup=keyboard)


# ── «Последние реки мерча»: drill-down мерч → эндпоинт → лимит → фильтр → список ──

_LR_TRAFFIC_DAYS = 7
_LR_KIND_LABEL = {"a": "просто последние", "y": "рек выдан", "n": "рек не выдан"}
_LR_KIND_TO_MB = {"a": "all", "y": "issued", "n": "not_issued"}


def _lr_active_merchants():
    """Список мерчей с трафиком — отфильтрованный от EXCLUDED."""
    rows = mb.get_active_merchants(_LR_TRAFFIC_DAYS)
    return [m for m in rows
            if (m.get("name") not in EXCLUDED_MERCHANTS)
            and (m.get("iname") not in EXCLUDED_MERCHANTS)]


def _lr_merchant_label(iname: str) -> str:
    for m in _lr_active_merchants():
        if m["iname"] == iname:
            return m.get("name") or iname
    return iname


def _lr_status_label(o: dict) -> str:
    """Короткий маркер статуса для строки ордера."""
    sid = o.get("status_id")
    if sid in (3, 6):
        return "✅ ОПЛ"
    err = (o.get("error_code") or "").upper()
    orig = o.get("error_orig") or ""
    if err == "SESSION_EXPIRED" or "TTL exceeded" in orig:
        return "❌ TTL-10"
    if err == "CANCELLED":
        return "✋ CANCEL"
    if err == "ROUTING_ERROR":
        return "🚧 ROUTING"
    if o.get("deeplink_present"):
        # рек выдан, не оплачено и не TTL — клиент бросил
        return "🟡 рек выдан, не опл"
    if err and err not in ("", "COMMON", "COMMON_ERROR"):
        return f"❌ {err[:12]}"
    return "⚪ нет река"


def _lr_format_order(o: dict) -> str:
    t = o["created_at_msk"].strftime("%d.%m %H:%M") if o.get("created_at_msk") else "—"
    try:
        price = int(float(o.get("price") or 0))
    except Exception:
        price = 0
    price_s = f"{price:,}".replace(",", " ")
    sl = _lr_status_label(o)
    gate = o.get("gate") or "—"
    short_id = (o.get("id") or "").split("-")[0]
    return f"• {t} · {price_s}₽ · {sl} · <i>{escape(gate)}</i> · <code>{escape(short_id)}</code>"


def _lr_build_list_text(merchant_iname, endpoint, limit, kind, orders):
    name = _lr_merchant_label(merchant_iname)
    kind_label = _LR_KIND_LABEL.get(kind, "—")
    header = (f"🧾 <b>{escape(name)}</b> → <code>{escape(endpoint)}</code>\n"
              f"Последние {len(orders)}/{limit} · фильтр: <b>{kind_label}</b>")
    if not orders:
        return header + "\n\n— Нет данных по выбранному фильтру."
    return header + "\n\n" + "\n".join(_lr_format_order(o) for o in orders)


async def _lr_send_root(message):
    """Стартовый экран: список мерчей. Шлёт НОВОЕ сообщение (reply)."""
    merchants = _lr_active_merchants()
    if not merchants:
        await message.reply_text("Нет мерчей с трафиком за последние 7 дней.")
        return
    text = ("🧾 <b>Последние реки мерча</b>\n\n"
            f"Выберите мерча (за последние {_LR_TRAFFIC_DAYS} дней):")
    await message.reply_text(text, parse_mode="HTML",
                             reply_markup=kb_lr_merchants(merchants))


async def _lr_edit_merchants(query):
    merchants = _lr_active_merchants()
    text = ("🧾 <b>Последние реки мерча</b>\n\n"
            f"Выберите мерча (за последние {_LR_TRAFFIC_DAYS} дней):")
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_lr_merchants(merchants))


async def _lr_edit_endpoints(query, merchant_iname):
    eps = mb.get_endpoints_for_merchant(merchant_iname, _LR_TRAFFIC_DAYS)
    name = _lr_merchant_label(merchant_iname)
    if not eps:
        text = f"🧾 <b>{escape(name)}</b>\n\nНет эндпоинтов с трафиком за {_LR_TRAFFIC_DAYS} дн."
    else:
        text = (f"🧾 <b>{escape(name)}</b>\n\n"
                f"Выберите эндпоинт (заявок за {_LR_TRAFFIC_DAYS} дн):")
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_lr_endpoints(merchant_iname, eps))


async def _lr_edit_count(query, merchant_iname, endpoint):
    name = _lr_merchant_label(merchant_iname)
    text = (f"🧾 <b>{escape(name)}</b> → <code>{escape(endpoint)}</code>\n\n"
            f"Сколько последних ордеров показать?")
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_lr_count(merchant_iname, endpoint))


async def _lr_edit_kind(query, merchant_iname, endpoint, limit):
    name = _lr_merchant_label(merchant_iname)
    text = (f"🧾 <b>{escape(name)}</b> → <code>{escape(endpoint)}</code>\n\n"
            f"Какой фильтр на {limit} последних?")
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_lr_kind(merchant_iname, endpoint, limit))


async def _lr_edit_list(query, merchant_iname, endpoint, limit, kind):
    mb_kind = _LR_KIND_TO_MB.get(kind, "all")
    orders = mb.get_last_orders(merchant_iname, endpoint, limit, mb_kind)
    text = _lr_build_list_text(merchant_iname, endpoint, limit, kind, orders)
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_lr_list(merchant_iname, endpoint, limit, kind))


async def handle_lr_callback(query, data):
    """Диспатч lr:* callback'ов. Формат:
       lr:m:<iname>                       — выбран мерч
       lr:e:<iname>:<ep>                  — выбран эндпоинт
       lr:c:<iname>:<ep>:<lim>            — выбран лимит (открыть выбор kind)
       lr:s:<iname>:<ep>:<lim>:<kind>     — показать список
       lr:back:m                          — ↑ к мерчам
       lr:back:e:<iname>                  — ↑ к эндпоинтам
       lr:back:c:<iname>:<ep>             — ↑ к выбору лимита
    """
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "back":
        sub = parts[2] if len(parts) > 2 else "m"
        if sub == "m":
            await _lr_edit_merchants(query)
        elif sub == "e" and len(parts) >= 4:
            await _lr_edit_endpoints(query, parts[3])
        elif sub == "c" and len(parts) >= 5:
            await _lr_edit_count(query, parts[3], parts[4])
        return
    if action == "m" and len(parts) >= 3:
        await _lr_edit_endpoints(query, parts[2])
        return
    if action == "e" and len(parts) >= 4:
        await _lr_edit_count(query, parts[2], parts[3])
        return
    if action == "c" and len(parts) >= 5:
        await _lr_edit_kind(query, parts[2], parts[3], int(parts[4]))
        return
    if action == "s" and len(parts) >= 6:
        await _lr_edit_list(query, parts[2], parts[3], int(parts[4]), parts[5])
        return


# ── Оборот по эндпоинту: мерч → эндпоинт → период → оборот + конв в успех ──
_TOV_PERIOD_LABEL = {"d0": "за сутки (сегодня)", "d7": "за 7 дней", "d30": "за 30 дней"}


def _tov_build_text(merchant_iname, endpoint, pcode):
    name = _lr_merchant_label(merchant_iname)
    label = _TOV_PERIOD_LABEL.get(pcode, "за 7 дней")
    d = mb.get_endpoint_turnover(merchant_iname, endpoint, pcode)
    total, succ, conv = d["total"], d["success"], d["conv"]
    money = _snap_money_multi(d["amounts"]) if d["amounts"] else _snap_money(0)
    header = (f"💸 <b>{escape(name)}</b> → <code>{escape(endpoint)}</code>\n"
              f"<i>{label}</i>")
    if total == 0:
        return header + "\n\n— Нет заявок за выбранный период."
    return (f"{header}\n\n"
            f"💰 Оборот: <b>{money}</b>\n"
            f"{_epd_icon(conv)} Конверсия в успех: <b>{conv:.1f}%</b> "
            f"({succ}/{total})")


async def _tov_send_root(message):
    """Стартовый экран: список мерчей (новое сообщение)."""
    merchants = _lr_active_merchants()
    if not merchants:
        await message.reply_text("Нет мерчей с трафиком за последние 7 дней.")
        return
    text = ("💸 <b>Оборот по эндпоинту</b>\n\n"
            f"Выберите мерча (за последние {_LR_TRAFFIC_DAYS} дней):")
    await message.reply_text(text, parse_mode="HTML",
                             reply_markup=kb_tov_merchants(merchants))


async def _tov_edit_merchants(query):
    merchants = _lr_active_merchants()
    text = ("💸 <b>Оборот по эндпоинту</b>\n\n"
            f"Выберите мерча (за последние {_LR_TRAFFIC_DAYS} дней):")
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_tov_merchants(merchants))


async def _tov_edit_endpoints(query, merchant_iname):
    eps = mb.get_endpoints_for_merchant(merchant_iname, _LR_TRAFFIC_DAYS)
    name = _lr_merchant_label(merchant_iname)
    if not eps:
        text = f"💸 <b>{escape(name)}</b>\n\nНет эндпоинтов с трафиком за {_LR_TRAFFIC_DAYS} дн."
    else:
        text = (f"💸 <b>{escape(name)}</b>\n\n"
                f"Выберите эндпоинт (заявок за {_LR_TRAFFIC_DAYS} дн):")
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_tov_endpoints(merchant_iname, eps))


async def _tov_edit_period(query, merchant_iname, endpoint):
    name = _lr_merchant_label(merchant_iname)
    text = (f"💸 <b>{escape(name)}</b> → <code>{escape(endpoint)}</code>\n\n"
            f"За какой период показать оборот?")
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_tov_period(merchant_iname, endpoint))


async def _tov_edit_result(query, merchant_iname, endpoint, pcode):
    text = _tov_build_text(merchant_iname, endpoint, pcode)
    await query.edit_message_text(text, parse_mode="HTML",
                                   reply_markup=kb_tov_result(merchant_iname, endpoint, pcode))


async def handle_tov_callback(query, data):
    """Диспатч tov:* callback'ов. Формат:
       tov:m:<iname>                  — выбран мерч → эндпоинты
       tov:e:<iname>:<ep>             — выбран эндпоинт → выбор периода
       tov:s:<iname>:<ep>:<pcode>     — выбран период → оборот + конв
       tov:back:m                     — ↑ к мерчам
       tov:back:e:<iname>             — ↑ к эндпоинтам
    """
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "back":
        sub = parts[2] if len(parts) > 2 else "m"
        if sub == "m":
            await _tov_edit_merchants(query)
        elif sub == "e" and len(parts) >= 4:
            await _tov_edit_endpoints(query, parts[3])
        return
    if action == "m" and len(parts) >= 3:
        await _tov_edit_endpoints(query, parts[2])
        return
    if action == "e" and len(parts) >= 4:
        await _tov_edit_period(query, parts[2], parts[3])
        return
    if action == "s" and len(parts) >= 5:
        await _tov_edit_result(query, parts[2], parts[3], parts[4])
        return


# ── Лимиты гейтов (D9) ──
def _split_html(text, limit=3800):
    """Режет длинный текст на части по строкам (для лимита Telegram 4096)."""
    chunks, buf = [], ""
    for ln in text.split("\n"):
        piece = ("\n" if buf else "") + ln
        if buf and len(buf) + len(piece) > limit:
            chunks.append(buf); buf = ln
        else:
            buf += piece
    if buf:
        chunks.append(buf)
    return chunks or [text]


def _build_gate_limits():
    try:
        rows = mb.get_gate_limits()
    except Exception as e:
        return f"❌ Ошибка: {escape(str(e)[:160])}"
    if not rows:
        return "Активных invoice-терминалов не найдено."
    by_gate = {}
    for r in rows:
        by_gate.setdefault(r["gate"], []).append(r)
    out = ["🏦 <b>Лимиты активных гейтов/терминалов</b>", ""]
    for gate in sorted(by_gate):
        out.append(f"<b>{escape(gate)}</b>")
        for r in by_gate[gate]:
            mn = int(r["min_amount"] or 0); mx = int(r["max_amount"] or 0)
            rng = f"{mn:,}–{mx:,} ₽".replace(",", " ") if mx else f"от {mn:,} ₽".replace(",", " ")
            cap = ""
            if r.get("day_limit"):
                used = int(r.get("day_used") or 0); lim = int(r["day_limit"])
                pct = round(100.0 * used / lim) if lim else 0
                cap = f" · дн. {used:,}/{lim:,} ₽ ({pct}%)".replace(",", " ")
            out.append(f"   • {escape(str(r['terminal']))}: {rng}{cap}")
        out.append("")
    return "\n".join(out)


# ── Конверсии: наша / мерча / без дублей ──
_CV_PERIOD_LABEL = {"d0": "за сутки (сегодня)", "d7": "за 7 дней", "d30": "за 30 дней"}


def _cv_build_text(merchant_iname, pcode):
    name = _lr_merchant_label(merchant_iname)
    label = _CV_PERIOD_LABEL.get(pcode, "за 7 дней")
    d = mb.get_conversion_views(merchant_iname, pcode)
    if d["total"] == 0:
        return (f"🔢 <b>{escape(name)}</b> · <i>{label}</i>\n\n— Нет заявок за период.")
    return (
        f"🔢 <b>{escape(name)}</b> · <i>{label}</i>\n\n"
        f"Всего заявок: <b>{d['total']}</b> · оплачено: <b>{d['success']}</b>\n\n"
        f"{_epd_icon(d['conv_merchant'])} Как видит мерч (от всех): <b>{d['conv_merchant']:.1f}%</b>\n"
        f"{_epd_icon(d['conv_gate'])} Наша (от выдачи реквизита): <b>{d['conv_gate']:.1f}%</b>\n"
        f"{_epd_icon(d['conv_intent'])} Без дублей (по клиентам): <b>{d['conv_intent']:.1f}%</b>\n\n"
        f"Дублей-повторов: <b>{d['dupes']}</b> из {d['total']} "
        f"(уникальных клиентов: {d['uniq']})\n\n"
        f"<i>Расхождение «у вас X — у нас Y» = мерч считает от всех заявок, включая "
        f"недолетевшие и дубли. Реальная конверсия намерений — последняя строка.</i>"
    )


async def _cv_send_root(message):
    merchants = _lr_active_merchants()
    if not merchants:
        await message.reply_text("Нет мерчей с трафиком за последние 7 дней.")
        return
    await message.reply_text(
        "🔢 <b>Конверсии: наша / мерча / без дублей</b>\n\nВыберите мерча:",
        parse_mode="HTML", reply_markup=kb_cv_merchants(merchants))


async def _cv_edit_merchants(query):
    merchants = _lr_active_merchants()
    await query.edit_message_text(
        "🔢 <b>Конверсии: наша / мерча / без дублей</b>\n\nВыберите мерча:",
        parse_mode="HTML", reply_markup=kb_cv_merchants(merchants))


async def _cv_edit_result(query, merchant_iname, pcode):
    await query.edit_message_text(_cv_build_text(merchant_iname, pcode), parse_mode="HTML",
                                   reply_markup=kb_cv_result(merchant_iname, pcode))


async def handle_cv_callback(query, data):
    """cv:m:<iname> — выбран мерч → карточка (сутки); cv:s:<iname>:<pcode> — период; cv:back:m — к мерчам."""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "back":
        await _cv_edit_merchants(query)
        return
    if action == "m" and len(parts) >= 3:
        await _cv_edit_result(query, parts[2], "d0")
        return
    if action == "s" and len(parts) >= 4:
        await _cv_edit_result(query, parts[2], parts[3])
        return


# ── Excel-отчёт по ошибкам мерча (D4) ──
from handlers import err_report as _erep  # noqa: E402


async def handle_er_callback(query, data):
    """er:<iname>:<pcode> — сгенерировать и прислать xlsx разбора отказов мерча (без провайдера)."""
    parts = data.split(":")
    if len(parts) < 3:
        return
    iname, pcode = parts[1], parts[2]
    name = _lr_merchant_label(iname)
    await query.message.reply_text("📑 Готовлю Excel-отчёт по ошибкам…")
    try:
        rep = mb.get_error_report(iname, pcode)
        if rep["summary"]["total"] == 0:
            await query.message.reply_text("Нет заявок за период — отчёт пуст.")
            return
        label = _CV_PERIOD_LABEL.get(pcode, "за 7 дней")
        bio = _erep.build_error_xlsx(name, label, rep)
        fname = f"{iname}_ошибки_{pcode}.xlsx"
        await query.message.reply_document(document=bio, filename=fname,
                                           caption=f"Разбор отказов: {name} · {label}. "
                                                   f"Без провайдерских данных — можно переслать мерчу.")
    except Exception as e:
        logger.error(f"er report error: {e}")
        await query.message.reply_text(f"❌ Ошибка отчёта: {escape(str(e)[:160])}")


async def handle_fr_callback(query, data):
    """fr:<iname>:<pcode> — клиенты с пачкой заявок без единой оплаты (дубли/абьюз) + ID для мерча."""
    parts = data.split(":")
    if len(parts) < 3:
        return
    iname, pcode = parts[1], parts[2]
    name = _lr_merchant_label(iname)
    label = _CV_PERIOD_LABEL.get(pcode, "за 7 дней")
    try:
        rows = mb.get_spam_customers(iname, pcode, min_unpaid=5)
    except Exception as e:
        logger.error(f"fr error: {e}")
        await query.message.reply_text(f"❌ Ошибка: {escape(str(e)[:160])}")
        return
    if not rows:
        await query.message.reply_text(
            f"🚫 <b>{escape(name)}</b> · {label}\n\nКлиентов с 5+ заявками без оплаты не найдено.",
            parse_mode="HTML")
        return
    total_dupes = sum(int(r["cnt"]) for r in rows)
    out = [f"🚫 <b>Фрод-юзеры: {escape(name)}</b> · {label}",
           f"Клиентов с 5+ заявками без единой оплаты: <b>{len(rows)}</b> "
           f"(всего заявок-дублей: {total_dupes})", ""]
    ids_for_merchant = []
    for r in rows[:15]:
        orders = [str(o) for o in (r.get("orders") or []) if o]
        out.append(f"• {r['cnt']} заявок без оплаты: <code>{escape(', '.join(orders[:5]))}</code>")
        ids_for_merchant.extend(orders[:5])
    out.append("")
    out.append("<i>Похоже на дубли/абьюз — можно отправить мерчу для проверки антифродом.</i>")
    await query.message.reply_text("\n".join(out), parse_mode="HTML")
    # плоский список ID для пересылки мерчу
    if ids_for_merchant:
        flat = "Заявки от пользователей с повторными неоплатами:\n" + "\n".join(ids_for_merchant)
        await query.message.reply_text(f"<code>{escape(flat)}</code>", parse_mode="HTML")
