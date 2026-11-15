"""
Acme CRM Bot v5.0
Модульная архитектура — точка входа и регистрация обработчиков.

Вся логика вынесена в handlers/:
  - utils.py        — утилиты, константы, форматирование чисел
  - keyboards.py    — построители клавиатур
  - formatters.py   — форматирование экранов и CSV-экспорт
  - jobs.py         — фоновые задачи (мониторинг, дайджест, очистка)
  - admin.py        — админ-команды (/newkey, /keys, /users, /revoke, /kickuser)
  - crm.py          — CRM: мерчанты, инциденты, inline-запросы
  - main_handlers.py — основные хендлеры (start, message, callback, document)
"""
import logging
from datetime import datetime

from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, InlineQueryHandler, filters,
)
from telegram import WebAppInfo, MenuButtonWebApp
from telegram.error import NetworkError, TimedOut

from config import BOT_TOKEN, WEBAPP_URL
import metabase as mb

# --- Импорт из модулей ---
from handlers.utils import (
    BTN_ADD_MERCHANT, BTN_EDIT_MERCHANT, BTN_NEW_INCIDENT,
    MERCH_NAME, MERCH_STATUS, MERCH_RATE, MERCH_CONTACT, MERCH_NOTES,
    EDIT_FIND, INC_MERCHANT, INC_TYPE, INC_PRIORITY, INC_DESC, INC_DUE,
)
from handlers.main_handlers import (
    start, handle_message, handle_callback,
    check_config, webapp_command,
)
from handlers.admin import cmd_newkey, cmd_keys, cmd_users, cmd_revoke, cmd_kickuser
from handlers.crm import (
    add_merchant_start, add_merchant_name, add_merchant_status_cb,
    add_merchant_rate, add_merchant_contact, add_merchant_notes,
    cancel_conversation,
    edit_merchant_start, edit_merchant_find,
    inc_create_start, inc_merchant_step, inc_type_cb,
    inc_priority_cb, inc_desc_step, inc_due_cb,
    handle_inline_query,
)
from handlers.jobs import (
    check_conversion, check_critical_drop, check_infra, check_trends, check_heartbeat,
    check_traffic_stop, check_nspk_vip_milestones, check_business_risks,
    check_finance_risks, check_dead_requisites, handle_req_callback, handle_su_callback,
    handle_pfmk_callback, handle_vega_callback,
    check_withdrawals, check_gate_relative, send_traffic_light, check_spam_users,
    check_new_endpoints, send_weekly_trends, check_test_traffic,
    send_daily_digest, send_daily_report, save_daily_conversion_job,
    check_overdue_incidents_job, cleanup_old_data_job,
    CHECK_INTERVAL, CONVERSION_THRESHOLD,
)

logger = logging.getLogger(__name__)


def main():
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  Acme CRM v5.0")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Проверка конфигурации
    config_errors = check_config()
    if config_errors:
        for err in config_errors:
            logger.error(f"Ошибка конфигурации: {err}")
        logger.error("Проверьте файл .env и переменные окружения")
        return

    if mb.test_connection():
        logger.info("Metabase подключен")
    else:
        logger.warning("Metabase недоступен — бот запустится без аналитики")

    logger.info("Запуск бота...")

    async def post_init(application):
        if WEBAPP_URL:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            logger.info(f"Кнопка меню WebApp: {WEBAPP_URL}")
        else:
            logger.info("WEBAPP_URL не задан — кнопка меню не изменена")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Интерактив — ТОЛЬКО в личке. В группах (алертная и любые будущие рабочие чаты)
    # бот лишь шлёт алерты и молчит на чужие сообщения — иначе на каждый текст
    # неавторизованного юзера в группе летело бы «Ключ недействителен».
    PRIVATE_ONLY = filters.ChatType.PRIVATE

    # --- Команды ---
    app.add_handler(CommandHandler("start", start, filters=PRIVATE_ONLY))
    app.add_handler(CommandHandler("webapp", webapp_command, filters=PRIVATE_ONLY))
    app.add_handler(CommandHandler("newkey", cmd_newkey, filters=PRIVATE_ONLY))
    app.add_handler(CommandHandler("keys", cmd_keys, filters=PRIVATE_ONLY))
    app.add_handler(CommandHandler("users", cmd_users, filters=PRIVATE_ONLY))
    app.add_handler(CommandHandler("revoke", cmd_revoke, filters=PRIVATE_ONLY))
    app.add_handler(CommandHandler("kickuser", cmd_kickuser, filters=PRIVATE_ONLY))

    # --- ConversationHandler: добавление мерчанта ---
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Text([BTN_ADD_MERCHANT]) & PRIVATE_ONLY, add_merchant_start)],
        states={
            MERCH_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_merchant_name)],
            MERCH_STATUS: [CallbackQueryHandler(add_merchant_status_cb, pattern="^mnew_status:")],
            MERCH_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_merchant_rate)],
            MERCH_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_merchant_contact)],
            MERCH_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_merchant_notes)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            MessageHandler(filters.Text(["◀️ Вернуться в меню"]), cancel_conversation),
        ],
    ))

    # --- ConversationHandler: редактирование мерчанта ---
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Text([BTN_EDIT_MERCHANT]) & PRIVATE_ONLY, edit_merchant_start)],
        states={
            EDIT_FIND: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_merchant_find)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            MessageHandler(filters.Text(["◀️ Вернуться в меню"]), cancel_conversation),
        ],
    ))

    # --- ConversationHandler: создание инцидента ---
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Text([BTN_NEW_INCIDENT]) & PRIVATE_ONLY, inc_create_start)],
        states={
            INC_MERCHANT: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_merchant_step)],
            INC_TYPE: [CallbackQueryHandler(inc_type_cb, pattern="^inc_type:")],
            INC_PRIORITY: [CallbackQueryHandler(inc_priority_cb, pattern="^inc_prio:")],
            INC_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, inc_desc_step)],
            INC_DUE: [CallbackQueryHandler(inc_due_cb, pattern="^inc_due:")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            MessageHandler(filters.Text(["◀️ Вернуться в меню"]), cancel_conversation),
        ],
    ))

    # Загрузка файлов (экспорт чатов) ОТКЛЮЧЕНА: фича от Acme зависела от
    # отсутствующих модулей scheduler/analyzer и падала. Acme CRM — мерч-аналитика,
    # не чат-мониторинг. Вернуть — допортировать scheduler.py/analyzer.py.

    # --- Сообщения и кнопки ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & PRIVATE_ONLY, handle_message))
    # Кнопки рек-алертов — ПЕРЕД общим обработчиком (PTB берёт первый matching в группе)
    app.add_handler(CallbackQueryHandler(handle_req_callback, pattern="^rq:"))
    app.add_handler(CallbackQueryHandler(handle_su_callback, pattern="^su:"))
    app.add_handler(CallbackQueryHandler(handle_pfmk_callback, pattern="^pfmk:"))
    app.add_handler(CallbackQueryHandler(handle_vega_callback, pattern="^vega:"))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # --- Inline-режим (@bot query) ---
    app.add_handler(InlineQueryHandler(handle_inline_query))

    # --- Лог chat_id групп (чтобы узнать ID новой группы для DEADREQ_CHAT_ID) ---
    _seen_groups = set()

    async def log_group_chat(update, context):
        chat = update.effective_chat
        if chat and chat.id not in _seen_groups:
            _seen_groups.add(chat.id)
            logger.info(f"Группа обнаружена: chat_id={chat.id} title={chat.title!r}")
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, log_group_chat), group=1)

    # --- Глобальный error handler (сетевые блипы polling — warning, не трейсбек-шторм) ---
    async def on_error(update, context):
        err = context.error
        if isinstance(err, (NetworkError, TimedOut)):
            logger.warning(f"Сетевой блип Telegram: {err}")
        else:
            logger.error("Необработанная ошибка", exc_info=err)
    app.add_error_handler(on_error)

    # --- Фоновые задачи ---
    job_queue = app.job_queue

    # Мониторинг конверсии — окно 2ч, каждые 15 мин; OK-статус только при переходе «проблемы → чисто»
    job_queue.run_repeating(check_conversion, interval=900, first=60)
    logger.info(f"Мониторинг конверсии: каждые 15 мин, окно 2ч (порог {CONVERSION_THRESHOLD}%, FTD 5%)")

    # Моментальный детект резкого падения канала — каждые 5 минут
    job_queue.run_repeating(check_critical_drop, interval=300, first=120)
    logger.info("Крит-мониторинг: каждые 5 мин (резкое падение канала в моменте)")

    # Инфра-мониторинг (гейты/латентность/маршрутизация) — каждые 5 минут
    job_queue.run_repeating(check_infra, interval=300, first=150)
    logger.info("Инфра-мониторинг: каждые 5 мин (гейты, латентность, маршрутизация)")

    # Тренды (объём vs вчера, спайк отказов) — каждый час
    job_queue.run_repeating(check_trends, interval=3600, first=180)
    logger.info("Тренды: каждый час (падение объёма, спайк кода отказа)")

    # Heartbeat «приём встал» — каждые 5 минут
    job_queue.run_repeating(check_heartbeat, interval=300, first=210)
    logger.info("Heartbeat: каждые 5 мин (приём встал / 0 оплат)")

    # Остановка трафика (мерч/эндпоинт лил и встал) — каждые 10 минут
    job_queue.run_repeating(check_traffic_stop, interval=600, first=240)
    logger.info("Остановка трафика: каждые 10 мин (мерч/эндпоинт лил и встал)")

    # NSPK VIP milestones: каждые +100к ₽ сумма успехов + спец-алерт около цели (700к) — раз в минуту
    job_queue.run_repeating(check_nspk_vip_milestones, interval=60, first=30)

    # бизнес-риски: SPOF / деньги в подвисе / дыра маршрутизации (раз в 30 мин)
    job_queue.run_repeating(check_business_risks, interval=1800, first=270)
    logger.info("NSPK VIP: каждые +100к ₽ суммы успехов + спец-алерт ~700к (опрос 60с)")

    # Финансовые риски: нулевая ставка / отрицательная маржа / порог объёма Echo МК (раз в 5 мин)
    job_queue.run_repeating(check_finance_risks, interval=300, first=120)
    logger.info("Финансовые риски: каждые 5 мин (нулевая ставка, отрицательная маржа, порог Echo МК)")

    # Реквизиты со стриком 7+ отмен подряд за сутки — каждые 30 мин, оповещаем сразу
    job_queue.run_repeating(check_dead_requisites, interval=1800, first=420)
    logger.info("Мёртвые реквизиты: каждые 30 мин (стрик 7+ отмен подряд за сутки)")

    # Выплаты: зависшие и серии reject'ов — каждые 30 мин
    job_queue.run_repeating(check_withdrawals, interval=1800, first=330)
    logger.info("Выплаты: каждые 30 мин (зависшие >2ч, серии reject)")

    # Гейт ниже своей 7-дневной нормы — каждые 15 мин
    job_queue.run_repeating(check_gate_relative, interval=900, first=360)
    logger.info("Гейты относительно нормы: каждые 15 мин (просадка 25+ пп от 7д)")

    # Сводка-светофор — каждые 3 часа
    job_queue.run_repeating(send_traffic_light, interval=10800, first=900)
    logger.info("Светофор: каждые 3 часа (гейты + топ-мерчи vs вчера)")

    # Новые эндпоинты: первые 48ч под жёстким присмотром — каждые 15 мин
    job_queue.run_repeating(check_new_endpoints, interval=900, first=480)
    logger.info("Новые эндпоинты: каждые 15 мин (первые 48ч жизни)")

    # Юзеры-плохиши: 5+ заявок за час без оплат — каждые 15 мин
    job_queue.run_repeating(check_spam_users, interval=900, first=540)
    logger.info("Юзеры-плохиши: каждые 15 мин (5+ заявок/час, 0 оплат)")

    # Тест от мерча — ловим сразу, каждые 2 мин
    job_queue.run_repeating(check_test_traffic, interval=120, first=90)
    logger.info("Тест от мерча: каждые 2 мин (реальные мерчи, наши прогоны исключены)")

    # Недельный тренд-отчёт по мерчам — понедельник 10:00 МСК (07:00 UTC)
    job_queue.run_daily(send_weekly_trends, time=datetime.strptime("07:00", "%H:%M").time(), days=(0,))
    logger.info("Недельные тренды: понедельник 10:00 МСК")

    # Утренний дайджест 09:00 МСК — ОТКЛЮЧЁН (не нужен в группе)
    # job_queue.run_daily(send_daily_digest, time=datetime.strptime("06:00", "%H:%M").time())

    # Дейли-отчёт (позавчера→вчера, по валютам) в 00:00 МСК — ОТКЛЮЧЁН
    # (дейли-сравнение день-к-дню не нужно в группе)
    # job_queue.run_daily(send_daily_report, time=datetime.strptime("21:00", "%H:%M").time())
    # logger.info("Дейли-отчёт: ежедневно в 00:00 МСК")

    # Сохранение конверсии за день в 23:50 МСК (20:50 UTC)
    job_queue.run_daily(save_daily_conversion_job, time=datetime.strptime("20:50", "%H:%M").time())
    logger.info("Сохранение конверсии: ежедневно в 23:50 МСК")

    # Проверка просроченных инцидентов каждые 30 мин
    job_queue.run_repeating(check_overdue_incidents_job, interval=1800, first=120)
    logger.info("Проверка дедлайнов инцидентов: каждые 30 мин")

    # Очистка старых данных в 03:00 МСК (00:00 UTC)
    job_queue.run_daily(cleanup_old_data_job, time=datetime.strptime("00:00", "%H:%M").time())
    logger.info("Очистка старых данных: ежедневно в 03:00 МСК")

    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
