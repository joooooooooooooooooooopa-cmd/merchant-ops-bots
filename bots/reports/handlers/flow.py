"""
Flow выгрузки: тип -> сущность (мерч/гейт/all) -> аудитория -> период -> файл.

Состояние держим в context.user_data (PTB 21 in-memory).
Ключи:
  scope:       'merchant' | 'gate' | 'all'
  scope_key:   iname (или None для all)
  scope_label: что показать в шапке
  audience:    'merchant' | 'provider' | 'internal'
  d_from, d_to: даты
  mlist / glist: кэш для пикеров и поиска
  last_request: dict для «повторить»
"""
import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

import builder
import db
import keys
from config import ACTIVE_DAYS, PAGE_SIZE

from . import keyboards as kb
from .utils import (MODE_DATE_CUSTOM, MODE_GATE_SEARCH, MODE_MERCHANT_SEARCH,
                    fmt_period, parse_date_input, today_msk, yesterday_msk)

log = logging.getLogger(__name__)


def _reset_state(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("scope", "scope_key", "scope_label", "audience", "d_from", "d_to",
              "mlist", "glist", "mode"):
        ctx.user_data.pop(k, None)


def _scope_label(scope: str, key: Optional[str], name: Optional[str] = None) -> str:
    if scope == "merchant":
        return f"Мерчант: {name or key}"
    if scope == "gate":
        return f"Гейт: {key}"
    return "Все мерчанты (сводно)"


async def start_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    _reset_state(ctx)
    msg = update.effective_message
    await msg.reply_text(
        "<b>Новая выгрузка</b>\n\nЧто выгружаем?",
        parse_mode="HTML",
        reply_markup=kb.kb_scope(),
    )


async def show_last_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    last = ctx.user_data.get("last_request")
    if not last:
        await msg.reply_text("Прошлой выгрузки нет — сделай одну через «Новая выгрузка».")
        return
    rows = [[InlineKeyboardButton("Запустить снова", callback_data="rerun:last")]]
    await msg.reply_text(
        "<b>Прошлая выгрузка</b>\n\n"
        f"{last['scope_label']}\n"
        f"Формат: {last['audience']}\n"
        f"Период: {fmt_period(last['d_from'], last['d_to'])}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    if not cq:
        return
    if not keys.is_allowed(cq.from_user.id):
        await cq.answer("Доступ закрыт", show_alert=True)
        return

    data = cq.data or ""
    await cq.answer()
    log.info(f"cb from {cq.from_user.id}: {data}")

    parts = data.split(":")
    head = parts[0]

    try:
        if head == "noop":
            return

        if head == "sc":
            return await _on_scope(cq, ctx, parts[1])

        if head == "back":
            if parts[1] == "scope":
                _reset_state(ctx)
                await cq.edit_message_text(
                    "<b>Новая выгрузка</b>\n\nЧто выгружаем?",
                    parse_mode="HTML",
                    reply_markup=kb.kb_scope(),
                )
                return
            if parts[1] == "picker":
                return await _show_picker(cq, ctx,
                                          ctx.user_data.get("scope") or "merchant",
                                          page=0)
            if parts[1] == "audience":
                scope = ctx.user_data.get("scope") or "merchant"
                return await cq.edit_message_text(
                    "Кому отчёт?", reply_markup=kb.kb_audience(scope))

        if head == "pg":
            kind = parts[1]
            page = int(parts[2])
            scope = "merchant" if kind == "m" else "gate"
            return await _show_picker(cq, ctx, scope, page=page)

        if head == "pk":
            kind = parts[1]
            idx = int(parts[2])
            return await _on_pick(cq, ctx, kind, idx)

        if head == "sr":
            kind = parts[1]
            ctx.user_data["mode"] = (MODE_MERCHANT_SEARCH if kind == "m"
                                    else MODE_GATE_SEARCH)
            label = "мерча" if kind == "m" else "гейта"
            await cq.edit_message_text(
                f"Введи название/часть имени {label}.\nПример: <code>alpha</code>",
                parse_mode="HTML",
            )
            return

        if head == "au":
            ctx.user_data["audience"] = parts[1]
            return await _ask_period(cq, ctx)

        if head == "pd":
            return await _on_period(cq, ctx, parts[1])

        if head == "rerun":
            return await _rerun(update, ctx)

    except Exception as e:
        log.exception(f"callback error: {e}")
        try:
            await cq.edit_message_text(f"Ошибка: {e}")
        except Exception:
            pass


async def _on_scope(cq, ctx, scope: str) -> None:
    ctx.user_data["scope"] = scope
    if scope == "all":
        ctx.user_data["scope_key"] = None
        ctx.user_data["scope_label"] = _scope_label("all", None)
        return await cq.edit_message_text(
            f"{_scope_label('all', None)}\n\nКому отчёт?",
            reply_markup=kb.kb_audience("all"),
        )
    return await _show_picker(cq, ctx, scope, page=0)


async def _show_picker(cq, ctx, scope: str, page: int) -> None:
    cache_key = "mlist" if scope == "merchant" else "glist"
    items = ctx.user_data.get(cache_key)
    if not items:
        await cq.edit_message_text("Загружаю список из реплики…")
        items = await asyncio.to_thread(
            db.list_merchants if scope == "merchant" else db.list_gates,
            ACTIVE_DAYS,
        )
        ctx.user_data[cache_key] = items

    if not items:
        return await cq.edit_message_text(
            "Пусто (за 30 дней трафика нет). Реплика отвечает?"
        )

    title = ("<b>Выбери мерчанта</b>" if scope == "merchant"
             else "<b>Выбери гейт / провайдера</b>")
    kind = "m" if scope == "merchant" else "g"
    await cq.edit_message_text(
        f"{title}\n<i>За {ACTIVE_DAYS} дн., с трафиком, отсортировано по объёму.</i>",
        parse_mode="HTML",
        reply_markup=kb.kb_picker(items, page, PAGE_SIZE, kind),
    )


async def _on_pick(cq, ctx, kind: str, idx: int) -> None:
    cache_key = "mlist" if kind == "m" else "glist"
    items = ctx.user_data.get(cache_key) or []
    if idx < 0 or idx >= len(items):
        return await cq.edit_message_text("Элемент устарел, открой пикер заново.")
    item = items[idx]
    scope = "merchant" if kind == "m" else "gate"
    ctx.user_data["scope"] = scope
    ctx.user_data["scope_key"] = item.get("iname")
    name = item.get("name") if kind == "m" else item.get("iname")
    ctx.user_data["scope_label"] = _scope_label(scope, item.get("iname"), name)
    await cq.edit_message_text(
        f"{ctx.user_data['scope_label']}\n\nКому отчёт?",
        parse_mode="HTML",
        reply_markup=kb.kb_audience(scope),
    )


async def _ask_period(cq, ctx) -> None:
    audience = ctx.user_data.get("audience")
    label = ctx.user_data.get("scope_label", "")
    pretty_a = {
        "merchant": "для мерча (без gate/terminal)",
        "provider": "для провайдера",
        "internal": "внутренняя (полная)",
    }.get(audience, audience)
    await cq.edit_message_text(
        f"{label}\nФормат: {pretty_a}\n\nКакой период?",
        parse_mode="HTML",
        reply_markup=kb.kb_period(),
    )


async def _on_period(cq, ctx, kind: str) -> None:
    if kind == "today":
        d_from = d_to = today_msk()
    elif kind == "yest":
        d_from = d_to = yesterday_msk()
    elif kind == "7":
        d_to = today_msk()
        d_from = d_to - timedelta(days=6)  # вкл. сегодня = 7 дней
    elif kind == "30":
        d_to = today_msk()
        d_from = d_to - timedelta(days=29)
    elif kind == "custom":
        ctx.user_data["mode"] = MODE_DATE_CUSTOM
        return await cq.edit_message_text(
            "Введи дату или диапазон:\n\n"
            "— <code>05.06</code> — один день текущего года\n"
            "— <code>05.06.2026</code> — один день\n"
            "— <code>01.06-07.06</code> — диапазон\n"
            "— <code>01.06.2026-07.06.2026</code> — диапазон с годами",
            parse_mode="HTML",
        )
    else:
        return

    ctx.user_data["d_from"] = d_from
    ctx.user_data["d_to"] = d_to
    try:
        await cq.edit_message_text(
            f"{ctx.user_data.get('scope_label', '')}\n"
            f"{fmt_period(d_from, d_to)}\n\nСобираю выгрузку…"
        )
    except Exception:
        pass
    await _run_build(cq.message.chat_id, ctx, ctx.bot)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Возвращает True, если хендлер обработал ввод."""
    mode = ctx.user_data.get("mode")
    if not mode:
        return False
    text = (update.effective_message.text or "").strip()
    if not text:
        return True

    if mode in (MODE_MERCHANT_SEARCH, MODE_GATE_SEARCH):
        ctx.user_data.pop("mode", None)
        scope = "merchant" if mode == MODE_MERCHANT_SEARCH else "gate"
        cache_key = "mlist" if scope == "merchant" else "glist"
        items = ctx.user_data.get(cache_key) or []
        q = text.lower()
        filtered = [
            it for it in items
            if q in (it.get("name") or "").lower()
            or q in (it.get("iname") or "").lower()
        ]
        if not filtered:
            if scope == "merchant":
                found = await asyncio.to_thread(db.resolve_merchant, text)
                if found:
                    items.append({"iname": found["iname"], "name": found["name"], "cnt": 0})
                    ctx.user_data[cache_key] = items
                    filtered = [items[-1]]
            else:
                found = await asyncio.to_thread(db.resolve_gate, text)
                if found:
                    items.append({"iname": found["iname"], "cnt": 0})
                    ctx.user_data[cache_key] = items
                    filtered = [items[-1]]
        if not filtered:
            await update.effective_message.reply_text(
                f"По «{text}» ничего не нашёл. Попробуй другой запрос или нажми «Новая выгрузка»."
            )
            return True
        if scope == "merchant":
            ctx.user_data["mlist"] = filtered
        else:
            ctx.user_data["glist"] = filtered
        await update.effective_message.reply_text(
            f"Найдено: {len(filtered)}",
            reply_markup=kb.kb_picker(
                filtered, 0, PAGE_SIZE,
                "m" if scope == "merchant" else "g",
            ),
        )
        return True

    if mode == MODE_DATE_CUSTOM:
        parsed = parse_date_input(text)
        if not parsed:
            await update.effective_message.reply_text(
                "Не понял дату. Пример: <code>05.06</code> или <code>01.06-07.06</code>",
                parse_mode="HTML",
            )
            return True
        ctx.user_data.pop("mode", None)
        d_from, d_to = parsed
        ctx.user_data["d_from"] = d_from
        ctx.user_data["d_to"] = d_to
        await _run_build(update.effective_chat.id, ctx, ctx.bot)
        return True

    return False


async def _run_build(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE, bot) -> None:
    scope = ctx.user_data.get("scope")
    key = ctx.user_data.get("scope_key")
    audience = ctx.user_data.get("audience")
    d_from = ctx.user_data.get("d_from")
    d_to = ctx.user_data.get("d_to")

    if not (scope and audience and d_from and d_to):
        await bot.send_message(chat_id, "Неполные параметры — начни заново.")
        return

    await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    status = await bot.send_message(
        chat_id,
        f"<b>Собираю отчёт…</b>\n"
        f"{ctx.user_data.get('scope_label', '')}\n"
        f"{fmt_period(d_from, d_to)}",
        parse_mode="HTML",
    )

    try:
        # Предварительный размер: если транзакций > MAX — попросим сузить.
        # Иначе xlsx > 50 MB не пролезет в Telegram + ВПС задохнётся по памяти.
        from db import _q, date_params, date_range_filter, NOTEST_FULL  # noqa
        wh = [date_range_filter(), NOTEST_FULL]
        params = date_params(d_from, d_to)
        if scope == "merchant":
            wh.append("i.merchant_id IN (SELECT id FROM pricing_merchant WHERE iname = %(key)s)")
            params["key"] = key
        elif scope == "gate":
            wh.append(
                "i.primary_terminal_id IN ("
                " SELECT id FROM pricing_terminal WHERE payment_gate_id IN ("
                "  SELECT id FROM pricing_paymentgate WHERE iname = %(key)s"
                " ))"
            )
            params["key"] = key
        where_sql = " AND ".join(wh)
        count_sql = f"SELECT COUNT(*) AS n FROM pricing_invoice i WHERE {where_sql}"
        n_rows = (await asyncio.to_thread(_q, count_sql, params))[0]["n"]

        MAX_ROWS = 200_000
        if n_rows > MAX_ROWS:
            n_fmt = f"{n_rows:,}".replace(",", " ")
            cap_fmt = f"{MAX_ROWS:,}".replace(",", " ")
            await status.edit_text(
                f"За период <b>{fmt_period(d_from, d_to)}</b> найдено "
                f"<b>{n_fmt}</b> заявок — слишком много для одного файла "
                f"(лимит {cap_fmt}).\n\nСузь период и попробуй снова.",
                parse_mode="HTML",
            )
            return

        out_path: Path = await asyncio.to_thread(
            builder.build, scope, key, audience, d_from, d_to,
        )
    except Exception as e:
        log.exception("build failed")
        await status.edit_text(f"Не получилось собрать: {e}")
        return

    size_kb = out_path.stat().st_size / 1024
    size_fmt = f"{int(size_kb):,}".replace(",", " ")
    caption = (
        f"{ctx.user_data.get('scope_label', '')}\n"
        f"{fmt_period(d_from, d_to)}\n"
        f"Формат: {audience}\n"
        f"{out_path.name} ({size_fmt} KB)"
    )

    with out_path.open("rb") as f:
        await bot.send_document(chat_id, document=f, filename=out_path.name, caption=caption)
    try:
        await status.delete()
    except Exception:
        pass

    ctx.user_data["last_request"] = {
        "scope": scope, "scope_key": key, "audience": audience,
        "d_from": d_from, "d_to": d_to,
        "scope_label": ctx.user_data.get("scope_label", ""),
    }


async def _rerun(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    last = ctx.user_data.get("last_request")
    if not last:
        await update.effective_message.reply_text("Прошлой выгрузки нет.")
        return
    for k, v in last.items():
        ctx.user_data[k] = v
    await _run_build(update.effective_chat.id, ctx, ctx.bot)
