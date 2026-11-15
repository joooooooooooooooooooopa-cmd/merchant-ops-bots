"""
ReportBot — приватный бот выгрузок owner.

Запуск: python bot.py (polling, без вебхука).
ENV: .env рядом с bot.py (см. .env.example).
"""
import asyncio
import logging

from telegram import Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import db
import keys
from config import BOT_TOKEN, LOG_LEVEL, OWNER_ID
from handlers import flow
from handlers.keyboards import (BTN_ADMIN, BTN_CANCEL, BTN_HEALTH, BTN_LAST,
                                BTN_REPORT, kb_admin, main_menu)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")


async def _require_access(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if keys.is_allowed(user.id):
        return True
    msg = update.effective_message
    if msg and msg.text:
        if keys.try_unlock(user.id, msg.text):
            await msg.reply_text(
                "Доступ открыт. Это приватный бот выгрузок Acme.",
                reply_markup=main_menu(keys.is_owner(update.effective_user.id)),
            )
            log.warning(f"access granted by master key to user_id={user.id} (@{user.username})")
            return True
    if msg:
        await msg.reply_text(
            "Закрытый бот. Если у тебя есть ключ доступа — отправь его одним сообщением."
        )
    return False


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_access(update):
        return
    hello = (
        "<b>ReportBot</b> — инструмент выгрузок.\n\n"
        "Собираю Excel за день/диапазон в трёх форматах:\n"
        "— <b>для мерча</b>: без gate/terminal/external_id;\n"
        "— <b>для провайдера</b>: по гейту/терминалу, без merchant_order;\n"
        "— <b>внутренняя</b>: всё подряд, со ставкой.\n\n"
        "В каждом файле листы: Параметры, Сводка, По дням (с линейным графиком), "
        "Анализ причин, Транзакции.\n\n"
        "Источник — read-only реплика Core. Формулы 1:1 с CRM-ботом."
    )
    await update.effective_message.reply_text(hello, parse_mode="HTML", reply_markup=main_menu(keys.is_owner(update.effective_user.id)))


async def cmd_health(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_access(update):
        return
    msg = await update.effective_message.reply_text("Проверяю реплику…")
    ok = await asyncio.to_thread(db.replica_healthy)
    await msg.edit_text("Реплика отвечает." if ok else "Реплика не отвечает.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_access(update):
        return
    ctx.user_data.clear()
    await update.effective_message.reply_text("Сброшено.", reply_markup=main_menu(keys.is_owner(update.effective_user.id)))


async def cmd_whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    txt = (f"id: <code>{u.id}</code>\n"
           f"username: @{u.username or '-'}\n"
           f"owner: <code>{OWNER_ID}</code>\n"
           f"allowed: {keys.is_allowed(u.id)}")
    await update.effective_message.reply_text(txt, parse_mode="HTML")


# ── Админка (только для OWNER) ─────────────────────────────────
def _owner_only(update: Update) -> bool:
    u = update.effective_user
    return bool(u and keys.is_owner(u.id))


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Открыть админ-меню. Доступно только OWNER-у."""
    if not _owner_only(update):
        await update.effective_message.reply_text("Команда доступна только владельцу.")
        return
    await update.effective_message.reply_text(
        "<b>Админка</b>\n\nЧто сделать?",
        parse_mode="HTML",
        reply_markup=kb_admin(),
    )


async def cmd_newkey(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        await update.effective_message.reply_text("Команда доступна только владельцу.")
        return
    code = keys.create_key(update.effective_user.id)
    await update.effective_message.reply_text(
        f"Создан одноразовый ключ:\n\n<code>{code}</code>\n\n"
        f"Перешли его получателю — он отправит ключ боту одним сообщением "
        f"и получит доступ. Ключ срабатывает один раз.",
        parse_mode="HTML",
    )


async def cmd_keys(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        await update.effective_message.reply_text("Команда доступна только владельцу.")
        return
    rows = keys.list_keys()
    if not rows:
        await update.effective_message.reply_text("Ключей пока нет. /newkey — создать.")
        return
    lines = ["<b>Все ключи</b> (новые сверху):", ""]
    for r in rows:
        status = (f"использован user_id=<code>{r['used_by']}</code> "
                  f"({r['used_at'] or '-'})" if r["used_by"] else "не использован")
        lines.append(f"<code>{r['code']}</code> — {status}")
    text = "\n".join(lines)
    # Telegram-лимит 4096; режем по необходимости
    for i in range(0, len(text), 3800):
        await update.effective_message.reply_text(text[i:i + 3800], parse_mode="HTML")


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        await update.effective_message.reply_text("Команда доступна только владельцу.")
        return
    users = keys.list_users()
    if not users:
        await update.effective_message.reply_text(
            "Доступ есть только у тебя. Других — пока нет."
        )
        return
    lines = ["<b>Пользователи с доступом</b>:", ""]
    for uid in users:
        lines.append(f"— <code>{uid}</code>  (отозвать: /revoke {uid})")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _owner_only(update):
        await update.effective_message.reply_text("Команда доступна только владельцу.")
        return
    args = ctx.args or []
    if not args:
        await update.effective_message.reply_text(
            "Формат: <code>/revoke &lt;user_id&gt;</code>", parse_mode="HTML"
        )
        return
    try:
        uid = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id должен быть числом.")
        return
    ok = keys.revoke(uid)
    await update.effective_message.reply_text(
        f"Доступ для <code>{uid}</code> отозван." if ok
        else f"У <code>{uid}</code> и так нет доступа (или это OWNER).",
        parse_mode="HTML",
    )


async def on_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_access(update):
        return
    text = (update.effective_message.text or "").strip()

    if text == BTN_REPORT:
        return await flow.start_report(update, ctx)
    if text == BTN_LAST:
        return await flow.show_last_request(update, ctx)
    if text == BTN_HEALTH:
        return await cmd_health(update, ctx)
    if text == BTN_CANCEL:
        return await cmd_cancel(update, ctx)
    if text == BTN_ADMIN:
        return await cmd_admin(update, ctx)

    handled = await flow.on_text(update, ctx)
    if handled:
        return

    await update.effective_message.reply_text(
        'Жми «Новая выгрузка» в меню. Если меню скрыто — /start.',
        reply_markup=main_menu(keys.is_owner(update.effective_user.id)),
    )


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cq = update.callback_query
    data = (cq.data if cq else "") or ""
    # Админ-каллбэки видит только OWNER.
    if data.startswith("adm:"):
        if not (cq and keys.is_owner(cq.from_user.id)):
            await cq.answer("Только для владельца", show_alert=True)
            return
        await cq.answer()
        action = data.split(":", 1)[1]
        if action == "new":
            code = keys.create_key(cq.from_user.id)
            await cq.message.reply_text(
                f"Создан одноразовый ключ:\n\n<code>{code}</code>\n\n"
                f"Перешли его получателю — отправит боту одним сообщением, "
                f"получит доступ. Ключ срабатывает один раз.",
                parse_mode="HTML",
            )
            return
        if action == "keys":
            return await cmd_keys(update, ctx)
        if action == "users":
            return await cmd_users(update, ctx)
        return
    await flow.on_callback(update, ctx)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"Сбой: {ctx.error}\n(детали в логах)"
            )
        except Exception:
            pass


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан в .env")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    # Админ-команды (доступны только OWNER, гард внутри хендлеров)
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("newkey", cmd_newkey))
    app.add_handler(CommandHandler("keys", cmd_keys))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message))
    app.add_error_handler(on_error)

    log.info(f"ReportBot starting (owner={OWNER_ID})")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
