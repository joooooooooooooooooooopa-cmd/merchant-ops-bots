"""Acme RateBot — быстрый справочник ставок: мерчанты / методы / лимиты / провайдеры + поиск + AI."""
import os
import json
import time
import logging
import difflib
from html import escape

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes,
)

import secrets
from config import BOT_TOKEN, ACCESS_KEY, ADMIN_IDS, KEYS_FILE
import rates_db
import keys as keystore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ops_ratebot")

BTN_MERCH = "🏪 Мерчанты"
BTN_METHODS = "💳 Методы"
BTN_PROV = "🏦 Провайдеры"
BTN_ENDPOINTS = "📍 Эндпоинты"
BTN_PORTFOLIO = "🗂 Портфолио мерча"
BTN_REFRESH = "🔄 Обновить"
BTN_ADMIN = "🔐 Админ"


def _menu(uid):
    rows = [[KeyboardButton(BTN_MERCH), KeyboardButton(BTN_METHODS)],
            [KeyboardButton(BTN_PROV), KeyboardButton(BTN_ENDPOINTS)],
            [KeyboardButton(BTN_PORTFOLIO)],
            [KeyboardButton(BTN_REFRESH)]]
    if uid in ADMIN_IDS:
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

_OBJ_LABEL = {"invoice": "пай-ин", "withdrawal": "выплата", "refund": "возврат"}
_CACHE_TTL = 300
_cache = {"ts": 0}


# ───────────── авторизация (одноразовые ключи) ─────────────
def _is_authed(uid):
    return keystore.is_authorized(uid, ADMIN_IDS)


def _is_admin(uid):
    return uid in ADMIN_IDS


# ───────────── данные/кэш ─────────────
def _refresh(force=False):
    now = time.time()
    if not force and _cache.get("ts") and now - _cache["ts"] < _CACHE_TTL and "merch" in _cache:
        return
    merch = rates_db.get_merchant_rates()
    prov = rates_db.get_provider_rates()
    try:
        endp = rates_db.get_endpoint_rates()
    except Exception:
        endp = {}
    methods = {}
    for m, rs in merch.items():
        for r in rs:
            methods.setdefault(r["method"], []).append({"merchant": m, **r})
    # карта метод → мерчант → [эндпоинты] (для карточки метода под сценарий «провайдер включился»)
    meth_ep = {}
    for m, eps in endp.items():
        for e in eps:
            meth_ep.setdefault(e.get("method", "—"), {}).setdefault(m, []).append(e)
    _cache.update({
        "merch": merch, "prov": prov, "methods": methods,
        "endp": endp, "meth_ep": meth_ep,
        "merch_list": sorted(merch.keys(), key=str.lower),
        "method_list": sorted(methods.keys(), key=str.lower),
        "gate_list": sorted(prov.keys(), key=str.lower),
        "endp_list": sorted(endp.keys(), key=str.lower),
        "ts": now,
    })


def C(k):
    _refresh()
    return _cache[k]


# ───────────── форматирование ─────────────
def _band(r):
    if not (r.get("min") or r.get("max")):
        return ""
    lo = f"{int(r['min']):,}".replace(",", " ") if r.get("min") else "0"
    hi = f"{int(r['max']):,}".replace(",", " ") if r.get("max") else "∞"
    return f"  💰 {lo}–{hi}₽"


def _rate(r):
    s = f"<b>{r['percent']:g}%</b>"
    if r.get("fix"):
        s += f" + {r['fix']:g}₽"
    return s


def card_merchant(name):
    rs = C("merch").get(name, [])
    lines = [f"🏪 <b>{escape(name)}</b> — ставки:"]
    for r in sorted(rs, key=lambda x: -x["percent"]):
        lines.append(f"   • {escape(r['method'])}: {_rate(r)}{_band(r)}")
    return "\n".join(lines)


def _pf_icon(c):
    return "🟢" if c >= 40 else ("🟡" if c >= 25 else ("🟠" if c >= 10 else "🔴"))


def _pf_money(v):
    return f"{int(v):,}".replace(",", " ") + "₽"


def card_portfolio(name, days=7):
    pf = rates_db.get_merchant_portfolio(name, days)
    s = pf["summary"]
    eps = C("endp").get(name, [])
    lbl = {1: "сегодня (с 00:00 МСК)", 7: "7 дней", 30: "30 дней"}.get(days, f"{days} дн")
    lines = [f"🗂 <b>Портфолио — {escape(name)}</b>  ·  <i>{lbl}</i>", ""]
    lines.append(f"📊 Заявок {s['total']} · выдача {s['issued']} · опл {s['success']} · "
                 f"{_pf_icon(s['conv'])} <b>{s['conv']:.1f}%</b>")
    lines.append(f"💰 Оборот: <b>{_pf_money(s['amount'])}</b>")

    # методы + эндпоинты со ставками
    by_method = {}
    for e in eps:
        by_method.setdefault(e.get("method", "—"), []).append(e)
    if by_method:
        set_n = sum(1 for e in eps if e["is_set"])
        lines.append(f"\n🧩 <b>Методы и эндпоинты</b> (ставок {set_n}/{len(eps)}):")
        for meth in sorted(by_method):
            mset = sum(1 for e in by_method[meth] if e["is_set"])
            lines.append(f"  💳 <b>{escape(meth)}</b> — {mset}/{len(by_method[meth])} со ставкой")
            for e in sorted(by_method[meth], key=lambda x: x["endpoint"].lower()):
                mark = f"{_rate(e)}{_band(e)}" if e["is_set"] else "⚪ 0% (не проставлена)"
                lines.append(f"      └ <code>{escape(e['endpoint'])}</code>: {mark}")
    else:
        lines.append("\n🧩 Эндпоинты со ставками не заведены.")

    # реальные провайдеры за период
    if pf["gates"]:
        lines.append(f"\n🏦 <b>Провайдеры за {lbl}</b> (по факту трафика):")
        for g in pf["gates"][:8]:
            lines.append(f"  {_pf_icon(g['conv'])} {escape(g['gate'])}: {g['conv']:.0f}% "
                         f"({g['success']}/{g['total']})")
    else:
        lines.append(f"\n🏦 За {lbl} трафика не было.")
    return "\n".join(lines)


def _pf_kb(i, days):
    def b(lbl, d):
        return InlineKeyboardButton(("• " if d == days else "") + lbl, callback_data=f"pfp:{i}:{d}")
    return InlineKeyboardMarkup([[b("Сегодня", 1), b("7 дней", 7), b("30 дней", 30)]])


def card_endpoints(name):
    eps = C("endp").get(name, [])
    lines = [f"📍 <b>{escape(name)}</b> — ставки по эндпоинтам:"]
    for e in sorted(eps, key=lambda x: x["endpoint"].lower()):
        if e["is_set"]:
            mark = f"✅ {_rate(e)}{_band(e)}"
        else:
            mark = "⚪ <i>0% — не проставлена</i>"
        lines.append(f"   • <code>{escape(e['endpoint'])}</code>: {mark}")
    set_n = sum(1 for e in eps if e["is_set"])
    lines.append(f"\n<i>Проставлено {set_n} из {len(eps)} эндпоинтов.</i>")
    return "\n".join(lines)


def card_method(method):
    """Под сценарий «провайдер по методу включился»: кому можно включать обработку
    (ставка проставлена, с эндпоинтами) + кто заведён, но без ставки."""
    by_merch = C("meth_ep").get(method, {})
    ready, unset = [], []
    for m, eps in by_merch.items():
        (ready if any(e["is_set"] for e in eps) else unset).append((m, eps))
    lines = [f"💳 <b>{escape(method)}</b>"]
    if ready:
        lines.append("\n✅ <b>Ставка проставлена</b> — кому можно включать обработку:")
        ready.sort(key=lambda x: min((e["percent"] for e in x[1] if e["is_set"]), default=0))
        for m, eps in ready:
            rates = sorted({e["percent"] for e in eps if e["is_set"]})
            rate_s = f"{rates[0]:g}%" if len(rates) == 1 else f"{min(rates):g}–{max(rates):g}%"
            lines.append(f"• <b>{escape(m)}</b> — {rate_s}")
            for e in sorted(eps, key=lambda x: x["endpoint"].lower()):
                mark = f"{_rate(e)}{_band(e)}" if e["is_set"] else "⚪ 0% (не проставлена)"
                lines.append(f"    └ <code>{escape(e['endpoint'])}</code>: {mark}")
    else:
        lines.append("\n— по этому методу ставка ни у кого не проставлена —")
    if unset:
        names = ", ".join(escape(m) for m, _ in sorted(unset, key=lambda x: x[0].lower()))
        lines.append(f"\n⚪ <i>Заведено, но без ставки (0%) — сначала нужна ставка: {names}</i>")
    return "\n".join(lines)


def card_gate(gate):
    rs = C("prov").get(gate, [])
    lines = [f"🏦 <b>{escape(gate)}</b> — терминалы/ставки:"]
    for r in sorted(rs, key=lambda x: x.get("terminal") or ""):
        obj = "" if r.get("obj") == "invoice" else f" [{_OBJ_LABEL.get(r.get('obj'), r.get('obj'))}]"
        lines.append(f"   • {escape(r.get('terminal') or '—')}: {_rate(r)}{obj}{_band(r)}")
    return "\n".join(lines)


def _list_kb(items, prefix):
    rows, row = [], []
    for i, name in enumerate(items):
        row.append(InlineKeyboardButton(name[:28], callback_data=f"{prefix}:{i}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ───────────── хендлеры ─────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _is_authed(uid):
        context.user_data["awaiting_key"] = True
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n🔐  <b>ACME RATES</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nДля доступа введите ключ:", parse_mode="HTML")
        return
    await update.message.reply_text(
        "💰 <b>Справочник ставок Acme</b>\n\n"
        "Быстрый доступ: <b>Мерчанты</b> / <b>Методы</b> / <b>Провайдеры</b>.\n"
        "Или просто напишите запрос (мерчант, метод, эндпоинт).",
        parse_mode="HTML", reply_markup=_menu(update.effective_user.id))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()

    if context.user_data.get("awaiting_key") and not _is_authed(uid):
        if ACCESS_KEY and secrets.compare_digest(txt, ACCESS_KEY):
            keystore.authorize_user(uid, "master")
            context.user_data["awaiting_key"] = False
            await update.message.reply_text("✅ Доступ открыт.", reply_markup=_menu(uid))
            await start(update, context)
        elif keystore.validate_and_consume(txt, uid):
            context.user_data["awaiting_key"] = False
            await update.message.reply_text("✅ Доступ открыт (ключ активирован).", reply_markup=_menu(uid))
            await start(update, context)
        else:
            await update.message.reply_text("❌ Неверный или уже использованный ключ. Попробуйте ещё раз.")
        return
    if not _is_authed(uid):
        context.user_data["awaiting_key"] = True
        await update.message.reply_text("🔐 Введите ключ доступа:")
        return

    try:
        if txt == BTN_MERCH:
            await update.message.reply_text("🏪 Выберите мерчанта:", reply_markup=_list_kb(C("merch_list"), "m"))
            return
        if txt == BTN_METHODS:
            await update.message.reply_text("💳 Выберите метод:", reply_markup=_list_kb(C("method_list"), "meth"))
            return
        if txt == BTN_PROV:
            await update.message.reply_text("🏦 Выберите провайдера:", reply_markup=_list_kb(C("gate_list"), "g"))
            return
        if txt == BTN_ENDPOINTS:
            await update.message.reply_text("📍 Выберите мерчанта (ставки по эндпоинтам):",
                                            reply_markup=_list_kb(C("endp_list"), "e"))
            return
        if txt == BTN_PORTFOLIO:
            await update.message.reply_text("🗂 Выберите мерчанта (полное портфолио):",
                                            reply_markup=_list_kb(C("endp_list"), "pf"))
            return
        if txt == BTN_REFRESH:
            _refresh(force=True)
            await update.message.reply_text("🔄 Ставки обновлены.", reply_markup=_menu(update.effective_user.id))
            return
        if txt == BTN_ADMIN and _is_admin(uid):
            await update.message.reply_text(
                "🔐 <b>Админка — выдача доступов</b>\n\n"
                "Сгенерируйте одноразовый ключ и передайте сотруднику. "
                "Команды: /newkey · /keys · /users · /revoke &lt;ключ|user_id&gt;",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔑 Новый одноразовый ключ", callback_data="adm:newkey")],
                    [InlineKeyboardButton("📋 Ключи", callback_data="adm:keys"),
                     InlineKeyboardButton("👥 Пользователи", callback_data="adm:users")],
                ]))
            return

        await _search(update, txt)
    except Exception as e:
        logger.error(f"msg error: {e}")
        await update.message.reply_text(f"❌ Ошибка: {escape(str(e)[:120])}", reply_markup=_menu(update.effective_user.id))


async def _search(update, txt):
    q = txt.lower()
    merch, methods, prov = C("merch"), C("methods"), C("prov")
    blocks, shown = [], set()
    for n in merch:
        if q in n.lower():
            blocks.append(card_merchant(n)); shown.add(n)
    for meth in methods:
        if q in meth.lower():
            blocks.append(card_method(meth))
    for g in prov:
        if q in g.lower() or any(q in (r.get("terminal") or "").lower() for r in prov[g]):
            blocks.append(card_gate(g))
    if not blocks:
        close = difflib.get_close_matches(q, [n.lower() for n in merch] + [m.lower() for m in methods], n=3, cutoff=0.5)
        for n in merch:
            if n.lower() in close:
                blocks.append(card_merchant(n))
        for meth in methods:
            if meth.lower() in close:
                blocks.append(card_method(meth))
    if not blocks:
        hint = "Не нашёл. Используйте кнопки или поиск.\nМетоды: " + ", ".join(C("method_list")[:12])
        await update.message.reply_text(hint, reply_markup=_menu(update.effective_user.id))
        return
    out = "\n\n".join(blocks[:20])
    await update.message.reply_text(out[:4000], parse_mode="HTML", reply_markup=_menu(update.effective_user.id))


# ───────────── админка: ключи доступа ─────────────
def _fmt_keys():
    ks = keystore.list_keys()
    if not ks:
        return "Ключей пока нет. Создайте: /newkey"
    lines = ["📋 <b>Ключи доступа:</b>"]
    for k, r in sorted(ks.items(), key=lambda kv: kv[1].get("created_at", 0), reverse=True)[:30]:
        st = f"✅ использован (uid <code>{r['used_by']}</code>)" if r.get("used_by") else "🆕 свободен"
        lines.append(f"<code>{escape(k)}</code> — {st}")
    return "\n".join(lines)


def _fmt_users():
    us = keystore.list_users()
    if not us:
        return "Авторизованных пользователей нет."
    lines = ["👥 <b>Авторизованные пользователи:</b>"]
    for uid, r in us.items():
        lines.append(f"• <code>{escape(uid)}</code> (ключ: {escape(str(r.get('via_key')))})")
    return "\n".join(lines)


async def _admin_action(q, arg):
    if arg == "newkey":
        k = keystore.generate_key(q.from_user.id)
        await q.message.reply_text(
            f"🔑 <b>Одноразовый ключ:</b>\n<code>{k}</code>\n\n"
            "Передайте сотруднику — он вводит его в боте, ключ срабатывает один раз.",
            parse_mode="HTML")
    elif arg == "keys":
        await q.message.reply_text(_fmt_keys(), parse_mode="HTML")
    elif arg == "users":
        await q.message.reply_text(_fmt_users(), parse_mode="HTML")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not _is_authed(uid):
        return
    try:
        kind, _, arg = q.data.partition(":")
        if kind == "adm":
            if _is_admin(uid):
                await _admin_action(q, arg)
            return
        if kind == "pf":          # выбор мерча → портфолио (7д по умолчанию)
            i = int(arg)
            name = C("endp_list")[i]
            await q.message.reply_text(card_portfolio(name, 7), parse_mode="HTML",
                                       reply_markup=_pf_kb(i, 7))
            return
        if kind == "pfp":         # переключение периода портфолио
            si, _, sd = arg.partition(":")
            i, days = int(si), int(sd)
            name = C("endp_list")[i]
            try:
                await q.edit_message_text(card_portfolio(name, days), parse_mode="HTML",
                                          reply_markup=_pf_kb(i, days))
            except Exception:
                await q.message.reply_text(card_portfolio(name, days), parse_mode="HTML",
                                           reply_markup=_pf_kb(i, days))
            return
        i = int(arg)
        if kind == "m":
            text = card_merchant(C("merch_list")[i])
        elif kind == "meth":
            text = card_method(C("method_list")[i])
        elif kind == "g":
            text = card_gate(C("gate_list")[i])
        elif kind == "e":
            text = card_endpoints(C("endp_list")[i])
        else:
            return
        await q.message.reply_text(text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"cb error: {e}")


async def cmd_newkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _is_admin(uid):
        return
    k = keystore.generate_key(uid)
    await update.message.reply_text(
        f"🔑 <b>Одноразовый ключ:</b>\n<code>{k}</code>\n\nПередайте сотруднику (срабатывает один раз).",
        parse_mode="HTML")


async def cmd_keys(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_admin(update.effective_user.id):
        await update.message.reply_text(_fmt_keys(), parse_mode="HTML")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_admin(update.effective_user.id):
        await update.message.reply_text(_fmt_users(), parse_mode="HTML")


async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Использование: /revoke <ключ или user_id>")
        return
    t = context.args[0]
    if keystore.revoke_key(t):
        await update.message.reply_text(f"🗑 Ключ <code>{escape(t)}</code> удалён.", parse_mode="HTML")
    elif t.isdigit() and keystore.revoke_user(int(t)):
        await update.message.reply_text(f"🗑 Доступ пользователя <code>{escape(t)}</code> отозван.", parse_mode="HTML")
    else:
        await update.message.reply_text("Не найдено (укажите существующий ключ или user_id).")


def main():
    logger.info("Acme RateBot запускается...")
    logger.info("Реплика подключена" if rates_db.test_connection() else "Реплика недоступна")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newkey", cmd_newkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
