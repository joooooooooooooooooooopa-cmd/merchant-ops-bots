"""Acme AM-ассистент — приватный бот (только владелец).
Кнопочный интерфейс + grounded-аналитика + готовые сообщения. Проактивный мониторинг 24/7."""
import asyncio
import base64
import io
import logging
import os
import re
from datetime import time as dtime, datetime, timezone, timedelta
from html import escape

from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes,
)

from config import (BOT_TOKEN, OWNER_ID, OPENAI_API_KEY, PRIORITY,
                    QUIET_START, QUIET_END, INTEG_NUDGE_DAYS)
import data
import brain
import monitor
import store
import views
import guard
import chatdump
import tester

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("ops_assistant")

# ── reply-меню ─────────────────────────────────────────────────
BTN_BRIEF = "🌅 Сводка"
BTN_PRIORITY = "⭐ Приоритетные"
BTN_MERCHANTS = "🏪 Мерчанты"
BTN_INTEG = "🧩 Интеграция"
BTN_ORDER = "🧾 Сверка по ID"
BTN_API = "🛠 API-доки"
BTN_NOTES = "🗒 Заметки"
BTN_HELP = "❓ Помощь"
BTN_OUTBOX = "📨 На сегодня"
BTN_BDM = "📋 Для БДМ"
BTN_CHECK = "✅ Чек-лист"
BTN_STATUS = "🛡 Статус системы"
BTN_CHATDUMP = "📂 Разбор чата"
BTN_PROVIDERS = "🏦 Провайдеры"
BTN_ANTIFRAUD = "🛡 Антифрод"
BTN_TEST = "🧪 Тест метода"

MSK = timezone(timedelta(hours=3))


def _is_quiet():
    h = datetime.now(MSK).hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END


BTN_ANALYTICS = "📊 Аналитика"


def _menu():
    # Два блока: Аналитик (свободные вопросы + сверка) и Тех (API/интеграция) + сервис.
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_ANALYTICS)],
         [KeyboardButton(BTN_ORDER), KeyboardButton(BTN_API)],
         [KeyboardButton(BTN_STATUS), KeyboardButton(BTN_HELP)]],
        resize_keyboard=True)


def _owner(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == OWNER_ID


HELP = (
    "🤖 <b>Твой аналитик Acme</b>\n\n"
    "Просто пиши вопрос по данным — я сам лезу в базу, считаю и отвечаю с цифрами.\n\n"
    "📊 <b>Аналитик</b> (любой вопрос по БД):\n"
    "• «конверсия Alpha за сегодня» / «кто лучше — Bravo, Alpha или Delta за неделю»\n"
    "• «разбери Alpha трансгран по проектам, топ худших finish url»\n"
    "• «почему UZ BankA плохо конвертит» / «динамика Bravo по часам»\n"
    "• «дохлые реки Bravo» / «зависшие выплаты Alpha»\n"
    "• «выгрузи в эксель заявки Charlie havale за сегодня» — пришлю файлом\n"
    "• «дай текст коллегам / мерчу …» — напишу готовое сообщение твоим стилем на пересылку\n"
    "🧾 <b>Сверка по ID</b> — статус заявки/выплаты по external_id / merchant_order / uuid.\n\n"
    "🛠 <b>Тех / Интеграция</b>: вопросы по API (/init, /status, выплаты, рефанды, P2P, реестры).\n\n"
    "<i>Цифры только из БД, не выдумываю. Сам ничего не отправляю — ты пересылаешь.</i>"
)


# ── утилиты доставки ───────────────────────────────────────────
CHECKLIST_PROMPT = (
    "Составь ЧЕК-ЛИСТ на сегодня для аккаунт-менеджера (учитывай БРИФ ОТ БДМ из системного промпта). "
    "Данные подтяни: priority_brief, integration_list, integration_candidates, bdm_conditions для приоритетных. "
    "Формат — пункты с □, компактно:\n"
    "1) <b>Приоритет</b>: по каждому приоритетному мерчу — статус конверсии и КОНКРЕТНОЕ действие "
    "(проблема → какая+что делать; ок → 'мониторить').\n"
    "2) <b>Интеграция — написать сегодня</b>: активный пуш из брифа + кого давно не дёргали из трекера "
    "+ кандидаты. По каждому: спросить как идёт интеграция/нужна ли помощь. Через chat_coverage проверь рабочий чат; "
    "если чата нет — пометь «нет чата → сообщить БДМ». НЕ включай мерчей из DO_NOT_CONTACT.\n"
    "3) <b>Спец-задачи</b>: постоянные задачи из брифа. "
    "Через chat_coverage проверь, что у приоритетных есть полный набор чатов.\n"
    "4) <b>Напоминания/заметки</b>, если есть.\n"
    "В конце блок «❓ К БДМ:» — что уточнить/где давно не писали в чате. Без воды.")

OUTBOX_PROMPT = (
    "Подготовь «сообщения на сегодня». Посмотри priority_brief и integration_list. Для каждого, кому есть "
    "смысл написать (просадка/проблема у приоритетных; интеграционные, кого давно не дёргали), дай ГОТОВЫЙ "
    "к пересылке текст [МЕРЧУ] — короткий, вежливый, по делу. Каждый блок: кому + текст в <blockquote>. "
    "Если писать некому — так и скажи.")

BDM_PROMPT = (
    "Сделай краткий статус-апдейт для БДМ (для таблицы/отчёта). По приоритетным мерчам — строкой: "
    "конверсия сегодня/7д, оборот, главное событие/риск. По интеграции (integration_list) — этап и когда "
    "последний контакт. Сухо, структурно, готово к копированию. Без готовых сообщений мерчам.")


def _md_to_html(t):
    t = re.sub(r"```[a-zA-Z]*\n?", "", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.S)
    t = re.sub(r"(?<!\w)`([^`\n]+?)`(?!\w)", r"<code>\1</code>", t)
    return t


def _strip_tags(t):
    return re.sub(r"<[^>]+>", "", t)


async def _deliver(bot, chat_id, text, with_menu=False):
    text = _md_to_html(text)
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for idx, chunk in enumerate(chunks):
        rm = _menu() if (with_menu and idx == len(chunks) - 1) else None
        try:
            await bot.send_message(chat_id, chunk, parse_mode="HTML",
                                   reply_markup=rm, disable_web_page_preview=True)
        except Exception as e:
            logger.warning(f"HTML send failed ({e}); plain")
            await bot.send_message(chat_id, _strip_tags(chunk), reply_markup=rm,
                                   disable_web_page_preview=True)


async def _send(update: Update, text):
    await _deliver(update.get_bot(), update.effective_chat.id, text, with_menu=True)


async def _think(update, context, user_content, label=None):
    """Запрос к мозгу (в отдельном потоке — не блокируем event loop) с памятью диалога."""
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    hist = context.user_data.setdefault("hist", [])
    files = []
    try:
        answer = await asyncio.to_thread(brain.run, user_content, hist[-8:], 6, files)
    except Exception as e:
        logger.error(f"brain error: {e}")
        answer = f"❌ Ошибка анализа: {str(e)[:200]}"
    hist.append({"role": "user", "content": user_content if isinstance(user_content, str) else (label or "[вложение]")})
    hist.append({"role": "assistant", "content": answer})
    del hist[:-8]
    await _send(update, answer)
    # выгрузки (export_excel) — отправляем файлами
    for f in files:
        try:
            with open(f["path"], "rb") as fh:
                await context.bot.send_document(update.effective_chat.id, fh,
                                                filename=f["path"].split("/")[-1],
                                                caption=f.get("caption", ""))
        except Exception as e:
            logger.error(f"send_document error: {e}")


# ── inline-клавиатуры ──────────────────────────────────────────
def _merchant_list():
    pri = []
    for p in PRIORITY:
        mm = data.resolve_merchant(p)
        if mm and mm not in pri:
            pri.append(mm)
    for mm in data.list_merchants():
        if mm not in pri:
            pri.append(mm)
    return pri


def _picker(context):
    ml = _merchant_list()
    context.user_data["mlist"] = ml
    rows, n = [], min(len(ml), 14)
    for i in range(0, n, 2):
        row = [InlineKeyboardButton(("⭐ " if ml[i] in _resolved_priority() else "") + ml[i][:18],
                                    callback_data=f"msel:{i}")]
        if i + 1 < n:
            row.append(InlineKeyboardButton(("⭐ " if ml[i + 1] in _resolved_priority() else "") + ml[i + 1][:18],
                                            callback_data=f"msel:{i + 1}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔎 Найти другого", callback_data="nav:msearch")])
    return InlineKeyboardMarkup(rows)


_PRI_CACHE = {}


def _resolved_priority():
    if not _PRI_CACHE:
        for p in PRIORITY:
            mm = data.resolve_merchant(p)
            if mm:
                _PRI_CACHE[mm] = True
    return _PRI_CACHE


def _action_kb(i):
    A = [("📊 Обзор", "overview"), ("📈 Тренд", "trend"), ("🧭 Поведение", "behavior"),
         ("💸 В подвисе", "limbo"), ("💳 Ставки", "rates"), ("📊 Бенчмарк", "bench"),
         ("📤 Выплаты", "payouts"), ("🗒 Заметки", "notes")]
    rows = []
    for j in range(0, len(A), 2):
        row = [InlineKeyboardButton(A[j][0], callback_data=f"act:{i}:{A[j][1]}")]
        if j + 1 < len(A):
            row.append(InlineKeyboardButton(A[j + 1][0], callback_data=f"act:{i}:{A[j + 1][1]}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🤖 AI-разбор", callback_data=f"act:{i}:ai"),
                 InlineKeyboardButton("✉️ Сообщение мерчу", callback_data=f"act:{i}:msg")])
    rows.append([InlineKeyboardButton("⬅️ К мерчантам", callback_data="nav:merchants")])
    return InlineKeyboardMarkup(rows)


def _integ_kb(context):
    items = store.integ_list()
    context.user_data["ilist"] = [it["merchant"] for it in items]
    rows = []
    for i, it in enumerate(items[:12]):
        rows.append([InlineKeyboardButton(f"✅ контакт: {it['merchant'][:14]}", callback_data=f"intg:touch:{i}"),
                     InlineKeyboardButton("✉️ чек-ин", callback_data=f"intg:msg:{i}")])
    rows.append([InlineKeyboardButton("➕ Добавить мерча", callback_data="intg:add"),
                 InlineKeyboardButton("🔍 Авто-кандидаты", callback_data="intg:cand")])
    return InlineKeyboardMarkup(rows)


def _cand_kb(context):
    cands = data.integration_candidates()
    context.user_data["clist"] = [c["merchant"] for c in cands]
    rows = [[InlineKeyboardButton(f"➕ {c['merchant'][:20]}", callback_data=f"intgcand:{i}")]
            for i, c in enumerate(cands[:12])]
    rows.append([InlineKeyboardButton("⬅️ К трекеру", callback_data="intg:back")])
    return cands, InlineKeyboardMarkup(rows)


# ── команды/меню ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner(update):
        return
    context.user_data.clear()
    await update.message.reply_text(
        "👋 <b>Привет! Я твой личный AM-ассистент Acme.</b>\n\n" + HELP,
        parse_mode="HTML", reply_markup=_menu())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner(update):
        return
    txt = (update.message.text or "").strip()
    mode = context.user_data.get("mode")

    # режимы ввода
    if mode == "order":
        context.user_data["mode"] = None
        try:
            await _send(update, views.order_card(data.lookup_order(txt)))
        except Exception as e:
            await _send(update, f"❌ Ошибка сверки: {str(e)[:150]}")
        return
    if mode == "api":
        context.user_data["mode"] = None
        await _think(update, context, f"Технический вопрос по интеграции/API: «{txt}». Обязательно сверься "
                     "через search_api_docs и ответь точно (эндпоинт, поля, ответы). Если уместно — дай готовое "
                     "сообщение мерчу.")
        return
    if mode == "intg_add":
        context.user_data["mode"] = None
        parts = [p.strip() for p in txt.split(";")]
        store.integ_add(parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")
        await _send(update, "✅ Добавил в трекер.\n\n" + views.integration(store.integ_list()))
        return
    if mode == "merchant_search":
        context.user_data["mode"] = None
        m = data.resolve_merchant(txt)
        if not m:
            await _send(update, f"❌ Мерчант «{escape(txt)}» не найден.")
            return
        ml = _merchant_list()
        if m not in ml:
            ml = [m] + ml
        context.user_data["mlist"] = ml
        await update.message.reply_text(f"🏪 <b>{escape(m)}</b> — выбери действие:",
                                        parse_mode="HTML", reply_markup=_action_kb(ml.index(m)))
        return

    # reply-кнопки
    if txt == BTN_HELP:
        await update.message.reply_text(HELP, parse_mode="HTML", reply_markup=_menu()); return
    if txt == BTN_ANALYTICS:
        await update.message.reply_text(
            "📊 <b>Спроси что угодно по данным</b> — я сам посчитаю по базе. Примеры:\n\n"
            "• конверсия Alpha за сегодня\n"
            "• кто лучше по конверсии за неделю — Bravo, Alpha или Delta\n"
            "• разбери Alpha трансгран по проектам, топ худших\n"
            "• почему UZ BankA плохо конвертит\n"
            "• динамика Bravo по часам\n"
            "• дохлые реки Bravo / зависшие выплаты Alpha\n"
            "• выгрузи в эксель заявки Charlie havale за сегодня\n"
            "• дай текст коллегам/мерчу по …\n\n"
            "Просто напиши вопрос своими словами.",
            parse_mode="HTML", reply_markup=_menu()); return
    if txt == BTN_CHECK:
        await _think(update, context, CHECKLIST_PROMPT)
        return
    if txt in (BTN_BRIEF, BTN_PRIORITY):
        await _think(update, context,
                     "Сделай сводку по приоритетным мерчам (priority_brief): по каждому ситуация, диагноз, "
                     "что делать сегодня, и список 'кому писать'. Учти заметки. Компактно.")
        return
    if txt == BTN_OUTBOX:
        await _think(update, context, OUTBOX_PROMPT)
        return
    if txt == BTN_BDM:
        await _think(update, context, BDM_PROMPT)
        return
    if txt == BTN_MERCHANTS:
        await update.message.reply_text("🏪 <b>Выбери мерчанта:</b>", parse_mode="HTML",
                                        reply_markup=_picker(context)); return
    if txt == BTN_INTEG:
        await update.message.reply_text(views.integration(store.integ_list()), parse_mode="HTML",
                                        reply_markup=_integ_kb(context)); return
    if txt == BTN_ORDER:
        context.user_data["mode"] = "order"
        await update.message.reply_text("🧾 Пришли ID заявки/выплаты (external_id / merchant_order / uuid):"); return
    if txt == BTN_API:
        context.user_data["mode"] = "api"
        await update.message.reply_text("🛠 Какой вопрос по API/интеграции? (напр. «как создать платёж», «статус выплаты»)"); return
    if txt == BTN_CHATDUMP:
        await update.message.reply_text(
            "📂 <b>Разбор чата</b>\n\n"
            "1. В Telegram Desktop открой чат → ⋮ → <b>Export chat history</b> → формат <b>Machine-readable JSON</b> "
            "(фото/видео можно не включать) → Export.\n"
            "2. Пришли мне сюда файл <code>result.json</code>.\n\n"
            "Первый раз разберу всё (последние сообщения), дальше — <b>только новое</b> с прошлой выгрузки "
            "(дедуп по id, одно и то же не перечитываю). Можешь каждый раз слать полную выгрузку — сам пойму, что новое.",
            parse_mode="HTML", reply_markup=_menu())
        return
    if mode == "test_amount":
        context.user_data["mode"] = None
        eid = context.user_data.get("test_eid")
        ename = context.user_data.get("test_ename", "")
        amts = []
        for part in txt.replace(",", " ").split():
            if part.isdigit():
                amts.append(int(part))
        if not eid or not amts:
            await update.message.reply_text("Нужны суммы числами, напр.: <code>1000 5000 50000</code>",
                                            parse_mode="HTML", reply_markup=_menu())
            return
        await _run_test(update, context, eid, ename, amts)
        return
    if txt == BTN_TEST:
        eps = tester.endpoints()
        if not eps:
            await update.message.reply_text("Нет настроенных тест-эндпоинтов (test_endpoints.json пуст).",
                                            reply_markup=_menu())
            return
        names = list(eps.keys())
        context.user_data["test_eps"] = names
        rows = [[InlineKeyboardButton(n, callback_data=f"test:{i}")] for i, n in enumerate(names)]
        await update.message.reply_text(
            "🧪 <b>Тест метода</b> — выбери эндпоинт (метод/провайдер). Прогоню /init по суммам "
            f"({', '.join(str(a) for a in tester.DEFAULT_AMOUNTS)}₽):",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return
    if txt == BTN_PROVIDERS:
        try:
            gw = data.gate_window(120)
            base = data.gate_conv_baseline(7)
            lines = ["🏦 <b>Провайдеры (2ч)</b>", ""]
            for g in gw:
                if g["gate"] == "(нет гейта)" or g["total"] < 5:
                    continue
                b = base.get(g["gate"], 0)
                warn = " ⚠️" if (g["failed"] >= 10 and g["no_acct_share"] >= 50) else ""
                lines.append(f"• <b>{escape(g['gate'])}</b>: {g['conv']:.0f}% (база {b:.0f}%) · "
                             f"{g['success']}/{g['total']}{warn}")
                if g["failed"] >= 5 and g["no_acct_share"] >= 30:
                    lines.append(f"    └ без счёта: {g['no_acct_share']:.0f}% фейлов ({g['failed_no_acct']}/{g['failed']})")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=_menu())
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:150]}")
        return
    if txt == BTN_ANTIFRAUD:
        try:
            af = data.antifraud_window(120)
            lines = [f"🛡 <b>Антифрод (2ч)</b>", "",
                     f"Зарезано (ROUTING_ERROR): <b>{af['rejected']}/{af['total']}</b> = {af['rate']:.1f}%"]
            if af["by_merchant"]:
                lines.append("\nПо мерчам:")
                for m in af["by_merchant"]:
                    lines.append(f"  • {escape(m['merchant'])}: {m['rate']:.0f}% ({m['rejected']}/{m['total']})")
            lines.append("\n<i>Высокий % = антифрод режет трафик до провайдера. Можно смягчить правила под нишу.</i>")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=_menu())
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:150]}")
        return
    if txt == BTN_STATUS:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("💾 Сделать бэкап сейчас", callback_data="guard:backup")]])
        await update.message.reply_text("🛡 <b>Статус системы</b>\n\n" + guard.health_report(),
                                        parse_mode="HTML", reply_markup=kb)
        return
    if txt == BTN_NOTES:
        ns = store.recent_notes(20)
        if not ns:
            await update.message.reply_text("🗒 Заметок пока нет. Напиши: «запиши по Alpha: …»", reply_markup=_menu())
        else:
            lines = ["🗒 <b>Последние заметки</b>", ""]
            for n in ns:
                lines.append(f"<code>{n['when']}</code> <b>{escape(n.get('merchant') or '')}</b>: {escape(n['text'])}")
            await _send(update, "\n".join(lines))
        return

    # пересланное сообщение мерча
    fwd = bool(getattr(update.message, "forward_origin", None) or getattr(update.message, "forward_date", None))
    if fwd:
        await _think(update, context,
                     f"Сообщение от мерчанта/контрагента (переслано):\n\n«{txt}»\n\nРазбери суть, при нужде "
                     "подтяни данные, предложи готовый черновик ответа [МЕРЧУ].")
        return

    # свободный вопрос
    await _think(update, context, txt)


def _test_icon(r):
    if r["ok"]:
        return "✅"
    code = (r.get("code") or "").upper()
    if "ROUTE" in code or "TERMINAL" in code:
        return "🚫"   # нет маршрута/терминала
    return "⚠️"       # дошло до провайдера, но ошибка


async def _run_test(src, context, eid, ename, amounts):
    bot = context.bot
    if hasattr(src, "effective_chat") and src.effective_chat:
        chat = src.effective_chat.id
    elif hasattr(src, "chat") and src.chat:
        chat = src.chat.id
    else:
        chat = OWNER_ID
    await bot.send_chat_action(chat, ChatAction.TYPING)
    try:
        results = await asyncio.to_thread(tester.run, eid, amounts)
    except Exception as e:
        await _deliver(bot, chat, f"❌ Ошибка теста: {str(e)[:150]}")
        return
    lines = [f"🧪 <b>Тест: {escape(ename)}</b>", ""]
    for r in results:
        amt = f"{r['amount']:,}".replace(",", " ") + "₽"
        if r["ok"]:
            short = (r.get("url") or "")[:60]
            lines.append(f"{_test_icon(r)} <b>{amt}</b> → {escape(str(r['status']))} · <code>{escape(short)}</code>")
        else:
            lines.append(f"{_test_icon(r)} <b>{amt}</b> → {escape(str(r.get('code') or r.get('status')))}"
                         f" · {escape(str(r.get('message') or '')[:60])}")
    ok_n = sum(1 for r in results if r["ok"])
    lines.append(f"\n<i>Успешно (redirect/QR): {ok_n} из {len(results)}. "
                 f"🚫 нет маршрута · ⚠️ ошибка провайдера.</i>")
    await _deliver(bot, chat, "\n".join(lines))


# ── inline-колбэки ─────────────────────────────────────────────
async def _do_action(q, context, m, i, action):
    if action in ("ai", "msg"):
        await q.answer("⏳ анализирую…")
        await context.bot.send_chat_action(q.message.chat_id, ChatAction.TYPING)
        prompt = (f"Разбери мерчанта {m}: ситуация, диагноз (наша/гейт vs трафик), что делать, готовое сообщение."
                  if action == "ai" else
                  f"Составь готовое вежливое сообщение мерчу {m} по текущей ситуации (подтяни данные). "
                  "Только сам текст [МЕРЧУ], по делу.")
        try:
            body = await asyncio.to_thread(brain.run, prompt)
        except Exception as e:
            body = f"❌ Ошибка: {str(e)[:150]}"
        await _deliver(context.bot, q.message.chat_id, body)
        return
    await q.answer()
    try:
        if action == "overview":
            txt = views.overview(m, data.merchant_overview(m))
        elif action == "trend":
            txt = views.trend(m, data.merchant_trend(m, 7))
        elif action == "behavior":
            txt = views.behavior(m, data.merchant_behavior(m, 24))
        elif action == "limbo":
            txt = views.limbo(m, data.money_in_limbo(m, 7))
        elif action == "rates":
            txt = views.rates(m, data.merchant_rates(m))
        elif action == "bench":
            txt = views.benchmark(m, data.platform_benchmark(m))
        elif action == "payouts":
            txt = views.withdrawals(m, data.withdrawals_overview(m, 7))
        elif action == "notes":
            txt = views.notes(m, store.get_notes(m))
        else:
            txt = "Неизвестное действие."
    except Exception as e:
        logger.error(f"action {action}: {e}")
        txt = f"❌ Ошибка: {str(e)[:150]}"
    try:
        await q.edit_message_text(txt[:3900], parse_mode="HTML", reply_markup=_action_kb(i))
    except Exception:
        try:
            await q.edit_message_text(_strip_tags(txt)[:3900], reply_markup=_action_kb(i))
        except Exception:
            await _deliver(context.bot, q.message.chat_id, txt)


async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not (q.from_user and q.from_user.id == OWNER_ID):
        await q.answer(); return
    d = q.data or ""
    try:
        if d == "guard:backup":
            await q.answer("⏳ делаю бэкап…")
            await _do_backup(context, manual=True)
            return
        if d.startswith("test:"):
            await q.answer("⏳ шлю /init…")
            i = int(d.split(":")[1])
            names = context.user_data.get("test_eps") or list(tester.endpoints().keys())
            if i >= len(names):
                await q.message.reply_text("Список устарел — открой 🧪 Тест метода заново."); return
            ename = names[i]
            eid = tester.endpoints().get(ename)
            context.user_data["test_eid"] = eid
            context.user_data["test_ename"] = ename
            await _run_test(q.message, context, eid, ename, tester.DEFAULT_AMOUNTS)
            await q.message.reply_text(
                "Своя сумма? Жми и пришли числа (через пробел):",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Свои суммы", callback_data="test_custom")]]))
            return
        if d == "test_custom":
            await q.answer()
            context.user_data["mode"] = "test_amount"
            await q.message.reply_text(f"Пришли суммы для «{escape(context.user_data.get('test_ename',''))}» "
                                       "числами через пробел (напр. <code>1000 7777 99999</code>):",
                                       parse_mode="HTML")
            return
        if d == "nav:help":
            await q.answer(); await q.edit_message_text(HELP, parse_mode="HTML"); return
        if d == "nav:merchants":
            await q.answer()
            await q.edit_message_text("🏪 <b>Выбери мерчанта:</b>", parse_mode="HTML", reply_markup=_picker(context))
            return
        if d == "nav:msearch":
            await q.answer()
            context.user_data["mode"] = "merchant_search"
            await q.edit_message_text("🔎 Напиши название мерчанта:")
            return
        if d.startswith("msel:"):
            await q.answer()
            i = int(d.split(":")[1])
            ml = context.user_data.get("mlist") or _merchant_list()
            context.user_data["mlist"] = ml
            if i >= len(ml):
                await q.edit_message_text("Список устарел — открой 🏪 Мерчанты заново."); return
            await q.edit_message_text(f"🏪 <b>{escape(ml[i])}</b> — выбери действие:",
                                      parse_mode="HTML", reply_markup=_action_kb(i))
            return
        if d.startswith("act:"):
            _, i, action = d.split(":", 2)
            i = int(i)
            ml = context.user_data.get("mlist") or _merchant_list()
            context.user_data["mlist"] = ml
            if i >= len(ml):
                await q.answer("Список устарел", show_alert=True); return
            await _do_action(q, context, ml[i], i, action)
            return
        if d.startswith("intg:touch:"):
            await q.answer("Отметил контакт ✅")
            i = int(d.split(":")[2])
            il = context.user_data.get("ilist") or [it["merchant"] for it in store.integ_list()]
            if i < len(il):
                store.integ_touch(il[i])
            await q.edit_message_text(views.integration(store.integ_list()), parse_mode="HTML",
                                      reply_markup=_integ_kb(context))
            return
        if d.startswith("intg:msg:"):
            await q.answer("⏳ готовлю сообщение…")
            i = int(d.split(":")[2])
            il = context.user_data.get("ilist") or [it["merchant"] for it in store.integ_list()]
            if i >= len(il):
                return
            await context.bot.send_chat_action(q.message.chat_id, ChatAction.TYPING)
            body = await asyncio.to_thread(
                brain.run, f"Составь короткое вежливое чек-ин сообщение мерчу {il[i]} на интеграции: "
                           "как продвигается интеграция, нужна ли помощь с нашей стороны. Только текст [МЕРЧУ].")
            await _deliver(context.bot, q.message.chat_id, body)
            return
        if d == "intg:add":
            await q.answer()
            context.user_data["mode"] = "intg_add"
            await q.edit_message_text("➕ Напиши: <code>Мерчант; этап; заметка</code> (этап и заметка опц.)",
                                      parse_mode="HTML")
            return
        if d == "intg:back":
            await q.answer()
            await q.edit_message_text(views.integration(store.integ_list()), parse_mode="HTML",
                                      reply_markup=_integ_kb(context))
            return
        if d == "intg:cand":
            await q.answer("⏳ ищу кандидатов…")
            cands, kb = _cand_kb(context)
            if not cands:
                await q.edit_message_text("🔍 Явных кандидатов на интеграцию в данных нет.",
                                          reply_markup=_integ_kb(context))
                return
            lines = ["🔍 <b>Авто-кандидаты на интеграцию</b>", "",
                     "<i>(новые мерчи / есть трафик, но ~0 успехов)</i>", ""]
            for c in cands[:12]:
                lines.append(f"• <b>{escape(c['merchant'])}</b> — {escape(c['reason'])}")
            lines.append("\nДобавь кнопкой ниже 👇")
            await q.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
            return
        if d.startswith("intgcand:"):
            i = int(d.split(":")[1])
            cl = context.user_data.get("clist") or []
            if i < len(cl):
                store.integ_add(cl[i], "кандидат (авто)", "")
                await q.answer(f"Добавил {cl[i]} ✅")
            await q.edit_message_text(views.integration(store.integ_list()), parse_mode="HTML",
                                      reply_markup=_integ_kb(context))
            return
        await q.answer()
    except Exception as e:
        logger.error(f"cb error: {e}")
        try:
            await q.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)
        except Exception:
            pass


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner(update):
        return
    try:
        photo = update.message.photo[-1]
        f = await context.bot.get_file(photo.file_id)
        buf = await f.download_as_bytearray()
        b64 = base64.b64encode(bytes(buf)).decode()
        caption = (update.message.caption or "").strip()
        content = [
            {"type": "text", "text": (caption or "Разбери этот скрин (ошибка/дашборд/переписка) "
                                      "и дай вывод + что делать. Если упомянут мерч — подтяни данные.")},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]
    except Exception as e:
        logger.error(f"photo error: {e}")
        await update.message.reply_text(f"❌ Не смог прочитать скрин: {str(e)[:150]}")
        return
    await _think(update, context, content, label=f"[скрин] {caption}".strip())


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner(update):
        return
    doc = update.message.document
    fname = (doc.file_name or "").lower()
    if not fname.endswith(".json"):
        await update.message.reply_text(
            "📂 Жду выгрузку чата в формате <b>Machine-readable JSON</b> (result.json). "
            "HTML не подойдёт — переэкспортируй в JSON.", parse_mode="HTML")
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        f = await context.bot.get_file(doc.file_id)
        raw = bytes(await f.download_as_bytearray())
    except Exception as e:
        logger.error(f"download file: {e}")
        await update.message.reply_text(f"❌ Не смог скачать файл: {str(e)[:150]}")
        return

    # ── ветка «полная выгрузка»: result.json со всеми чатами ──
    if chatdump.is_full_export(raw):
        await update.message.reply_text(
            "📂 Опознал ПОЛНУЮ выгрузку — запускаю анализ всех чатов и ставлю напоминания на сегодня…",
            parse_mode="HTML")
        try:
            import morning_analyzer
            res = await asyncio.to_thread(morning_analyzer.analyze_and_schedule, raw)
            summary = morning_analyzer.format_summary(res)
        except Exception as e:
            logger.exception("morning_analyzer failed")
            await update.message.reply_text(f"❌ Анализ упал: {str(e)[:200]}")
            return
        await _send(update, summary)
        return

    # ── одиночный чат (старая логика) ──
    try:
        name, cid, msgs = chatdump.parse(raw)
    except Exception as e:
        logger.error(f"chatdump parse: {e}")
        await update.message.reply_text(f"❌ Не смог разобрать файл: {str(e)[:150]}")
        return
    if not msgs:
        await update.message.reply_text("📂 В выгрузке нет текстовых сообщений.")
        return
    key = f"{name}|{cid}"
    first, new, total = chatdump.new_since(key, msgs)
    if not new:
        await update.message.reply_text(
            f"📂 <b>{escape(name)}</b>: новых сообщений нет с прошлой выгрузки (всего {total}).",
            parse_mode="HTML", reply_markup=_menu())
        return
    head = (f"📂 <b>{escape(name)}</b> · "
            + (f"первый разбор, {len(new)} из {total} сообщений" if first
               else f"новых сообщений: {len(new)}"))
    await update.message.reply_text(head, parse_mode="HTML")
    prompt = (
        f"Это выгрузка рабочего чата «{name}» (мерчант/интеграция/тех). Ниже {'последние' if first else 'НОВЫЕ'} "
        f"сообщения с датами/временем. Текущее время есть в системном сообщении. Разбери как AM-копайлот "
        "ПО ВСЕМ АСПЕКТАМ и дай конкретные действия + готовые тексты для отправки:\n\n"
        "1) ⏳ <b>Тишина/задержки</b>: от кого давно нет ответа и сколько (по датам последних сообщений). "
        "Если мы ждём их или они ждут нас — отметь и дай готовое сообщение-пинг «как продвигается, что нужно с нашей стороны».\n"
        "2) 🧩 <b>Статус интеграции</b>: на каком этапе, что блокирует, нужна ли помощь. Готовый чек-ин мерчу.\n"
        "3) 🏷 <b>Ставки/условия</b>: что в чате обговорено по ставкам/методам. Сверь с таблицей БДМ (bdm_conditions) "
        "и фактическими в системе (merchant_rates) — флагни расхождения или несогласованные ставки.\n"
        "4) 🔴 <b>Проблемы/ошибки</b>: ROUTE_ERROR, не проходит, антифрод, претензии, падение конверсии.\n"
        "5) ❓ <b>Открытые вопросы</b>: на что НЕ ответили (кто кого ждёт) → готовый ответ.\n"
        "6) 🎯 <b>Что мне сделать сегодня</b>: список действий, кому написать (с готовым текстом [МЕРЧУ]/[HEAD SUPPORT]).\n\n"
        "Если упомянут мерчант — обязательно подтяни его данные инструментами (overview/rates/bdm_conditions/integration). "
        "Не выдумывай. Если всё спокойно и действий не нужно — скажи прямо. Структурно, по делу.\n\n"
        f"=== СООБЩЕНИЯ ===\n{chatdump.transcript(new)}")
    try:
        answer = await asyncio.to_thread(brain.run, prompt)
        chatdump.commit(key, msgs)
    except Exception as e:
        logger.error(f"chatdump brain: {e}")
        answer = f"❌ Ошибка анализа: {str(e)[:200]}"
    await _send(update, answer)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _owner(update):
        return
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        v = update.message.voice or update.message.audio
        f = await context.bot.get_file(v.file_id)
        buf = await f.download_as_bytearray()
        bio = io.BytesIO(bytes(buf)); bio.name = "voice.oga"
        text = await asyncio.to_thread(brain.transcribe, bio)
    except Exception as e:
        logger.error(f"voice error: {e}")
        await update.message.reply_text(f"❌ Не смог распознать голос: {str(e)[:150]}")
        return
    if not text:
        await update.message.reply_text("🎙 Не расслышал, повтори?")
        return
    await update.message.reply_text(f"🎙 <i>«{escape(text)}»</i>", parse_mode="HTML")
    await _think(update, context, text)


# ── фоновые задачи ─────────────────────────────────────────────
async def monitor_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        to_send, recovered, escalations = monitor.process()
    except Exception as e:
        logger.error(f"monitor: {e}")
        return
    quiet = _is_quiet()
    for a in to_send:
        if quiet:
            store.quiet_add(f"🔔 {a['title']} — {a['detail']}"); continue
        # детерминированный заголовок: мерчант/гейт + цифры ВСЕГДА видны (не зависит от текста модели)
        hdr = f"🔔 <b>АЛЕРТ — {escape(a['title'])}</b>\n{escape(a['detail'])}"
        try:
            body = await asyncio.to_thread(
                brain.run, f"Сработал авто-алерт по «{a['merchant']}»: «{a['title']} — {a['detail']}». "
                           f"Дай диагноз (наша/гейт vs трафик) + что делать + готовое сообщение. "
                           f"НЕ повторяй заголовок, начни сразу с диагноза. Кратко.")
        except Exception:
            body = ""
        await _deliver(context.bot, OWNER_ID, hdr + ("\n\n" + body if body else ""))
    for e in escalations:
        if quiet:
            store.quiet_add(f"🚨 ЭСКАЛАЦИЯ {e['title']} (висит {e.get('minutes')}м)"); continue
        hdr = f"🚨🚨 <b>ЭСКАЛАЦИЯ — {escape(e['title'])}</b>\n{escape(e.get('detail', ''))} · висит {e.get('minutes')} мин"
        try:
            body = await asyncio.to_thread(
                brain.run, f"ЭСКАЛАЦИЯ по «{e['merchant']}»: «{e['title']}» не решается {e.get('minutes')} мин — "
                           "НАША/гейтовая сторона. Краткий статус + готовое сообщение [HEAD SUPPORT]. "
                           "НЕ повторяй заголовок.")
        except Exception:
            body = ""
        await _deliver(context.bot, OWNER_ID, hdr + ("\n\n" + body if body else ""))
    for r in recovered:
        if quiet:
            continue
        try:
            await context.bot.send_message(OWNER_ID, f"✅ <b>Восстановилось:</b> {escape(r.get('title', ''))}",
                                            parse_mode="HTML")
        except Exception as ex:
            logger.error(f"recovery: {ex}")


async def _push_brief(context, prompt, header):
    try:
        body = await asyncio.to_thread(brain.run, prompt)
    except Exception as e:
        logger.error(f"push {header}: {e}")
        return
    await _deliver(context.bot, OWNER_ID, header + body)


async def morning_brief_job(context: ContextTypes.DEFAULT_TYPE):
    # сначала отдаём, что накопилось за тихие часы
    q = store.quiet_flush()
    if q:
        lines = ["🌙 <b>За ночь (тихие часы):</b>", ""] + [f"• {escape(x['text'])}" for x in q[-25:]]
        await _deliver(context.bot, OWNER_ID, "\n".join(lines))
    # главный утренний артефакт — чек-лист на день
    await _push_brief(context, CHECKLIST_PROMPT, "✅ <b>Чек-лист на сегодня</b>\n\n")


async def integration_nudge_job(context: ContextTypes.DEFAULT_TYPE):
    """Дневное напоминание по интеграционным мерчам, с кем давно не связывались + готовые чек-ины."""
    if _is_quiet():
        return
    items = [it for it in store.integ_list()
             if it["days_since_contact"] is None or it["days_since_contact"] >= INTEG_NUDGE_DAYS]
    if not items:
        return
    names = ", ".join(it["merchant"] for it in items[:8])
    await _push_brief(context,
                      f"Эти интеграционные мерчи давно без контакта: {names}. Для каждого дай короткое готовое "
                      "чек-ин сообщение [МЕРЧУ] (как идёт интеграция, нужна ли помощь). Списком, каждый в <blockquote>.",
                      "🧩 <b>Интеграция — пора написать:</b>\n\n")


async def shift_summary_job(context: ContextTypes.DEFAULT_TYPE):
    await _push_brief(context,
                      "Итог дня по приоритетным: конверсия/оборот сегодня, что было проблемного (behavior), деньги "
                      "в подвисе если заметно, что осталось на завтра (заметки/напоминания). Коротко.",
                      "🌆 <b>Итог дня</b>\n\n")


async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    if _dt.now(_tz(_td(hours=3))).weekday() != 0:
        return
    await _push_brief(context,
                      "Недельный отчёт по приоритетным (merchant_trend(7)+overview+platform_benchmark): динамика "
                      "конверсии/оборота за неделю, что улучшилось/ухудшилось, бенчмарк, риски и фокус на неделю.",
                      "🗓 <b>Недельный отчёт</b>\n\n")


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        for r in store.due_reminders():
            await context.bot.send_message(OWNER_ID, f"⏰ <b>Напоминание:</b> {escape(r.get('text', ''))}",
                                            parse_mode="HTML")
    except Exception as e:
        logger.error(f"reminder: {e}")


async def _do_backup(context, manual=False):
    """Сделать бэкап и прислать архив владельцу в ЛС (off-site копия)."""
    try:
        path, n = guard.make_backup()
    except Exception as e:
        logger.error(f"backup: {e}")
        await context.bot.send_message(OWNER_ID, f"❌ Бэкап не удался: {str(e)[:150]}")
        return
    if not path:
        if manual:
            await context.bot.send_message(OWNER_ID, "ℹ️ Нечего бэкапить (state-файлы не найдены).")
        return
    try:
        with open(path, "rb") as f:
            await context.bot.send_document(
                OWNER_ID, document=f, filename=os.path.basename(path),
                caption=f"💾 Бэкап Acme — {n} файлов · {datetime.now(MSK).strftime('%d.%m %H:%M')} МСК")
    except Exception as e:
        logger.error(f"backup send: {e}")
        await context.bot.send_message(OWNER_ID, f"✅ Бэкап создан ({n} файлов), но не отправился: {str(e)[:120]}")


async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    await _do_backup(context, manual=False)


async def watchdog_job(context: ContextTypes.DEFAULT_TYPE):
    """Сторож живости: сервисы/реплика/диск/флаппинг → алерт владельцу при проблеме."""
    prev = context.bot_data.get("guard_restarts")
    try:
        problems, restarts = guard.detect_problems(prev)
    except Exception as e:
        logger.error(f"watchdog: {e}")
        return
    context.bot_data["guard_restarts"] = restarts
    if not problems:
        return
    # троттлинг: не повторять идентичный набор проблем чаще раза в 30 мин
    import time as _t
    key = "|".join(sorted(problems))
    last = context.bot_data.get("guard_last", {})
    now = _t.time()
    if now - last.get(key, 0) < 1800:
        return
    last[key] = now
    context.bot_data["guard_last"] = last
    await _deliver(context.bot, OWNER_ID, "🛡 <b>WATCHDOG — проблема в системе</b>\n\n" + "\n".join(problems))


def main():
    logger.info("Acme AM-ассистент запускается...")
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY не задан"); return
    logger.info("Реплика подключена" if data.test_connection() else "Реплика НЕдоступна")

    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if app.job_queue:
        # Бот чисто реактивный: два блока (аналитик + тех) отвечают ТОЛЬКО на запрос владельца.
        # Все проактивные авто-сводки (мониторинг/утро/интеграция/недельный/итог дня/напоминания) убраны.
        # Остаётся только инфраструктура, без контентных пушей:
        app.job_queue.run_repeating(watchdog_job, interval=600, first=120)        # сторож, молчит пока всё ок
        app.job_queue.run_daily(backup_job, time=dtime(1, 0))                     # бэкап стейта 04:00 МСК
        logger.info("Задачи: watchdog 10м (молчит пока ок), бэкап 04:00 МСК. Авто-сводки отключены.")
    else:
        logger.warning("JobQueue недоступна — нужен python-telegram-bot[job-queue]")

    logger.info(f"Бот запущен. Владелец: {OWNER_ID}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
