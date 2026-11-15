"""Acme Сверка — бот лукапа заявок для саппорта.
Отправь любой ID (external_id / merchant_order / invoice id / uuid) → карточка заявки:
статус, суммы, мерчант, эндпоинт, метод, гейт/терминал, реквизит, ошибка, таймлайн попыток.
Доступ — одноразовые ключи (как в CRM-боте)."""
import logging
import re
from html import escape

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes,
)

import secrets
from config import BOT_TOKEN, ACCESS_KEY, ADMIN_IDS
import db
import keys as keystore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ops_sverka")

BTN_ADMIN = "🔐 Админ"
BTN_DISPUTE = "🧾 Сверка чека"


def _menu(uid):
    rows = [[KeyboardButton(BTN_DISPUTE)]]
    if uid in ADMIN_IDS:
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _card_kb(row):
    """Inline-кнопки под карточкой заявки — дают саппорту данные одним кликом.
    Текст ответа саппорт пишет сам. Для выплат (out) реквизит/клиент не показываем."""
    if row.get("_kind") == "out":
        return None
    rid = row.get("id")
    if not rid:
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Проверить реквизит", callback_data=f"chk:req:{rid}"),
        InlineKeyboardButton("👤 Заявки клиента", callback_data=f"chk:cust:{rid}"),
    ]])


def _is_authed(uid):
    return keystore.is_authorized(uid, ADMIN_IDS)


def _is_admin(uid):
    return uid in ADMIN_IDS


# ───────────── форматирование карточки ─────────────
def _money(v, cur="RUB"):
    if v is None:
        return "—"
    try:
        n = f"{int(v):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(v)
    if not cur or cur == "RUB":
        return n + "₽"
    return f"{n} {cur}"


def _dt(v):
    return v.strftime("%d.%m.%Y %H:%M") if v else "—"


def _hm(v):
    return v.strftime("%H:%M:%S") if v else "—"


def _status_icon(status):
    s = (status or "").lower()
    if any(k in s for k in ("complete", "success", "paid", "оплач")):
        return "✅"
    if any(k in s for k in ("decline", "fail", "error", "reject", "отмен", "cancel")):
        return "❌"
    if any(k in s for k in ("expire", "истёк", "истек", "timeout")):
        return "⌛"
    if any(k in s for k in ("refund", "возврат")):
        return "↩️"
    return "⏳"


def _call_icon(status):
    s = (status or "").lower()
    if "success" in s or "complete" in s:
        return "✅"
    if any(k in s for k in ("error", "fail", "decline", "reject")):
        return "❌"
    return "•"


def card(row, calls=None):
    is_out = row.get("_kind") == "out"
    cur = row.get("currency") or "RUB"
    si = _status_icon(row.get("status"))
    lines = []
    title = row.get("external_id") or row.get("merchant_order") or row.get("id")
    head = "📤 <b>Выплата</b>" if is_out else "📥 <b>Заявка</b>"
    lines.append(f"{head}  <code>{escape(str(title))}</code>")
    if row.get("is_test"):
        lines.append("⚠️ <i>тестовая</i>")
    lines.append("")
    lines.append(f"🕐 Создана: <b>{_dt(row.get('created_msk'))}</b> МСК")
    upd = row.get("status_upd_msk")
    upd_label = "завершена" if is_out else "обновлён"
    upd_s = f"  ·  {upd_label} {_dt(upd)}" if upd else ""
    lines.append(f"{si} Статус: <b>{escape(str(row.get('status') or '—'))}</b>{upd_s}")
    if is_out:
        lines.append(f"💸 Запрошено: <b>{_money(row.get('price'), cur)}</b>  ·  выплачено: <b>{_money(row.get('amount_paid'), cur)}</b>")
    else:
        lines.append(f"💰 Сумма: <b>{_money(row.get('price'), cur)}</b>  ·  оплачено: <b>{_money(row.get('amount_paid'), cur)}</b>")
    lines.append("")
    lines.append(f"🏪 Мерчант: <b>{escape(str(row.get('merchant') or '—'))}</b>")
    lines.append(f"🧩 Эндпоинт: <code>{escape(str(row.get('product') or '—'))}</code>")
    lines.append(f"💳 Метод: <b>{escape(str(row.get('method') or '—'))}</b>")
    gate = row.get("gate") or "—"
    term = row.get("terminal") or "—"
    lines.append(f"🏦 Гейт / терминал: <b>{escape(str(gate))}</b> / {escape(str(term))}")

    recipient = row.get("recipient")
    requisite = row.get("requisite")
    bank = row.get("bank")
    deeplink = row.get("deeplink")
    if recipient or requisite or bank or deeplink:
        lines.append("")
        if recipient:
            lines.append(f"👤 Получатель: {escape(str(recipient))}")
        if bank:
            lines.append(f"🏛 Банк: {escape(str(bank))}")
        if requisite:
            lines.append(f"🔢 Реквизит: <code>{escape(str(requisite))}</code>")
        if deeplink:
            dl = str(deeplink)
            short = dl if len(dl) <= 80 else dl[:77] + "…"
            lines.append(f'🔗 Ссылка: <a href="{escape(dl)}">{escape(short)}</a>')

    err_code = row.get("err_code")
    err_text = _err_text(row)
    if err_code or err_text:
        lines.append("")
        ec = f"<code>{escape(str(err_code))}</code>" if err_code else ""
        em = escape(err_text[:300]) if err_text else ""
        sep = " — " if (ec and em) else ""
        lines.append(f"❗ Ошибка: {ec}{sep}{em}")

    lines.append("")
    lines.append("🧾 <b>ID</b>:")
    lines.append(f"   external: <code>{escape(str(row.get('external_id') or '—'))}</code>")
    lines.append(f"   merchant_order: <code>{escape(str(row.get('merchant_order') or '—'))}</code>")
    lines.append(f"   invoice id: <code>{escape(str(row.get('id') or '—'))}</code>")
    lines.append(f"   uuid: <code>{escape(str(row.get('uuid') or '—'))}</code>")

    if not is_out:
        if calls:
            lines.append("")
            lines.append(f"📞 <b>Попытки к гейту</b> ({len(calls)}):")
            for c in calls[:20]:
                ic = _call_icon(c.get("status"))
                dur = f" {float(c['duration']):.1f}с" if c.get("duration") is not None else ""
                code = f" <code>{escape(str(c['error_code']))}</code>" if c.get("error_code") else ""
                emsg = f" — {escape(str(c['error_message'])[:80])}" if c.get("error_message") else ""
                g = escape(str(c.get("gate") or c.get("terminal") or ""))
                gtxt = f" [{g}]" if g else ""
                lines.append(f"   {ic} {_hm(c.get('created_msk'))} {escape(str(c.get('status') or '—'))}{dur}{code}{gtxt}{emsg}")
        elif calls is not None:
            lines.append("")
            lines.append("📞 Попыток к гейту не зафиксировано.")

    return "\n".join(lines)


def _brief_btn_label(row):
    si = _status_icon(row.get("status"))
    kind_ic = "📤" if row.get("_kind") == "out" else "📥"
    t = row.get("created_msk")
    tm = t.strftime("%d.%m %H:%M") if t else ""
    return f"{kind_ic}{si} {tm} · {str(row.get('merchant') or '—')[:13]} · {_money(row.get('price'), row.get('currency') or 'RUB')}"


# Текст ошибки для показа: приоритет — ошибка ПРОВАЙДЕРА (last_payment_error_orig),
# а не наша внутренняя/временная (last_payment_error):
# в сверке показывать «Client has gone / 3-D Secure not passed», а не «Something went wrong».
def _err_text(row):
    orig = (row.get("err_orig") or "").strip()
    if orig:
        return orig
    return (row.get("err_msg") or "").strip()


# Причина исхода заявки/выплаты — человеческим языком (для саппорта и ответа мерчу).
# Возвращает (ключ-категории, подпись). Порядок проверок важен.
def _reason(row):
    if row.get("_kind") == "out":
        st = (row.get("status") or "").lower()
        if "done" in st or "complete" in st:
            return ("paid", "выплата проведена")
        if "reject" in st or "fail" in st:
            return ("rejected", "выплата отклонена")
        return ("inprogress", "выплата в обработке")
    st = (row.get("status") or "").lower()
    ec = (row.get("err_code") or "").upper()
    em = ((row.get("err_msg") or "") + " " + (row.get("err_orig") or "")).lower()
    if "complete" in st or "partial" in st or "оплач" in st:
        return ("paid", "оплачено")
    if st in ("new", "pending") or "process" in st or "ожид" in st:
        return ("inprogress", "в обработке, ещё не финал")
    if "acquir" in em or "эквайер" in em or "declined" in em or "отклонено банк" in em:
        return ("acquirer", "отказ эквайера (банк отклонил платёж)")
    if ec == "CANCELLED" or "cancel" in em:
        return ("cancelled", "клиент закрыл форму / отменил")
    # «did not complete the payment within the allotted time» — гейтовая формулировка:
    # реквизит клиенту показан, оплата не пришла за TTL. Это НЕ «не был выдан»
    # (кейс 11.06: SBP-заявки orion помечались «реквизит не был выдан» при реальной выдаче).
    if ec == "SESSION_EXPIRED" or "ttl" in em or "expire" in em or "истёк" in em \
            or "allotted time" in em:
        return ("ttl", "истекло время — клиент не оплатил вовремя")
    if ec == "ROUTING_ERROR":
        return ("routing", "не прошёл маршрутизацию / антифрод")
    # выдача = любой из маркеров: deeplink / реквизит из invoicerequisite /
    # успешный роутинг / был запрос к гейту (у части SBP-флоу реквизит живёт на стороне гейта)
    if row.get("deeplink") or row.get("requisite") or row.get("route_ok") or row.get("has_gatecall"):
        return ("issued_unpaid", "реквизит выдан, но клиент не оплатил")
    return ("not_issued", "реквизит не был выдан")


# Порядок и подписи групп в ответе для мерча
_REASON_ORDER = [
    ("paid", "✅ Оплачено"),
    ("issued_unpaid", "🟡 Реквизит выдан, клиент не оплатил"),
    ("ttl", "⌛ Истекло время, клиент не оплатил"),
    ("cancelled", "✋ Клиент закрыл форму / отменил"),
    ("acquirer", "🏛 Отказ эквайера (банк отклонил)"),
    ("not_issued", "⚪ Реквизит не был выдан"),
    ("routing", "🚧 Не прошёл проверку / маршрутизацию"),
    ("inprogress", "⏳ В обработке"),
    ("rejected", "❌ Выплата отклонена"),
]


def _compact(row):
    """Сжатая карточка для пакетной сверки (3+ ордеров)."""
    si = _status_icon(row.get("status"))
    kind = "📤" if row.get("_kind") == "out" else "📥"
    cur = row.get("currency") or "RUB"
    title = row.get("external_id") or row.get("merchant_order") or row.get("id")
    t = row.get("created_msk")
    tm = t.strftime("%d.%m %H:%M") if t else "—"
    parts = [
        f"{kind}{si} <code>{escape(str(title))}</code> · <b>{escape(str(row.get('status') or '—'))}</b>",
        f"   💰 {_money(row.get('amount_paid'), cur)} / {_money(row.get('price'), cur)} · 🏪 {escape(str(row.get('merchant') or '—'))}",
        f"   💳 {escape(str(row.get('method') or '—'))} · 🏦 {escape(str(row.get('gate') or '—'))} · 🕐 {tm}",
    ]
    mo = row.get("merchant_order")
    if mo and str(mo) != str(title):
        parts.append(f"   🧾 merchant_order: <code>{escape(str(mo))}</code>")
    parts.append(f"   ↳ причина: <b>{escape(_reason(row)[1])}</b>")
    et = _err_text(row)
    if row.get("err_code") or et:
        ec = f"<code>{escape(str(row.get('err_code')))}</code>" if row.get("err_code") else ""
        em = escape(et[:90]) if et else ""
        sep = " — " if (ec and em) else ""
        parts.append(f"   ❗ {ec}{sep}{em}")
    return "\n".join(parts)


def _merchant_answer(results):
    """Плоский текст для пересылки мерчу: группировка по причине, БЕЗ провайдерских данных
    (без гейта/терминала/external_id) — только merchant_order/uuid, статус, сумма, причина."""
    groups = {}
    not_found = []
    for tok, matched in results:
        if not matched:
            not_found.append(tok)
            continue
        row = matched[0]
        key = _reason(row)[0]
        ident = row.get("merchant_order") or row.get("uuid") or row.get("id")
        cur = row.get("currency") or "RUB"
        ap = row.get("amount_paid") or 0
        pr = row.get("price") or 0
        amt = f" — {_money(ap if key == 'paid' else pr, cur)}"
        groups.setdefault(key, []).append(f"{ident}{amt}")
    out = ["Разбор по заявкам:", ""]
    for key, label in _REASON_ORDER:
        items = groups.get(key)
        if not items:
            continue
        out.append(f"{label} ({len(items)}):")
        out.extend(items)
        out.append("")
    if not_found:
        out.append(f"Не найдены ({len(not_found)}):")
        out.append(", ".join(not_found))
        out.append("")
    return "\n".join(out).strip()


# ── разбор списка ID (пакетная сверка) ──
_SPLIT_RE = re.compile(r"[\s,;]+")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{4,}$")  # ≥5 символов, похоже на идентификатор
_MAX_BATCH = 30


def _parse_tokens(txt):
    return [p for p in _SPLIT_RE.split((txt or "").strip()) if p]


def _looks_like_id(tok):
    return bool(_ID_RE.match(tok))


def _match_row(row, q):
    """Сопоставляет строку с конкретным запросом из пакета (повторяет логику SQL по ID)."""
    ql = (q or "").strip().lower()
    qnd = ql.replace("-", "")
    ext, mo = row.get("external_id"), row.get("merchant_order")
    rid, ru = row.get("id"), row.get("uuid")
    if ext and str(ext).lower() == ql:
        return True
    if mo and str(mo).lower() == ql:
        return True
    if mo and str(mo).lower().replace("-", "") == qnd:
        return True
    if rid and str(rid).lower().replace("-", "") == qnd:
        return True
    if ru and str(ru).lower().replace("-", "") == qnd:
        return True
    return False


async def _send_blocks(update, blocks, reply_markup=None):
    """Склеивает блоки и шлёт частями с учётом лимита Telegram (4096)."""
    chunks, buf = [], ""
    for ln in blocks:
        piece = ("\n" if buf else "") + ln
        if buf and len(buf) + len(piece) > 3800:
            chunks.append(buf)
            buf = ln
        else:
            buf += piece
    if buf:
        chunks.append(buf)
    for i, ch in enumerate(chunks):
        rm = reply_markup if i == len(chunks) - 1 else None
        await update.message.reply_text(ch, parse_mode="HTML", reply_markup=rm)


async def _handle_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, tokens):
    uid = update.effective_user.id
    if len(tokens) > _MAX_BATCH:
        await update.message.reply_text(f"⚠️ Слишком много ID ({len(tokens)}). Беру первые {_MAX_BATCH}.")
        tokens = tokens[:_MAX_BATCH]
    try:
        all_rows = db.search_many(tokens)
    except Exception as e:
        logger.error(f"batch lookup error: {e}")
        await update.message.reply_text(f"❌ Ошибка БД: {escape(str(e)[:160])}", reply_markup=_menu(uid))
        return

    results = [(t, [r for r in all_rows if _match_row(r, t)]) for t in tokens]

    # до 2 ID — подробные карточки как при обычном поиске
    if len(tokens) <= 2:
        for tok, matched in results:
            if not matched:
                await update.message.reply_text(
                    f"❓ <code>{escape(tok[:60])}</code> — не найдено.",
                    parse_mode="HTML", reply_markup=_menu(uid))
                continue
            for row in matched[:2]:
                calls = None
                if row.get("_kind") != "out":
                    try:
                        calls = db.get_gate_calls(row["id"])
                    except Exception:
                        calls = None
                await update.message.reply_text(card(row, calls), parse_mode="HTML", reply_markup=_menu(uid))
        return

    # 3+ ID — компактная сводка
    found = sum(1 for _, m in results if m)
    blocks = [f"🔎 <b>Пакетная сверка</b> · запросов: <b>{len(tokens)}</b> · найдено: <b>{found}</b>", ""]
    for tok, matched in results:
        if not matched:
            blocks.append(f"❓ <code>{escape(tok[:60])}</code> — не найдено")
            blocks.append("")
            continue
        for r in matched[:3]:
            blocks.append(_compact(r))
        if len(matched) > 3:
            blocks.append(f"   …ещё {len(matched) - 3} совпадений по этому ID")
        blocks.append("")
    blocks.append("💡 Пришлите один ID — покажу полную карточку с попытками к гейту.")
    context.user_data["batch_results"] = results
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📋 Ответ для мерча", callback_data="merchans")]])
    await _send_blocks(update, blocks, kb)


# ───────────── хендлеры ─────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not _is_authed(uid):
        context.user_data["awaiting_key"] = True
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n🔎  <b>ACME СВЕРКА</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\nДля доступа введите ключ:", parse_mode="HTML")
        return
    await update.message.reply_text(
        "🔎 <b>Сверка Acme</b> — заявки и выплаты\n\n"
        "Отправьте любой идентификатор:\n"
        "   • external_id\n   • merchant_order\n   • id / uuid\n"
        "   • или реквизит / телефон получателя\n\n"
        "Дефисы и регистр не важны. Ищу и по пай-инам (📥), и по выплатам (📤). "
        "Верну карточку: статус, суммы, метод, гейт/терминал, реквизит, ошибку, "
        "а для заявок — таймлайн попыток.\n\n"
        "📋 <b>Пакетная сверка:</b> пришлите сразу несколько ID "
        "(по строкам, через запятую или пробел). До 2 — подробные карточки, "
        "от 3 — компактная сводка по каждому.",
        parse_mode="HTML", reply_markup=_menu(uid))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = (update.message.text or "").strip()

    # авторизация по ключу
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

    if txt == BTN_ADMIN and _is_admin(uid):
        await _show_admin(update)
        return

    if txt == BTN_DISPUTE:
        await _dispute_prompt(update.message, context)
        return

    # режим сверки чека: ждём "ID реквизит [сумма]"
    if context.user_data.get("mode") == "dispute":
        context.user_data["mode"] = None
        parts = _parse_tokens(txt)
        if len(parts) < 2:
            await update.message.reply_text(
                "Нужно минимум: ID заявки и реквизит из чека. Нажмите «🧾 Сверка чека» и пришлите снова.",
                reply_markup=_menu(uid))
            return
        res = _dispute_result(parts[0], parts[1], parts[2] if len(parts) > 2 else None)
        await update.message.reply_text(res, parse_mode="HTML", reply_markup=_DISPUTE_KB)
        return

    # пакетная сверка: 2+ токена, каждый похож на ID → лукап списком
    tokens = _parse_tokens(txt)
    if len(tokens) >= 2 and all(_looks_like_id(t) for t in tokens):
        await _handle_batch(update, context, tokens)
        return

    # поиск заявки/выплаты
    try:
        rows = db.search(txt, limit=10)
    except Exception as e:
        logger.error(f"lookup error: {e}")
        await update.message.reply_text(f"❌ Ошибка БД: {escape(str(e)[:160])}", reply_markup=_menu(uid))
        return

    if not rows:
        await update.message.reply_text(
            f"❌ Не найдено по «<code>{escape(txt[:80])}</code>».\n"
            "Проверьте ID (external_id / merchant_order / id / uuid) или реквизит/телефон.",
            parse_mode="HTML", reply_markup=_menu(uid))
        return

    if len(rows) == 1:
        row = rows[0]
        calls = None
        if row.get("_kind") != "out":
            try:
                calls = db.get_gate_calls(row["id"])
            except Exception:
                calls = None
        await update.message.reply_text(card(row, calls), parse_mode="HTML",
                                        reply_markup=_card_kb(row) or _menu(uid))
        return

    # несколько совпадений → список с кнопками
    kb = [[InlineKeyboardButton(_brief_btn_label(r), callback_data=f"sv:{r.get('_kind','in')}:{r['id']}")] for r in rows]
    await update.message.reply_text(
        f"🔎 Найдено <b>{len(rows)}</b> — выберите (📥 заявка / 📤 выплата):",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not _is_authed(uid):
        return
    kind, _, arg = q.data.partition(":")
    try:
        if kind == "adm":
            if _is_admin(uid):
                await _admin_action(q, arg)
            return
        if kind == "sv":
            sub, _, item_id = arg.partition(":")
            row = db.get_card(sub, item_id)
            if not row:
                await q.message.reply_text("❌ Не найдено (возможно, удалено).")
                return
            calls = db.get_gate_calls(item_id) if sub != "out" else None
            await q.message.reply_text(card(row, calls), parse_mode="HTML",
                                       reply_markup=_card_kb(row))
        if kind == "chk":
            sub, _, item_id = arg.partition(":")
            if sub == "req":
                st = db.requisite_stats(item_id, days=3)
                if not st or not st.get("req"):
                    await q.message.reply_text("По заявке не нашёл реквизит.")
                    return
                total = int(st["total"] or 0); succ = int(st["succ"] or 0)
                conv = round(succ * 100.0 / total) if total else 0
                last = st.get("last_at")
                last_s = last.strftime("%d.%m %H:%M") if last else "—"
                verdict = "🟢 рабочий" if conv >= 30 else ("🟡 слабый" if conv > 0 else "🔴 не платит")
                await q.message.reply_text(
                    f"🔄 <b>Реквизит</b> <code>{escape(str(st['req']))}</code> за 3 дня:\n"
                    f"Заявок: {total} · оплат: {succ} ({conv}%) · отказов: {st.get('failed')}\n"
                    f"Клиентов: {st.get('clients')} · последняя: {last_s}\n"
                    f"Вердикт: <b>{verdict}</b>", parse_mode="HTML")
                return
            if sub == "cust":
                orders = db.customer_orders(item_id, hours=24)
                if not orders:
                    await q.message.reply_text("За сутки у клиента других заявок нет.")
                    return
                lines = [f"👤 <b>Заявки клиента за сутки</b> ({len(orders)}):", ""]
                for o in orders[:15]:
                    t = o.get("created_msk")
                    ts = t.strftime("%d.%m %H:%M") if t else "—"
                    stt = o.get("status") or "—"
                    ic = "✅" if stt in ("complete","partial") else ("⏳" if stt in ("new","pending") else "❌")
                    lines.append(f"{ic} {ts} · {_money(o.get('price'),'RUB')} · {escape(str(stt))} · "
                                 f"<code>{escape(str(o.get('merchant_order') or '—'))}</code>")
                await q.message.reply_text("\n".join(lines), parse_mode="HTML")
                return
        if kind == "merchans":
            results = context.user_data.get("batch_results")
            if not results:
                await q.message.reply_text("Нет данных пакетной сверки — пришлите список ID заново.")
                return
            txt = _merchant_answer(results)
            await q.message.reply_text(
                "Готовый текст для мерча (без внутренних данных) — скопируйте и отправьте:\n\n"
                f"<code>{escape(txt)}</code>", parse_mode="HTML")
        if kind == "disp":
            context.user_data["mode"] = "dispute"
            await _dispute_prompt(q.message)
    except Exception as e:
        logger.error(f"cb error: {e}")
        await q.message.reply_text(f"❌ Ошибка: {escape(str(e)[:160])}")


# ───────────── админка: ключи доступа ─────────────
async def _show_admin(update: Update):
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


def _digits(s):
    return re.sub(r"\D", "", str(s or ""))


def _dispute_result(oid, cheque_req, cheque_amt=None):
    """Текст сверки чека с заявкой (общий для команды и кнопки)."""
    try:
        rows = db.search(oid, limit=5)
    except Exception as e:
        return f"❌ Ошибка БД: {escape(str(e)[:160])}"
    inv = next((r for r in rows if r.get("_kind") != "out"), None)
    if not inv:
        return f"❓ Заявка <code>{escape(str(oid)[:60])}</code> не найдена."
    issued = inv.get("requisite")
    cd, idg = _digits(cheque_req), _digits(issued)
    if not idg:
        req_verdict = "⚠️ по заявке реквизит не зафиксирован — сверять не с чем"
    elif cd == idg:
        req_verdict = "✅ реквизит совпадает"
    elif cd and idg and (cd[-4:] == idg[-4:]):
        req_verdict = "🟡 совпадают последние 4 цифры, но не полностью — проверьте вручную"
    else:
        req_verdict = "❌ реквизит НЕ совпадает с выданным"
    lines = [
        f"🧾 <b>Сверка чека</b> · <code>{escape(str(oid)[:40])}</code>",
        f"Статус заявки: <b>{escape(str(inv.get('status') or '—'))}</b>",
        "",
        f"Выдан реквизит: <code>{escape(str(issued or '—'))}</code>",
        f"Реквизит из чека: <code>{escape(str(cheque_req))}</code>",
        f"→ {req_verdict}",
    ]
    if inv.get("recipient"):
        lines.append(f"Получатель по заявке: {escape(str(inv.get('recipient')))}")
    price = inv.get("price"); paid = inv.get("amount_paid")
    lines.append("")
    lines.append(f"Сумма заявки: <b>{_money(price)}</b> · оплачено: <b>{_money(paid)}</b>")
    if cheque_amt:
        ca = _digits(cheque_amt)
        amt_ok = ca and _digits(price) and abs(int(ca) - int(_digits(price))) <= 1
        lines.append(f"Сумма из чека: <b>{escape(str(cheque_amt))}</b> → "
                     f"{'✅ совпадает' if amt_ok else '❌ расходится с суммой заявки'}")
    return "\n".join(lines)


_DISPUTE_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Ещё сверка чека", callback_data="disp:new")]])


async def cmd_dispute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сверка чека: /dispute <ID> <реквизит_из_чека> [сумма] — или кнопка «🧾 Сверка чека»."""
    uid = update.effective_user.id
    if not _is_authed(uid):
        context.user_data["awaiting_key"] = True
        await update.message.reply_text("🔐 Введите ключ доступа:")
        return
    args = context.args or []
    if len(args) < 2:
        await _dispute_prompt(update.message, context)
        return
    txt = _dispute_result(args[0], args[1], args[2] if len(args) > 2 else None)
    await update.message.reply_text(txt, parse_mode="HTML", reply_markup=_DISPUTE_KB)


async def _dispute_prompt(message, context=None):
    if context is not None:
        context.user_data["mode"] = "dispute"
    await message.reply_text(
        "🧾 <b>Сверка чека</b>\n\nПришлите одной строкой: <b>ID заявки</b>, <b>реквизит из чека</b> и "
        "(по желанию) <b>сумму</b> — через пробел.\n\n"
        "Например: <code>10000000000001 +70000000000 5000</code>",
        parse_mode="HTML")


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
    logger.info("Acme Сверка запускается...")
    logger.info("Реплика подключена" if db.test_connection() else "Реплика недоступна")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("dispute", cmd_dispute))
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
