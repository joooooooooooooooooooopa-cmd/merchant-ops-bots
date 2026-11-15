"""
Scheduled jobs & low-conversion alerts.

Extracted from bot.py — constants, helpers and async job functions.
"""

import asyncio
import logging
import os
import json
from datetime import datetime, timezone, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import RetryAfter
from telegram.ext import ContextTypes

import metabase as mb
import database
import storage
import keys
import visualization
from config import ADMIN_IDS, BOT_TOKEN, ALERT_CHAT_ID

from handlers.utils import escape, format_money, format_number, MSK, is_admin, progress_bar
from handlers.formatters import format_today, format_conversion_detailed
from handlers.keyboards import kb_main, kb_alert

logger = logging.getLogger(__name__)

# ============ CONSTANTS ============

CONVERSION_THRESHOLD = 35
# FTD (первичка) по природе ниже STD — отдельный, более низкий порог: алертим только при реальном провале
FTD_THRESHOLD = 5
# Минимум заявок С РЕКВИЗИТАМИ для алерта (чтобы не спамить по мелким мерчам)
MIN_ORDERS_WITH_REQ_FOR_ALERT = 10
# Минимум реквизитов ПО МЕТОДУ для алерта по методу
MIN_METHOD_REQ_FOR_ALERT = 15
# Мерчанты, исключённые из алертов о низкой конверсии (вкл. тестовые — у них is_test=false в данных)
EXCLUDED_MERCHANTS = {"Merchant Lab", "Merchant Sandbox", "internal-test", "BDM_Test"}


def _is_excluded_merchant(name):
    """Исключаем из алертов: явный список + любые тестовые мерчанты (test в имени)."""
    if not name:
        return True
    if name in EXCLUDED_MERCHANTS:
        return True
    return "test" in name.lower()
# Интервал проверки (секунды) — 1 час
CHECK_INTERVAL = 3600

TREND_ICON = {'declining': '📉', 'improving': '📈', 'stable': ''}

# ============ HELPERS ============


def get_greeting_by_time():
    """Возвращает приветствие в зависимости от времени суток"""
    hour = datetime.now(MSK).hour

    if 6 <= hour < 12:
        return "Доброе утро"
    elif 12 <= hour < 18:
        return "Добрый день"
    elif 18 <= hour < 23:
        return "Добрый вечер"
    else:
        return "Доброго времени суток"


def get_merchants_status_map():
    """Возвращает словарь {имя_мерчанта: статус} из локальной БД"""
    try:
        merchants = database.get_all_merchants()
        return {m['name']: m.get('status', 'unknown') for m in merchants if m.get('name')}
    except Exception as e:
        logger.warning(f"Не удалось загрузить статусы мерчантов из БД: {e}")
        return {}


def get_template_variant(conv_level):
    """
    Возвращает вариант шаблона в зависимости от дня месяца
    conv_level: 'critical' (<10%), 'low' (10-20%), или 'warning' (20% — порог)
    """
    day = datetime.now(MSK).day

    critical_templates = [
        (
            "🔴 <b>КРИТИЧЕСКИЙ УРОВЕНЬ КОНВЕРСИИ</b>\n"
            "При текущих показателях конверсии продолжение сотрудничества невозможно.\n"
            "Просим в срочном порядке проанализировать клиентов на предмет спама, мошенничества и иных факторов по неоплаченным заявкам ниже."
        ),
        (
            "🔴 <b>КРИТИЧЕСКАЯ СИТУАЦИЯ</b>\n"
            "Конверсия находится на критически низком уровне. Работа на таких условиях невозможна.\n"
            "Необходимо срочно выявить причины: проверьте клиентов на мошенничество, спам и другие факторы по примерам ниже."
        ),
        (
            "🔴 <b>КРИТИЧЕСКИЙ УРОВЕНЬ</b>\n"
            "Конверсия ниже минимально допустимого порога для продолжения сотрудничества.\n"
            "Просим немедленно провести анализ неоплаченных заявок ниже на предмет недобросовестных клиентов и технических проблем."
        ),
        (
            "🔴 <b>КРИТИЧЕСКАЯ КОНВЕРСИЯ</b>\n"
            "Текущий уровень конверсии делает невозможным дальнейшее сотрудничество.\n"
            "Требуется экстренная проверка качества трафика и клиентов по неоплаченным заявкам ниже."
        ),
    ]

    low_templates = [
        (
            "⚠️ <b>НИЗКАЯ КОНВЕРСИЯ</b>\n"
            "Предоставленные нами реквизиты не конвертируются в оплату. Ниже примеры таких заявок.\n"
            "Просим проанализировать клиентов на предмет спама, мошенничества и иных факторов, снижающих конверсию."
        ),
        (
            "⚠️ <b>НИЗКИЙ УРОВЕНЬ ОПЛАТ</b>\n"
            "Выданные реквизиты не приводят к оплатам. Примеры проблемных заявок ниже.\n"
            "Рекомендуем проверить качество трафика, исключить спам-заявки и мошеннические попытки."
        ),
        (
            "⚠️ <b>КОНВЕРСИЯ НИЖЕ НОРМЫ</b>\n"
            "Большинство клиентов получают реквизиты, но не совершают оплату. Ниже примеры.\n"
            "Просим оценить качество клиентской базы, выявить спам и проблемы на стороне приема платежей."
        ),
        (
            "⚠️ <b>НИЗКАЯ ОПЛАЧИВАЕМОСТЬ</b>\n"
            "Реквизиты выдаются, но конверсия в оплату значительно ниже нормы. Примеры ниже.\n"
            "Необходимо проверить клиентов на добросовестность и исключить технические проблемы с оплатой."
        ),
    ]

    warning_templates = [
        (
            "💛 <b>ЗОНА ВНИМАНИЯ</b>\n"
            "Конверсия немного ниже целевого уровня. Уверены, что сможете улучшить показатели!\n"
            "Проверьте примеры ниже — возможно, есть точки роста в обработке клиентов. Сможем показать лучшие результаты вместе."
        ),
        (
            "💛 <b>НЕБОЛЬШОЕ СНИЖЕНИЕ</b>\n"
            "Показатели чуть ниже обычного, но это поправимо. Вместе сможем улучшить результаты!\n"
            "Посмотрите примеры неоплат ниже — возможно, найдёте способы повысить качество обработки заявок."
        ),
        (
            "💛 <b>ЕСТЬ ПОТЕНЦИАЛ</b>\n"
            "Конверсия близка к норме, но можно лучше! Уверены в ваших возможностях.\n"
            "Ниже примеры заявок — проанализируйте их, и сможем вместе показать отличные результаты."
        ),
        (
            "💛 <b>МОЖНО УЛУЧШИТЬ</b>\n"
            "Показатели немного просели, но вы точно справитесь! Есть все возможности для роста.\n"
            "Изучите примеры ниже — оптимизация обработки поможет показать лучшую конверсию."
        ),
    ]

    if conv_level == 'critical':
        variant_index = day % len(critical_templates)
        return critical_templates[variant_index]
    elif conv_level == 'low':
        variant_index = day % len(low_templates)
        return low_templates[variant_index]
    else:  # warning
        variant_index = day % len(warning_templates)
        return warning_templates[variant_index]


def format_low_conversion_alert(merchants, unpaid_orders):
    """Форматирует алерт о мерчантах с низкой конверсией"""
    low_conv_merchants = []

    # Получаем статусы мерчантов из локальной БД
    merchant_statuses = get_merchants_status_map()

    for m in merchants:
        name = m.get("Мерчант") or ""
        if "ИТОГО" in name:
            continue

        # Пропускаем отключенных мерчантов
        if merchant_statuses.get(name) == 'disconnected':
            continue

        total = m.get("Всего") or 0
        with_req = m.get("С реквизитами") or 0
        conv = m.get("Конверсия в оплату %") or 0

        # Проверяем порог (минимум заявок с реквизитами)
        if with_req >= MIN_ORDERS_WITH_REQ_FOR_ALERT and conv < CONVERSION_THRESHOLD:
            low_conv_merchants.append({
                "name": name,
                "total": total,
                "with_req": with_req,
                "conv": conv,
                "successful": m.get("Успешных") or 0,
            })

    if not low_conv_merchants:
        return f"✅ Все мерчанты имеют нормальную конверсию (≥{CONVERSION_THRESHOLD}%)"

    # Получаем приветствие по времени суток
    greeting = get_greeting_by_time()

    lines = [
        "🚨 <b>АЛЕРТ: НИЗКАЯ КОНВЕРСИЯ</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    for m in low_conv_merchants[:10]:
        conv = m['conv']

        # Выбираем emoji и шаблон в зависимости от конверсии
        if conv < 10:
            emoji = "🔴"
            template_text = get_template_variant('critical')
            template = f"Коллеги, {greeting}!\n\n{template_text}"
        elif conv < 20:  # 10-20%
            emoji = "⚠️"
            template_text = get_template_variant('low')
            template = f"Коллеги, {greeting}!\n\n{template_text}"
        else:  # 20% — порог
            emoji = "💛"
            template_text = get_template_variant('warning')
            template = f"Коллеги, {greeting}!\n\n{template_text}"

        lines.append(f"{emoji} <b>{escape(m['name'])}</b>")
        lines.append(f"   Конверсия в оплату: <code>{conv:.1f}%</code> (порог {CONVERSION_THRESHOLD}%)")
        lines.append(f"   С реквизитами: {m['with_req']} → Оплатили: {m['successful']}")
        lines.append("")
        lines.append(f"   {template}")
        lines.append("")

        # Добавляем примеры неоплаченных заявок
        merchant_unpaid = unpaid_orders.get(m['name'], [])
        if merchant_unpaid and isinstance(merchant_unpaid, list):
            lines.append("   📋 <b>Примеры неоплаченных заявок:</b>")
            for item in merchant_unpaid[:5]:  # Максимум 5 примеров
                if isinstance(item, dict):
                    ext = item.get('order_id', '?')
                    method = item.get('method', '')
                    method_str = f" · {method}" if method else ""
                    lines.append(f"   • <code>{ext}</code>{method_str}")
                else:
                    lines.append(f"   • <code>{item}</code>")
            lines.append("")

    lines.append(f"⏰ {datetime.now(MSK).strftime('%H:%M:%S')}")

    return "\n".join(lines)


def analyze_alert_patterns(merchant_name: str, current_conv: float) -> list:
    """Анализирует паттерны алертов и возвращает список бейджей"""
    badges = []
    try:
        if database.get_alert_count_today(merchant_name) >= 3:
            badges.append("🔥 Постоянная проблема")
        avg = database.get_avg_conversion_last_week(merchant_name)
        if avg and avg > 0 and current_conv < avg * 0.7:
            badges.append("⚠️ Резкое падение")
        if database.is_new_problem(merchant_name):
            badges.append("🆕 Новая проблема")
    except Exception:
        pass
    return badges


# ============ ASYNC JOBS ============


async def send_daily_digest(context):
    """Утренний дайджест (09:00 МСК): вчера конверсия/оборот, тренд vs позавчера, сегодня, топ-проблемы."""
    try:
        y = mb.get_day_overview(1)
        db = mb.get_day_overview(2)
        td = mb.get_day_overview(0)
        if db["conv"]:
            d = round(y["conv"] - db["conv"], 1)
            trend = f"{'📈 +' if d >= 0 else '📉 '}{d} п.п. vs позавчера ({db['conv']:.1f}%)"
        else:
            trend = "нет данных за позавчера"

        def _m(a):
            return f"{int(a):,}".replace(",", " ") + "₽"

        def _amounts(day_offset, fallback):
            """Оборот по валютам раздельно (RUB+TRY одним числом — некорректно)."""
            try:
                rows = mb.get_day_amounts_by_cur(day_offset)
                if rows:
                    return " + ".join(
                        f"{_fmt_rub(float(r['amount']))} {_CUR_SYM.get((r['cur'] or 'RUB').upper(), r['cur'])}"
                        for r in rows)
            except Exception:
                pass
            return _m(fallback)

        lines = [
            f"📅 <b>Утренний дайджест — {datetime.now(MSK).strftime('%d.%m.%Y')}</b>",
            "",
            f"<b>Вчера:</b> {y['total']} заявок · выдача {y['issued']} · опл {y['success']}",
            f"   📈 Конверсия: <b>{y['conv']:.1f}%</b>   ·   💰 Оборот: <b>{_amounts(1, y['amount'])}</b>",
            f"   {trend}",
            "",
            f"<b>Сегодня (с 00:00):</b> {td['total']} заявок · конв {td['conv']:.1f}% · {_amounts(0, td['amount'])}",
        ]
        try:
            ydata = mb.get_yesterday_by_merchant_endpoint()
            statuses = get_merchants_status_map()
            worst = []
            for m in ydata:
                if statuses.get(m) == 'disconnected' or _is_excluded_merchant(m):
                    continue
                for ck, node in _aggregate_channels(ydata[m]).items():
                    if node["with_req"] >= 20 and node["conv"] < CONVERSION_THRESHOLD:
                        worst.append((m, ck, node["conv"], node["successful"], node["with_req"]))
            worst.sort(key=lambda x: x[2])
        except Exception:
            worst = []
        if worst:
            lines += ["", "🔴 <b>Худшие каналы вчера:</b>"]
            for m, ck, cv, su, wr in worst[:5]:
                lines.append(f"   • {escape(m)} · <code>{escape(ck)}</code>: {cv:.1f}% ({su}/{wr})")
        else:
            lines += ["", "✅ Вчера каналов ниже порога не было."]

        lines += ["", f"⏰ {datetime.now(MSK).strftime('%H:%M')} МСК"]
        await _broadcast(context, "\n".join(lines))
        logger.info("Утренний дайджест отправлен")
    except Exception as e:
        logger.error(f"Ошибка дайджеста: {e}")


async def send_daily_report(context):
    """Дейли-отчёт (00:00 МСК): позавчера → вчера, раздельно по валютам/гео, без прибыли."""
    try:
        import daily_report
        text = daily_report.build_default()
        await _broadcast(context, text)
        logger.info("Дейли-отчёт отправлен")
    except Exception as e:
        logger.error(f"Ошибка дейли-отчёта: {e}")


async def save_daily_conversion_job(context):
    """Сохраняем конверсию за день в 23:50 МСК"""
    try:
        conv_data = mb.get_conversion_today()
        today = datetime.now(MSK).strftime('%Y-%m-%d')
        for m in conv_data:
            name = m.get("Мерчант") or ""
            if "ИТОГО" in name or not name:
                continue
            # привести к нативным типам: psycopg2 отдаёт ROUND как Decimal, а SQLite его не биндит
            conv = float(m.get("Конверсия в оплату %") or 0)
            total = int(m.get("Всего") or 0)
            success = int(m.get("Успешных") or 0)
            database.save_daily_conversion(name, today, conv, total, success)
        logger.info(f"Конверсия за {today} сохранена для {len(conv_data)} мерчантов")
    except Exception as e:
        logger.error(f"Ошибка сохранения конверсии: {e}")


async def check_overdue_incidents_job(context):
    """Проверка просроченных инцидентов каждые 30 минут.
    Дедуп: один инцидент пингует не чаще раза в 4 часа (раньше — каждые 30 мин бесконечно)."""
    try:
        overdue = database.get_overdue_incidents()
        if not overdue:
            return
        state = _load_state()
        overdue = [inc for inc in overdue
                   if _register(state, f"overdue_{inc.get('id')}", f"#{inc.get('id')}", hours=4)]
        _save_state(state)
        if not overdue:
            return
        lines = ["⏰ <b>Просроченные инциденты!</b>", ""]
        for inc in overdue[:5]:
            pe = {'P0': '🔴', 'P1': '🟠', 'P2': '🟡', 'P3': '⚪'}.get(inc.get('priority', 'P2'), '⚪')
            lines.append(f"{pe} #{inc['id']} {escape(inc.get('merchant_name', ''))} — {inc.get('incident_type', '')}")
            assigned = inc.get('assigned_user_name') or 'Не назначен'
            lines.append(f"   Назначен: {escape(assigned)}")
        text = "\n".join(lines)
        for uid in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Ошибка проверки дедлайнов: {e}")


async def cleanup_old_data_job(context):
    """Очистка старых данных в 03:00 МСК"""
    try:
        database.cleanup_old_data()
        storage.get_storage().cleanup_old_alerts(days=7)
        logger.info("Очистка старых данных выполнена")
    except Exception as e:
        logger.error(f"Ошибка очистки: {e}")


def _channel_key_tier(endpoint_name):
    """Канал = имя продукта с FTD/STD, схлопнутым в '*'. Возвращает (channel_key, tier)."""
    name = endpoint_name or "—"
    tier = "—"
    out = []
    for t in name.split("_"):
        if t == "FTD":
            tier = "FTD"; out.append("*")
        elif t == "STD":
            tier = "STD"; out.append("*")
        else:
            out.append(t)
    return "_".join(out), tier


_TIER_LABEL = {"FTD": "первичка (FTD)", "STD": "вторичка (STD)", "—": "прочее"}


def _tier_note(tiers):
    """Пометка первичка/вторичка для канала со схлопнутым '*':
    чтобы по 'Trans_AM_*_In' было сразу понятно, какой тир в проблеме."""
    ts = {t.get("tier") for t in (tiers or []) if (t.get("with_req") or 0) > 0}
    f, s = "FTD" in ts, "STD" in ts
    if f and s:
        return "первичка+вторичка"
    if f:
        return "первичка"
    if s:
        return "вторичка"
    return ""

# ── окна/пороги ──
CONV_WINDOW_MIN = 120
CONV_MIN_ISSUED = 5
# Для ЭНДПОИНТ-алертов порог выше: окно из 5 выдач даёт шумные «0/5» даже при здоровой
# конверсии (кейс Charlie_TRY_P2P_HAVALE_FTD)
CONV_MIN_ISSUED_EP = 10
RECOVERY_MIN_ISSUED = 10   # «восстановилось» только при подтверждённом объёме (не 1/1=100%)
HEARTBEAT_WINDOW = 20          # окно heartbeat (мин)
HEARTBEAT_MIN_EXPECTED = 10    # вчера в это окно было ≥ столько → «0 заявок сейчас» = аномалия
HEARTBEAT_MIN_FLOW = 15        # ≥ столько выдач при 0 оплат = приём лёг
THROTTLE_HOURS = 3            # повтор-напоминание не чаще раза в 3ч
# ── норма эндпоинта (доп. детект сверх фикс-порога) ──
BASELINE_DAYS = 7            # окно базовой конверсии канала
BASELINE_DROP_PP = 15        # просадка от своей нормы на ≥ столько п.п. → алерт (даже если выше фикс-порога)
BASELINE_MIN_REQ = 50        # минимум выдач за 7д, чтобы норма была достоверной
# ── эскалация залипших проблем ──
ESCALATE_HOURS = 4           # проблема открыта дольше → в алерт добавляется призыв эскалировать
# ── бизнес-риски (check_business_risks, раз в 30 мин) ──
SPOF_HOURS = 24              # окно концентрации приёма
SPOF_TOP_SHARE = 80          # доля топ-гейта ≥ столько % → single-point-of-failure
SPOF_MIN_OK = 100            # минимум успехов за окно, чтобы доля была значимой
SPOF_THROTTLE_H = 12         # напоминание о концентрации не чаще раза в 12ч
LIMBO_WINDOW_MIN = 180       # окно «деньги в подвисе»
LIMBO_RUB = 300_000          # выдан-не-оплачен ≥ столько ₽ на эндпоинт за окно → алерт
# Пороги по валютам (~эквивалент 300к ₽): суммировать TRY/UZS с RUB одним числом нельзя
LIMBO_THRESHOLD = {"RUB": LIMBO_RUB, "TRY": 150_000, "KZT": 1_800_000, "UZS": 45_000_000,
                   "TJS": 40_000, "AZN": 6_000, "USD": 3_500, "EUR": 3_200, "AMD": 1_400_000}
_CUR_SYM = {"RUB": "₽", "TRY": "₺", "KZT": "₸", "UZS": "soʻm", "TJS": "SM",
            "AZN": "₼", "USD": "$", "EUR": "€", "AMD": "֏"}
LIMBO_THROTTLE_H = 6
ROUTE_HOLE_WINDOW_MIN = 120  # окно хронической дыры маршрутизации (НЕ 20-мин спайк check_infra)
ROUTE_HOLE_EP_MIN = 20       # ≥ столько «застряло до гейта» на ОДНОМ эндпоинте за окно → алерт
ROUTE_HOLE_THROTTLE_H = 3
# ── просадка конверсии ПО МЕРЧУ (D2: «AM бьёт тревогу первым») ──
MCONV_WINDOW_MIN = 120       # окно текущей конверсии мерча
MCONV_DROP_PP = 12           # падение конверсии (success/total) от 7-дн нормы на ≥ столько п.п.
MCONV_MIN_TOTAL = 40         # минимум заявок мерча за окно
MCONV_BASE_MIN_TOTAL = 300   # минимум заявок за 7д, чтобы норма была достоверной
MCONV_THROTTLE_H = 3
# ── D7: callback-фейлы и зависшие pending ──
CALLBACK_WINDOW_MIN = 30     # окно проверки доставки callback
CALLBACK_FAIL_MIN = 5        # ≥ столько не-2xx callback'ов мерчу за окно → алерт
CALLBACK_THROTTLE_H = 3
PENDING_WINDOW_MIN = 120     # окно набора зависших
PENDING_OLDER_MIN = 15       # «зависла», если в pending/new дольше столько минут
PENDING_MIN = 25            # ≥ столько зависших на мерче×эндпоинте → алерт
PENDING_THROTTLE_H = 3
CRITICAL_WINDOW_MIN = 20
CRITICAL_MIN_ISSUED = 8
CRITICAL_CONV = 10
GATE_COLLAPSE_MIN_ISSUED = 10
GATE_COLLAPSE_CONV = 10
GATE_LATENCY_MIN_CALLS = 15
# Латентность гейта триггерим по МЕДИАНЕ (как мерит саппорт), а не по p95: у нашего round-trip
# gc.duration постоянный хвост ~5% медленных вызовов задирает p95 к 5с даже когда гейт здоров
# (медиана ~0.5–1с, ошибок 0) — это давало ложные «Гейт тормозит».
GATE_MEDIAN_SEC = 3          # медиана времени ответа > 3с = реально медленный гейт (весь поток, не хвост)
GATE_P95_SEC = 5             # p95 теперь только справочно в тексте (не триггер)
GATE_FAILED_PCT = 50
LAT_MIN_SUCC = 8             # минимум успехов для оценки скорости подтверждения
LAT_RATIO = 2.5             # свежая медиана >= 2.5x базовой = деградация
LAT_MIN_SEC = 120           # и не меньше 120с (отсечь мелочь)
ROUTING_SPIKE = 25
INFRA_WINDOW_MIN = 20
# Эндпоинты, убранные из алерта «Нет маршрута / ROUTING_ERROR».
# Echo MK ВЕРНУЛИ в маршрут-алерты — оставлен только спам-юзер.
ROUTING_EXCLUDE_ENDPOINTS = set()
# Подстрока для исключения Echo MK ТОЛЬКО из алерта «Юзер-плохиш»:
# по MC-методу клиенты массово не платят by-design, помечать их спамерами — шум. Остальные алерты работают.
ALERT_EXCLUDE_SUBSTR = ("echo_rub_p2p_mc",)


def _is_excluded_alert_target(name):
    """True для эндпоинтов, исключённых из спам-юзер-алерта (Echo MK)."""
    s = (name or "").lower()
    return any(x in s for x in ALERT_EXCLUDE_SUBSTR)


VOLUME_DROP_RATIO = 0.5
VOLUME_MIN_YEST = 30
DECLINE_SPIKE_MIN = 50
DECLINE_SPIKE_SHARE = 40
DECLINE_IGNORE = {"(нет кода)", "CANCELLED", "SESSION_EXPIRED", "COMMON", "COMMON_ERROR",
                  "validation_error", "ROUTING_ERROR"}
# ── Алерт «мерч/эндпоинт лил и остановил трафик» ──
STOP_WINDOW_MIN = 60           # окно тишины для STD-эндпоинтов (вторичка); 30→60
STOP_WINDOW_FTD_MIN = 180      # окно тишины для FTD (первичка): естественно реже → 30-мин гэп = шум, ждём 3ч
STOP_BASE_MIN = 1440           # базлайн = сутки (был 2.5ч): мерч/эндпоинт «лил», если был трафик за последние 24ч
STOP_MERCHANT_BASE_MIN = 20    # мин. заявок мерча в базлайне за сутки, чтобы считать «лившим»
STOP_ENDPOINT_BASE_MIN = 12    # то же для отдельного эндпоинта
STOP_REMIND_HOURS = 3          # пока трафика нет — напоминание не чаще, чем раз в N часов
STOP_REMIND_CAP_HOURS = 24     # после 24ч простоя напоминания НЕ слать (без Romeo-style спама на 58+ч); ✅ при возврате трафика всё равно придёт
# Анти-пинг-понг: при базе 20 заявок/24ч (≈1 в 72 мин) 30-мин тишина — норма, а не остановка.
# Требуем матожидание ≥ N заявок в окне тишины (по 24ч-рейту) + кулдаун после «возобновился».
STOP_MIN_EXPECTED = 2.5        # ожидаемых заявок в окне тишины, ниже — не детектим остановку
STOP_RESUME_COOLDOWN_MIN = 60  # после «✅ возобновился» новый детект той же пары не раньше, чем через час


def _is_ftd_ep(name):
    """Первичный эндпоинт (FTD) — тишину считаем 3ч, а не 30 мин."""
    s = (name or "").lower()
    return "_ftd_" in s or s.endswith("_ftd_in") or s.endswith("_ftd_out")
_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "alert_state.json")

# Единый in-memory state. Раньше каждая джоба делала load → (await-ы рассылки) → save ЦЕЛИКОМ,
# и параллельная джоба, сохранившаяся в этом окне, теряла свои ключи (= дубли алертов).
# Все джобы живут в одном event loop, поэтому общий dict закрывает гонку без локов.
_STATE_CACHE = None


def _gc_state(d):
    """Чистка при загрузке: nspk-ключи прошлых суток + записи без обновлений >14 дней."""
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    cutoff = datetime.now().timestamp() - 14 * 86400
    for k in list(d.keys()):
        v = d.get(k)
        if k.startswith("nspk_vip_") and not k.endswith(today):
            del d[k]
        elif isinstance(v, dict):
            ts = max(v.get("last") or 0, v.get("since") or 0, v.get("t") or 0)
            if ts and ts < cutoff:
                del d[k]


def _load_state():
    global _STATE_CACHE
    if _STATE_CACHE is None:
        try:
            with open(_STATE_PATH, encoding="utf-8") as f:
                _STATE_CACHE = json.load(f)
        except Exception:
            _STATE_CACHE = {}
        _gc_state(_STATE_CACHE)
    return _STATE_CACHE


def _save_state(d=None):
    """Атомарная запись (tmp + os.replace): обрыв на середине не оставит битый JSON
    (битый JSON = пустой state на старте = повторный шторм всех открытых алертов)."""
    data = _STATE_CACHE if _STATE_CACHE is not None else (d or {})
    try:
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _STATE_PATH)
    except Exception as e:
        logger.warning(f"state save failed: {e}")


def _register(state, key, label, hours=THROTTLE_HOURS):
    """Регистрирует активную проблему. True = надо слать (новая или пора напомнить)."""
    now = datetime.now().timestamp()
    rec = state.get(key)
    if rec is None:
        state[key] = {"label": label, "since": now, "last": now}
        return True
    if now - (rec.get("last") or 0) >= hours * 3600:
        rec["last"] = now
        rec["label"] = label
        return True
    return False


def _dur(since):
    mins = int(max(0, datetime.now().timestamp() - (since or 0)) / 60)
    return f"{mins // 60}ч {mins % 60}м" if mins >= 60 else f"{mins}м"


def _window_metrics(window_min=CONV_WINDOW_MIN):
    """Метрики ВСЕХ каналов/эндпоинтов за окно: {ключ: {conv, with_req, thr}} — для проверки восстановления."""
    data = mb.get_recent_by_merchant_endpoint(window_min)
    statuses = get_merchants_status_map()
    out = {}
    for m in data:
        if statuses.get(m) == 'disconnected' or _is_excluded_merchant(m):
            continue
        for ck, node in _aggregate_channels(data[m]).items():
            out[f"conv_{m}_{ck}"] = {"conv": node["conv"], "with_req": node["with_req"], "thr": _channel_threshold(node)}
        for ep_name, ep in data[m].items():
            _, tier = _channel_key_tier(ep_name)
            wr = ep.get("with_req", 0) or 0
            su = ep.get("successful", 0) or 0
            out[f"conv_{m}_{ep_name}"] = {"conv": round(su * 100.0 / wr, 1) if wr else 0, "with_req": wr,
                                         "thr": FTD_THRESHOLD if tier == "FTD" else CONVERSION_THRESHOLD}
    return out


def _reconcile_recovered(state, prefix, current_keys):
    """Проблемы с этим префиксом, которых больше нет в current_keys → восстановились."""
    rec = []
    for k in list(state.keys()):
        if k.startswith(prefix) and k not in current_keys:
            rec.append((state[k].get("label", k), state[k].get("since")))
            del state[k]
    return rec


def _channel_threshold(node):
    """Порог канала с учётом природы трафика: если канал фактически целиком FTD (первичка),
    сравниваем с FTD-порогом — иначе чисто-FTD канал со здоровой конверсией 10-20%
    вечно алертил бы по STD-порогу 35%."""
    wr = node.get("with_req", 0) or 0
    ftd_wr = sum((t.get("with_req") or 0) for t in node.get("tiers", []) if t.get("tier") == "FTD")
    return FTD_THRESHOLD if wr and ftd_wr >= 0.9 * wr else CONVERSION_THRESHOLD


def _aggregate_channels(endpoints):
    channels = {}
    for ep_name, ep in endpoints.items():
        ck, tier = _channel_key_tier(ep_name)
        node = channels.setdefault(ck, {"with_req": 0, "successful": 0, "total": 0, "tiers": [], "methods_acc": {}})
        wr = ep.get("with_req", 0) or 0
        su = ep.get("successful", 0) or 0
        node["with_req"] += wr
        node["successful"] += su
        node["total"] += ep.get("total", 0) or 0
        node["tiers"].append({"tier": tier, "endpoint": ep_name, "with_req": wr, "successful": su,
                              "conv": round(su * 100.0 / wr, 1) if wr else 0})
        for ms in ep.get("methods", []):
            acc = node["methods_acc"].setdefault(ms.get("method") or "не определён", {"with_req": 0, "successful": 0})
            acc["with_req"] += ms.get("with_req", 0) or 0
            acc["successful"] += ms.get("successful", 0) or 0
    for node in channels.values():
        node["conv"] = round(node["successful"] * 100.0 / node["with_req"], 1) if node["with_req"] else 0
        node["methods"] = [{"method": m, "with_req": v["with_req"], "successful": v["successful"],
                            "conv_pct": round(v["successful"] * 100.0 / v["with_req"], 1) if v["with_req"] else 0}
                           for m, v in node["methods_acc"].items()]
        node["tiers"].sort(key=lambda t: t["with_req"], reverse=True)
    return channels


def collect_problems(window_min=CONV_WINDOW_MIN, min_issued=CONV_MIN_ISSUED, baselines=None):
    data = mb.get_recent_by_merchant_endpoint(window_min)
    statuses = get_merchants_status_map()
    problems = []
    for m in sorted(data.keys()):
        if statuses.get(m) == 'disconnected' or _is_excluded_merchant(m):
            continue
        channels = _aggregate_channels(data[m])
        alerting = set()
        for ck, node in channels.items():
            if node["with_req"] < min_issued:
                continue
            base = (baselines or {}).get(f"{m}|{ck}")
            base_ok = bool(base and base["with_req"] >= BASELINE_MIN_REQ)
            thr = _channel_threshold(node)                                  # FTD-канал → FTD-порог
            below_abs = node["conv"] < thr
            below_base = base_ok and (base["conv"] - node["conv"]) >= BASELINE_DROP_PP  # просадка от нормы
            if below_abs or below_base:
                alerting.add(ck)
                problems.append({"kind": "channel", "merchant": m, "channel": ck,
                                 "key": f"conv_{m}_{ck}", "with_req": node["with_req"],
                                 "successful": node["successful"], "conv": node["conv"],
                                 "threshold": thr,
                                 "tiers": node["tiers"], "methods": node["methods"],
                                 "baseline": (base["conv"] if base_ok else None)})
        for ep_name, ep in data[m].items():
            ck, tier = _channel_key_tier(ep_name)
            if ck in alerting:
                continue
            wr = ep.get("with_req", 0) or 0
            su = ep.get("successful", 0) or 0
            conv = round(su * 100.0 / wr, 1) if wr else 0
            thr = FTD_THRESHOLD if tier == "FTD" else CONVERSION_THRESHOLD
            if wr >= max(min_issued, CONV_MIN_ISSUED_EP) and conv < thr:
                ch = channels.get(ck, {})
                problems.append({"kind": "endpoint", "merchant": m, "endpoint": ep_name, "tier": tier,
                                 "channel": ck, "key": f"conv_{m}_{ep_name}", "with_req": wr,
                                 "successful": su, "conv": conv, "threshold": thr,
                                 "channel_conv": ch.get("conv", conv), "channel_with_req": ch.get("with_req", wr),
                                 "methods": ep.get("methods", [])})
    problems.sort(key=lambda p: (p["conv"], -p["with_req"]))
    return problems


def _emoji_template(conv):
    if conv < 10:
        return "🔴", get_template_variant('critical')
    if conv < 20:
        return "⚠️", get_template_variant('low')
    return "💛", get_template_variant('warning')


def _icon(c):
    return "🟢" if c >= 40 else ("🟡" if c >= CONVERSION_THRESHOLD else ("🟠" if c >= 10 else "🔴"))


def _methods_block(methods):
    out = []
    mwr = [ms for ms in methods if (ms.get('with_req', 0) or 0) > 0]
    if mwr:
        out.append("")
        out.append("💳 <b>По методам:</b>")
        for ms in sorted(mwr, key=lambda x: x.get('with_req', 0), reverse=True):
            mc = ms.get('conv_pct', 0) or 0
            out.append(f"   {_icon(mc)} {ms.get('method', '?')}: <b>{mc:.1f}%</b> "
                       f"({ms.get('successful', 0)}/{ms.get('with_req', 0)})")
    return out


def _unpaid_block(merchant, unpaid_orders, endpoints=None):
    """endpoints — список имён продуктов алерта: примеры только с НИХ. Без фильтра
    в алерт по TRY-havale попадали рублёвые sbp-заявки с других эндпоинтов мерча
    (кейс Charlie) — саппорт разбирал не те заявки."""
    out = []
    mu = unpaid_orders.get(merchant, [])
    if mu and isinstance(mu, list):
        if endpoints:
            eps = set(endpoints)
            mu = [it for it in mu if not isinstance(it, dict) or not it.get("endpoint")
                  or it["endpoint"] in eps]
        if not mu:
            return out
        out.append("")
        out.append("📋 <b>Примеры неоплаченных заявок:</b>")
        for item in mu[:5]:
            if isinstance(item, dict):
                ext = item.get('order_id', '?')
                method = item.get('method', '')
                out.append(f"• <code>{ext}</code>{(' · ' + method) if method else ''}")
            else:
                out.append(f"• <code>{item}</code>")
    return out


def _alert_recipients():
    """Только групповой алертный чат. Раньше дублировали админам и всем авторизованным
    в ЛС — отключено (алерты и так в группе)."""
    return {ALERT_CHAT_ID}


async def _broadcast(context, text, reply_markup=None, chat_ids=None):
    """Рассылка с обработкой flood-control: на RetryAfter ждём и повторяем,
    иначе залп из нескольких алертов молча терял сообщения.
    chat_ids — явные получатели (напр. отдельный fin-чат); по умолчанию — общий алерт-чат."""
    for uid in (chat_ids if chat_ids is not None else _alert_recipients()):
        for attempt in range(3):
            try:
                await context.bot.send_message(chat_id=uid, text=text, parse_mode="HTML",
                                               reply_markup=reply_markup)
                break
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 3)) + 1)
            except Exception as e:
                logger.error(f"Ошибка отправки алерта -> {uid}: {e}")
                break
        await asyncio.sleep(0.1)  # лёгкий пейсинг между получателями


def _win_label(window_min):
    return f"{window_min // 60}ч" if window_min % 60 == 0 else f"{window_min}м"


_CAUSE_TECH = {"COMMON", "COMMON_ERROR", "PROVIDER_ERROR", "PROVIDER_REQUEST_TIMEOUT", "PAYMENT_SYSTEM_ERROR"}
_CAUSE_USER = {"CANCELLED", "SESSION_EXPIRED", "VALIDATION_ERROR"}


def _cause_category(code):
    """Категория кода отказа: техника (наша/гейт) / клиент / маршрутизация / прочее.
    ROUTING_ERROR — это НЕ обязательно антифрод: заявка не дошла до выдачи реквизита
    (нет свободного река/терминала, лимиты, каскад, либо антифрод). Это наш сигнал,
    а не «клиент плохой» — поэтому отдельная категория «маршрутизация» (ключ af)."""
    c = (code or "").upper()
    if c == "ROUTING_ERROR" or "ЛИМИТ" in c or "LIMIT" in c:
        return "af"   # маршрутизация / превышен лимит терминала — реквизит не выдан (наша сторона/ёмкость)
    if c in _CAUSE_USER or "TTL" in c or "CANCEL" in c:
        return "user"   # CANCEL ловит и CANCELLED, и MANUAL_CANCEL
    if c in _CAUSE_TECH or "PROVIDER" in c:
        return "tech"
    return "other"


def _fmt_dur(sec):
    mins = int(max(0, sec) / 60)
    return f"{mins // 60}ч {mins % 60}м" if mins >= 60 else f"{mins}м"


def _root_cause_lines(merchant, window_min, endpoints):
    """Строки 'причина отказов' (доли категорий, сходятся на 100%) + гейт-виновник.
    Грунт — полная разбивка status=4 за окно (без LIMIT), доли от суммы всех отказов."""
    try:
        rows = mb.get_failure_breakdown(merchant, window_min, endpoints)
    except Exception:
        return []
    cats = {"tech": 0, "user": 0, "af": 0, "other": 0}
    gate_fail = {}
    err_top = {}
    total = 0
    for r in rows:
        n = int(r.get("n") or 0)
        total += n
        cat = _cause_category(r.get("code"))
        cats[cat] += n
        err = (r.get("err") or "").strip()
        if err:
            err_top[err] = err_top.get(err, 0) + n
        if cat in ("tech", "af"):
            g = r.get("gate") or "(нет гейта)"
            if g != "(нет гейта)":
                gate_fail[g] = gate_fail.get(g, 0) + n
    if total == 0:
        return []
    order = [("tech", "техника"), ("user", "клиент"),
             ("af", "маршрутизация (не выдан реквизит)"), ("other", "прочее")]
    parts = [f"{lbl} {round(cats[k] * 100.0 / total)}%" for k, lbl in order if cats[k]]
    lines = ["", f"Причина отказов за {_win_label(window_min)} (всего {total}): " + " · ".join(parts)]
    # конкретика вместо голой категории: «техника 100%» без текстов бесполезна
    # (кейс Charlie: COMMON = и Duplicate Payment, и Declined by Acquirer)
    if err_top:
        top = sorted(err_top.items(), key=lambda kv: -kv[1])[:3]
        lines.append("Топ ошибок: " + " · ".join(f"{escape(e)} ×{n}" for e, n in top))
    # гейт-виновник — только когда технико/антифрод-отказы значимы (иначе при клиентском отвале это шум)
    if gate_fail and (cats["tech"] + cats["af"]) * 100.0 / total >= 15:
        g, n = max(gate_fail.items(), key=lambda kv: kv[1])
        lines.append(f"Больше всего технических отказов через гейт {escape(g)} ({n})")
    return lines


_BASELINE_CACHE = {"ts": 0.0, "data": None}


def _channel_baselines(days=BASELINE_DAYS):
    """Базовая конверсия канала за N дней — ТА ЖЕ логика, что в окне (get_recent_by_merchant_endpoint),
    поэтому сравнима с текущей. {'<merchant>|<channel>': {'conv', 'with_req'}}.
    Кэш на час: 7-дневный скан каждые 15 минут реплике ни к чему, норма меняется медленно."""
    now_ts = datetime.now().timestamp()
    if _BASELINE_CACHE["data"] is not None and now_ts - _BASELINE_CACHE["ts"] < 3600:
        return _BASELINE_CACHE["data"]
    try:
        data = mb.get_recent_by_merchant_endpoint(days * 1440)
    except Exception:
        return _BASELINE_CACHE["data"] or {}
    statuses = get_merchants_status_map()
    out = {}
    for m in data:
        if statuses.get(m) == 'disconnected' or _is_excluded_merchant(m):
            continue
        for ck, node in _aggregate_channels(data[m]).items():
            out[f"{m}|{ck}"] = {"conv": node["conv"], "with_req": node["with_req"]}
    _BASELINE_CACHE["ts"] = now_ts
    _BASELINE_CACHE["data"] = out
    return out


def _diag_block(p, window_min, endpoints, age_sec):
    """Диагностический блок алерта: норма за 7д + причина отказов/гейт + эскалация."""
    out = []
    base = p.get("baseline")
    if base is not None:
        out += ["", f"Норма за {BASELINE_DAYS} дней: {base:.1f}% (сейчас {p['conv']:.1f}%)"]
    out += _root_cause_lines(p["merchant"], window_min, endpoints)
    if age_sec >= ESCALATE_HOURS * 3600:
        out += ["", f"Проблема открыта уже {_fmt_dur(age_sec)} — пора эскалировать в Head Support."]
    return out


def _format_channel_alert(p, greeting, unpaid_orders, window_min, age_sec=0):
    conv = p["conv"]
    emoji, template_text = _emoji_template(conv)
    badges = analyze_alert_patterns(p["merchant"], conv)
    badge_str = "  ".join(badges) if badges else ""
    note = _tier_note(p.get("tiers"))
    note_str = f" — {note}" if note else ""
    lines = [
        f"{emoji} <b>{escape(p['merchant'])}</b> · <code>{escape(p['channel'])}</code>{note_str}"
        f"{(' · ' + badge_str) if badge_str else ''}",
        "",
        f"📈 Тотал-авг канала (FTD+STD) за {_win_label(window_min)}: <b>{conv:.1f}%</b> (порог {p.get('threshold', CONVERSION_THRESHOLD)}%)",
        f"📋 С реквизитами: {p['with_req']} → Оплатили: {p['successful']}",
    ]
    tiers = [t for t in p.get("tiers", []) if (t.get("with_req", 0) or 0) > 0]
    if len(tiers) > 1:
        lines.append("")
        lines.append("🧩 <b>Первичка / Вторичка:</b>")
        for t in tiers:
            lines.append(f"   {_icon(t['conv'])} {_TIER_LABEL.get(t['tier'], t['tier'])}: "
                         f"<b>{t['conv']:.1f}%</b> ({t['successful']}/{t['with_req']})")
    ep_names = [t["endpoint"] for t in p.get("tiers", []) if t.get("endpoint")] or None
    lines += _diag_block(p, window_min, ep_names, age_sec)
    lines += _methods_block(p.get("methods", []))
    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "", f"Коллеги, {greeting}!", "", template_text]
    lines += _unpaid_block(p["merchant"], unpaid_orders, ep_names)
    lines.append("")
    lines.append(f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")
    return "\n".join(lines)


def _format_endpoint_problem(p, greeting, unpaid_orders, window_min, age_sec=0):
    conv = p["conv"]
    emoji, template_text = _emoji_template(conv)
    tier_lbl = _TIER_LABEL.get(p.get("tier", "—"), "")
    lines = [
        f"{emoji} <b>{escape(p['merchant'])}</b> · <code>{escape(p['endpoint'])}</code>",
        f"📍 Эндпоинт-алерт ({tier_lbl}) за {_win_label(window_min)}",
        "",
        f"📊 Конверсия эндпоинта: <b>{conv:.1f}%</b> (порог {p.get('threshold', CONVERSION_THRESHOLD)}%)",
        f"📈 Тотал-авг канала (FTD+STD): <b>{p.get('channel_conv', conv):.1f}%</b> "
        f"(выдач по каналу: {p.get('channel_with_req', p['with_req'])})",
        f"📋 С реквизитами: {p['with_req']} → Оплатили: {p['successful']}",
    ]
    lines += _diag_block(p, window_min, [p["endpoint"]], age_sec)
    lines += _methods_block(p.get("methods", []))
    lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "", f"Коллеги, {greeting}!", "", template_text]
    lines += _unpaid_block(p["merchant"], unpaid_orders, [p["endpoint"]])
    lines.append("")
    lines.append(f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")
    return "\n".join(lines)


def _format_ok_status(window_min):
    s = mb.get_today_summary()
    conv = s.get("conversion", 0) or 0
    return (f"✅ <b>Конверсия в норме</b>\n\n"
            f"Проблемные каналы восстановились — за {_win_label(window_min)} все каналы выше порогов.\n"
            f"📈 Общая конверсия за сегодня: <b>{conv:.1f}%</b> "
            f"({s.get('successful_orders', 0)}/{s.get('total_orders', 0)})\n\n"
            f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")


def _format_recovery(label, since):
    return (f"✅ <b>Восстановилось</b>: {escape(label)}\n"
            f"Конверсия вернулась к норме (была в проблеме ~{_dur(since)}).\n\n"
            f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")


async def check_conversion(context):
    """Каждые 15 мин: проблемы конверсии за 2ч + восстановления + OK-статус (раз в час)."""
    try:
        baselines = _channel_baselines()
        problems = collect_problems(CONV_WINDOW_MIN, CONV_MIN_ISSUED, baselines)
        state = _load_state()
        try:
            unpaid = mb.get_unpaid_orders()
        except Exception:
            unpaid = {}
        greeting = get_greeting_by_time()
        current = {p["key"] for p in problems}

        sent = 0
        for p in problems:
            # для канала (схлопнут '*') добавляем тир в лейбл — чтобы и в «Восстановилось» было видно
            # первичка/вторичка. Эндпоинт уже содержит FTD/STD в имени.
            label = f"{p['merchant']} · {p.get('channel') or p.get('endpoint')}"
            if p["kind"] == "channel":
                _tn = _tier_note(p.get("tiers"))
                if _tn:
                    label += f" ({_tn})"
            if not _register(state, p["key"], label):
                continue
            since = (state.get(p["key"]) or {}).get("since") or datetime.now().timestamp()
            age_sec = datetime.now().timestamp() - since
            if p["kind"] == "channel":
                txt = _format_channel_alert(p, greeting, unpaid, CONV_WINDOW_MIN, age_sec)
            else:
                txt = _format_endpoint_problem(p, greeting, unpaid, CONV_WINDOW_MIN, age_sec)
            await _broadcast(context, txt)
            try:
                database.save_alert_history(p["merchant"], p["conv"], 'low_conversion')
            except Exception:
                pass
            sent += 1

        # восстановления — ТОЛЬКО при подтверждённом здоровье (объём ≥ RECOVERY_MIN и conv ≥ порога),
        # иначе (мало объёма / 1 успех / простой) молчим, проблему держим открытой
        metrics = _window_metrics(CONV_WINDOW_MIN)
        for k in list(state.keys()):
            if not k.startswith("conv_") or k in current:
                continue
            mt = metrics.get(k)
            if mt and mt["with_req"] >= RECOVERY_MIN_ISSUED and mt["conv"] >= mt["thr"]:
                rec = state.pop(k)
                await _broadcast(context, _format_recovery(rec.get("label", k), rec.get("since")))
                logger.info(f"Восстановление: {rec.get('label', k)}")

        # OK-статус — только при переходе «были проблемы → стало чисто» и при живой реплике.
        # Раньше слался каждый час (24 «всё ок»/день — спам для рабочего чата) и срабатывал
        # ложно при упавшей реплике (_q → [] → «проблем нет» рядом с алертом «реплика недоступна»).
        if problems:
            state["had_conv_problems"] = {"t": datetime.now().timestamp()}
        elif state.pop("had_conv_problems", None) and mb.replica_healthy():
            await _broadcast(context, _format_ok_status(CONV_WINDOW_MIN))

        _save_state(state)
        logger.info(f"check_conversion: проблем {len(problems)}, отправлено {sent}")
    except Exception as e:
        logger.error(f"Ошибка проверки конверсии: {e}")


def _fmt_rub(n):
    return f"{int(n):,}".replace(",", " ")


async def check_business_risks(context):
    """Каждые 30 мин: бизнес-риски, которых не ловят остальные джобы —
    1) концентрация приёма (single-point-of-failure по гейту),
    2) деньги в подвисе (выдан рек, не оплачен) на крупную сумму по эндпоинту,
    3) хроническая дыра маршрутизации по эндпоинту (сверх 20-мин спайка check_infra)."""
    try:
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')

        # 1) SPOF — концентрация приёма
        try:
            c = mb.get_gate_concentration(SPOF_HOURS)
            if (c["total_ok"] >= SPOF_MIN_OK and c["share"] >= SPOF_TOP_SHARE
                    and c["top_gate"] and c["top_gate"] != "(нет)"):
                if _register(state, "spof_gate", "концентрация приёма", hours=SPOF_THROTTLE_H):
                    await _broadcast(context,
                        f"⚠️ <b>Риск: единая точка отказа по приёму</b>\n\n"
                        f"За {SPOF_HOURS}ч гейт <b>{escape(c['top_gate'])}</b> = "
                        f"<b>{c['share']:.0f}%</b> всего приёма "
                        f"({c['top_ok']} из {c['total_ok']} успехов).\n"
                        f"При сбое этого гейта встаёт почти весь оборот. "
                        f"Стоит распределить трафик на резервные гейты.\n\n⏰ {ts}")
        except Exception as e:
            logger.error(f"SPOF-чек: {e}")

        # 2) Деньги в подвисе (выдан рек, не оплачен) по эндпоинтам
        try:
            statuses = get_merchants_status_map()
            for r in mb.get_limbo_by_endpoint(LIMBO_WINDOW_MIN):
                merch = r.get("merchant") or "—"
                if statuses.get(merch) == 'disconnected' or _is_excluded_merchant(merch):
                    continue
                cur = (r.get("cur") or "RUB").upper()
                amount = float(r.get("amount") or 0)
                if amount < LIMBO_THRESHOLD.get(cur, LIMBO_RUB):
                    continue
                ep = r.get("endpoint") or "—"
                key = f"limbo_{merch}_{ep}_{cur}"
                if _register(state, key, f"{merch} · {ep}", hours=LIMBO_THROTTLE_H):
                    await _broadcast(context,
                        f"💸 <b>Деньги в подвисе</b>\n\n"
                        f"{escape(merch)} · <code>{escape(ep)}</code>\n"
                        f"За {_win_label(LIMBO_WINDOW_MIN)}: реквизит выдан, но не оплачено "
                        f"<b>{_fmt_rub(amount)} {_CUR_SYM.get(cur, cur)}</b> ({r.get('cnt')} заявок).\n"
                        f"Деньги «на столе» — стоит подтолкнуть мерча/проверить приём.\n\n⏰ {ts}")
        except Exception as e:
            logger.error(f"Limbo-чек: {e}")

        # 3) Хроническая дыра маршрутизации по эндпоинту (сверх спайка check_infra)
        try:
            statuses = get_merchants_status_map()
            for r in mb.get_recent_routing_by_endpoint(ROUTE_HOLE_WINDOW_MIN):
                merch = r.get("merchant") or "—"
                if statuses.get(merch) == 'disconnected' or _is_excluded_merchant(merch):
                    continue
                ep = r.get("endpoint") or "—"
                if ep.strip().lower() in ROUTING_EXCLUDE_ENDPOINTS:
                    continue
                errs = int(r.get("errs") or 0)
                if errs < ROUTE_HOLE_EP_MIN:
                    continue
                key = f"route_hole_{merch}_{ep}"
                if _register(state, key, f"{merch} · {ep}", hours=ROUTE_HOLE_THROTTLE_H):
                    await _broadcast(context,
                        f"🧭 <b>Дыра маршрутизации</b>\n\n"
                        f"{escape(merch)} · <code>{escape(ep)}</code>\n"
                        f"За {_win_label(ROUTE_HOLE_WINDOW_MIN)}: <b>{errs}</b> заявок не дошли до гейта "
                        f"(нет маршрута / ROUTING_ERROR).\n"
                        f"Заявки гибнут до приёма — проверьте терминалы/лимиты/антифрод на эндпоинте.\n\n⏰ {ts}")
        except Exception as e:
            logger.error(f"Route-hole-чек: {e}")

        # 4) Просадка конверсии ПО МЕРЧУ vs 7-дневная норма (с автопричиной)
        try:
            statuses = get_merchants_status_map()
            now_w = mb.get_merchant_conv_window(MCONV_WINDOW_MIN)
            base_w = mb.get_merchant_conv_window(BASELINE_DAYS * 1440)
            for merch, cur in now_w.items():
                if statuses.get(merch) == 'disconnected' or _is_excluded_merchant(merch):
                    continue
                if cur["total"] < MCONV_MIN_TOTAL:
                    continue
                b = base_w.get(merch)
                if not b or b["total"] < MCONV_BASE_MIN_TOTAL:
                    continue
                if (b["conv"] - cur["conv"]) < MCONV_DROP_PP:
                    continue
                key = f"mconv_{merch}"
                if not _register(state, key, merch, hours=MCONV_THROTTLE_H):
                    continue
                lines = [
                    f"📉 <b>Просадка по мерчу: {escape(merch)}</b>",
                    "",
                    f"Конверсия в оплату за {_win_label(MCONV_WINDOW_MIN)}: <b>{cur['conv']:.1f}%</b> "
                    f"({cur['success']}/{cur['total']})",
                    f"Норма за {BASELINE_DAYS} дней: <b>{b['conv']:.1f}%</b> "
                    f"(падение на {b['conv'] - cur['conv']:.0f} п.п.)",
                ]
                lines += _root_cause_lines(merch, MCONV_WINDOW_MIN, None)
                lines += ["", "Это сводно по мерчу — стоит разобрать и написать в чат первым.",
                          f"\n⏰ {ts}"]
                await _broadcast(context, "\n".join(lines))
        except Exception as e:
            logger.error(f"Merchant-conv-чек: {e}")

        # 5) Недоставленные callback'и (наш вебхук вернул не-2xx)
        try:
            statuses = get_merchants_status_map()
            for r in mb.get_callback_failures(CALLBACK_WINDOW_MIN):
                merch = r.get("merchant") or "—"
                if statuses.get(merch) == 'disconnected' or _is_excluded_merchant(merch):
                    continue
                if r["n"] < CALLBACK_FAIL_MIN:
                    continue
                if not _register(state, f"cbfail_{merch}", merch, hours=CALLBACK_THROTTLE_H):
                    continue
                await _broadcast(context,
                    f"📡 <b>Callback не доходят: {escape(merch)}</b>\n\n"
                    f"За {_win_label(CALLBACK_WINDOW_MIN)} наш вебхук мерчу вернул не-2xx "
                    f"<b>{r['n']}</b> раз ({escape(r['codes_str'])}).\n"
                    f"Вероятно мерч не принимает callback (403/блок/наш IP не в whitelist) — "
                    f"оплаты у него не закрываются.\n\n⏰ {ts}")
        except Exception as e:
            logger.error(f"Callback-чек: {e}")

        # 6) Зависшие pending/new дольше нормы
        try:
            statuses = get_merchants_status_map()
            for r in mb.get_stuck_pending(PENDING_WINDOW_MIN, PENDING_OLDER_MIN):
                merch = r.get("merchant") or "—"
                if statuses.get(merch) == 'disconnected' or _is_excluded_merchant(merch):
                    continue
                n = int(r.get("n") or 0)
                if n < PENDING_MIN:
                    continue
                ep = r.get("endpoint") or "—"
                if not _register(state, f"pending_{merch}_{ep}", f"{merch} · {ep}", hours=PENDING_THROTTLE_H):
                    continue
                await _broadcast(context,
                    f"⏳ <b>Зависшие заявки</b>\n\n"
                    f"{escape(merch)} · <code>{escape(ep)}</code>\n"
                    f"<b>{n}</b> заявок висят в обработке дольше {PENDING_OLDER_MIN} мин и не финализируются.\n"
                    f"Проверьте гейт/пул реквизитов — возможен бэклог приёма.\n\n⏰ {ts}")
        except Exception as e:
            logger.error(f"Pending-чек: {e}")

        _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка проверки бизнес-рисков: {e}")


def _format_critical_alert(p, endpoints=None):
    note = _tier_note(p.get("tiers"))
    note_str = f" — {note}" if note else ""
    lines = [
        f"🆘 <b>СРОЧНО: канал просел</b>", "",
        f"{escape(p['merchant'])} · <code>{escape(p['channel'])}</code>{note_str}",
        f"За последние {CRITICAL_WINDOW_MIN} мин: конверсия <b>{p['conv']:.1f}%</b> "
        f"({p['successful']}/{p['with_req']})",
    ]
    # детальная разбивка отказов по заявкам канала (причины/коды/гейт) — чтобы сразу видеть проблему
    lines += _root_cause_lines(p["merchant"], CRITICAL_WINDOW_MIN, endpoints)
    lines += ["", "Канал работал и в моменте упал — проверьте приём прямо сейчас.",
              "", f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}"]
    return "\n".join(lines)


async def check_critical_drop(context):
    """Каждые 5 мин: быстрый коллапс канала за 20 мин. Общий ключ с check_conversion (без дублей)."""
    try:
        data = mb.get_recent_by_merchant_endpoint(CRITICAL_WINDOW_MIN)
        if not data:
            return
        statuses = get_merchants_status_map()
        state = _load_state()
        sent = 0
        for m in sorted(data.keys()):
            if statuses.get(m) == 'disconnected' or _is_excluded_merchant(m):
                continue
            for ck, node in _aggregate_channels(data[m]).items():
                # FTD-канал: здоровые 8-10% не должны крититься по STD-порогу 10%
                crit_thr = 3 if _channel_threshold(node) == FTD_THRESHOLD else CRITICAL_CONV
                if node["with_req"] >= CRITICAL_MIN_ISSUED and node["conv"] < crit_thr:
                    _tn = _tier_note(node.get("tiers"))
                    clabel = f"{m} · {ck}" + (f" ({_tn})" if _tn else "")
                    if not _register(state, f"conv_{m}_{ck}", clabel):
                        continue
                    ep_names = [t["endpoint"] for t in node.get("tiers", []) if t.get("endpoint")] or None
                    await _broadcast(context, _format_critical_alert(
                        {"merchant": m, "channel": ck, "conv": node["conv"], "tiers": node.get("tiers"),
                         "with_req": node["with_req"], "successful": node["successful"]}, ep_names))
                    sent += 1
                    logger.info(f"КРИТ-алерт: {m} / {ck} ({node['conv']:.1f}%)")
        if sent:
            _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка крит-проверки: {e}")


async def check_infra(context):
    """Каждые 5 мин: гейт встал(+восстановление), гейт тормозит/ошибки, медленные подтверждения, нет маршрута."""
    try:
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        fired = False
        down_now = set()
        gate_health = {}

        # 1) Гейт встал (исключаем "(нет гейта)")
        for r in mb.get_recent_gate_conversion(INFRA_WINDOW_MIN):
            gate = r.get("gate") or "(?)"
            if gate == "(нет гейта)":
                continue
            wr = int(r.get("with_req") or 0)
            su = int(r.get("success") or 0)
            conv = round(su * 100.0 / wr, 1) if wr else 0
            gate_health[f"gate_{gate}"] = {"conv": conv, "with_req": wr}
            if wr >= GATE_COLLAPSE_MIN_ISSUED and conv < GATE_COLLAPSE_CONV:
                key = f"gate_{gate}"
                down_now.add(key)
                if _register(state, key, f"гейт {gate}"):
                    fired = True
                    await _broadcast(context, (
                        f"🆘 <b>Гейт просел по приёму</b>\n\n{escape(gate)}\n"
                        f"За {INFRA_WINDOW_MIN} мин: реквизит выдан <b>{wr}</b> раз, оплачено <b>{su}</b> → "
                        f"конверсия <b>{conv:.1f}%</b>\n"
                        f"<i>(тревога при конверсии ниже {GATE_COLLAPSE_CONV}% и от {GATE_COLLAPSE_MIN_ISSUED} выдач за окно)</i>\n"
                        f"Реквизиты выдаются, а оплат почти нет — похоже, провайдер/терминал перестал принимать. "
                        f"Проверьте приём.\n\n⏰ {ts}"))
                    logger.info(f"Инфра: гейт встал {gate} ({conv}%)")

        # восстановление гейтов — только при подтверждённом здоровье (объём + conv ≥ порога)
        for k in list(state.keys()):
            if not k.startswith("gate_") or k in down_now:
                continue
            h = gate_health.get(k)
            if h and h["with_req"] >= GATE_COLLAPSE_MIN_ISSUED and h["conv"] >= GATE_COLLAPSE_CONV:
                rec = state.pop(k)
                fired = True
                await _broadcast(context, _format_recovery(rec.get("label", k), rec.get("since")))
                logger.info(f"Восстановление: {rec.get('label', k)}")

        # 2) Гейт тормозит / ошибки вызовов
        for r in mb.get_recent_gate_latency(INFRA_WINDOW_MIN):
            gate = r.get("gate") or "(?)"
            calls = int(r.get("calls") or 0)
            p50 = float(r.get("p50") or 0)
            p95 = float(r.get("p95") or 0)
            failed = int(r.get("failed") or 0)
            fpct = round(failed * 100.0 / calls, 1) if calls else 0
            slow = p50 > GATE_MEDIAN_SEC
            erroring = fpct > GATE_FAILED_PCT
            if calls >= GATE_LATENCY_MIN_CALLS and (slow or erroring) and _register(state, f"lat_{gate}", gate):
                fired = True
                if erroring and slow:
                    icon, title = "🔴", "Гейт: ошибки вызовов + медленно"
                    hint = "Провайдер и отклоняет вызовы, и отвечает медленно — проверьте терминал/доступ к провайдеру."
                elif erroring:
                    icon, title = "🔴", "Гейт: ошибки вызовов"
                    hint = "Провайдер отклоняет вызовы или не отвечает (по скорости норма) — проверьте терминал/доступ к провайдеру."
                else:
                    icon, title = "🐢", "Гейт тормозит"
                    hint = "Провайдер отвечает медленно (по ошибкам норма) — возможен бэклог приёма."
                fmark = " ⚠️" if erroring else ""
                pmark = " ⚠️" if slow else ""
                await _broadcast(context, (
                    f"{icon} <b>{title}</b>\n\n{escape(gate)}\n"
                    f"За {INFRA_WINDOW_MIN} мин (вызовов к провайдеру: {calls}):\n"
                    f"• Неудачных вызовов: <b>{fpct:.0f}%</b> ({failed}/{calls}){fmark}\n"
                    f"• Время ответа: медиана <b>{p50:.1f}с</b>{pmark} (p95 {p95:.1f}с)\n"
                    f"<i>(порог: ошибок &gt;{GATE_FAILED_PCT}% или медиана &gt;{GATE_MEDIAN_SEC}с при ≥{GATE_LATENCY_MIN_CALLS} вызовах)</i>\n"
                    f"{hint}\n\n⏰ {ts}"))
                logger.info(f"Инфра: латентность {gate} median={p50} p95={p95} fail%={fpct}")

        # 3) Деградация скорости подтверждения (#5) — адаптивно vs базовой медианы гейта
        for r in mb.get_gate_latency_degradation(30, 360):
            gate = r.get("gate") or "(?)"
            rs = int(r.get("recent_succ") or 0)
            rmed = float(r.get("recent_med") or 0)
            bmed = float(r.get("base_med") or 0)
            if rs >= LAT_MIN_SUCC and bmed > 0 and rmed >= LAT_RATIO * bmed and rmed >= LAT_MIN_SEC and _register(state, f"slowconf_{gate}", gate):
                fired = True
                await _broadcast(context, (
                    f"⚠️ <b>Гейт медленно подтверждает оплату</b>\n\n{escape(gate)}\n"
                    f"Время до успеха: было ~{bmed:.0f}с, стало ~<b>{rmed:.0f}с</b> (×{rmed / bmed:.1f}).\n"
                    f"Ранний признак проблем с провайдером.\n\n⏰ {ts}"))
                logger.info(f"Инфра: медленные подтверждения {gate} {bmed}->{rmed}")

        # 4) Нет маршрута / ROUTING_ERROR — по эндпоинтам (validation_error исключён)
        rows = mb.get_recent_routing_by_endpoint(INFRA_WINDOW_MIN)
        rows = [r for r in rows
                if (r.get("endpoint") or "").strip().lower() not in ROUTING_EXCLUDE_ENDPOINTS]
        total = sum(int(r["errs"]) for r in rows)
        if total >= ROUTING_SPIKE and _register(state, "routing", "маршрутизация"):
            fired = True
            lines = [f"⚠️ <b>Нет маршрута / ошибки маршрутизации</b>", "",
                     f"За {INFRA_WINDOW_MIN} мин: <b>{total}</b> заявок без маршрута или с ROUTING_ERROR.", "",
                     "📍 <b>По эндпоинтам:</b>"]
            for r in rows[:8]:
                lines.append(f"   • <code>{escape(r['endpoint'])}</code> ({escape(r['merchant'])}): {int(r['errs'])}")
            lines += ["", f"⏰ {ts}"]
            await _broadcast(context, "\n".join(lines))
            logger.info(f"Инфра: нет маршрута {total}")

        if fired:
            _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка инфра-проверки: {e}")


async def check_trends(context):
    """Каждый час: падение объёма по эндпоинтам vs вчера + спайк кода отказа."""
    try:
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        fired = False

        drops = []
        for r in mb.get_volume_by_endpoint(60):
            now_win = int(r.get("now_win") or 0)
            yest = int(r.get("yest_win") or 0)
            if yest >= VOLUME_MIN_YEST and now_win < VOLUME_DROP_RATIO * yest:
                drops.append((r.get("endpoint") or "—", r.get("merchant") or "—", now_win, yest))
        drops = [d for d in drops if _register(state, f"vol_{d[1]}_{d[0]}", f"объём {d[0]}")]
        if drops:
            fired = True
            lines = ["📉 <b>Падение объёма заявок</b>", "", "📍 <b>По эндпоинтам (за час vs вчера):</b>"]
            for ep, mer, nw, ye in sorted(drops, key=lambda x: x[3], reverse=True)[:10]:
                dp = round((1 - nw / ye) * 100) if ye else 0
                lines.append(f"   • <code>{escape(ep)}</code> ({escape(mer)}): {nw} vs {ye} (−{dp}%)")
            lines += ["", "Возможно, мерчант перестал слать трафик или сломалась интеграция.", "", f"⏰ {ts}"]
            await _broadcast(context, "\n".join(lines))
            logger.info(f"Тренд: падение объёма на {len(drops)} эндпоинтах")

        codes = mb.get_recent_decline_codes(60)
        total_fail = sum(int(r["c"]) for r in codes)
        if total_fail >= DECLINE_SPIKE_MIN and codes:
            top = codes[0]
            share = round(int(top["c"]) * 100.0 / total_fail, 1) if total_fail else 0
            code = top["code"]
            if share >= DECLINE_SPIKE_SHARE and code not in DECLINE_IGNORE and _register(state, f"decline_{code}", str(code)):
                fired = True
                await _broadcast(context, (
                    f"⚠️ <b>Спайк отказов: {escape(str(code))}</b>\n\n"
                    f"За час: <b>{int(top['c'])}</b> из {total_fail} отказов ({share:.0f}%) — один код доминирует.\n"
                    f"Похоже на системную проблему (банк/метод/гейт).\n\n⏰ {ts}"))
                logger.info(f"Тренд: спайк отказов {code} {share}%")

        if fired:
            _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка трендов: {e}")


async def check_heartbeat(context):
    """Каждые 5 мин: «приём встал» — нет заявок (при наличии вчера) или поток без оплат. С восстановлением."""
    try:
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')

        # Реплика недоступна → все _q() возвращают [] и мониторинг СЛЕПНЕТ (тихо, без алертов).
        # Явно сигналим о недоступности данных, вместо ложного «всё ок».
        if not mb.replica_healthy():
            if _register(state, "hb_replica_down", "реплика недоступна"):
                await _broadcast(context, (
                    f"🚨 <b>Реплика недоступна</b>\n\n"
                    f"Мониторинг временно слеп — данные из БД не читаются.\n"
                    f"Проверьте доступность реплики Core.\n\n⏰ {ts}"))
                logger.error("Heartbeat: реплика недоступна")
                _save_state(state)
            return
        if "hb_replica_down" in state:
            rec = state.pop("hb_replica_down")
            await _broadcast(context, _format_recovery(rec.get("label", "реплика"), rec.get("since")))
            _save_state(state)

        w = mb.get_window_summary(HEARTBEAT_WINDOW)
        vc = mb.get_volume_compare(HEARTBEAT_WINDOW)
        fired = False

        # a) нет заявок (а вчера в это окно были)
        if w["total"] == 0 and (vc.get("yest_win") or 0) >= HEARTBEAT_MIN_EXPECTED:
            if _register(state, "hb_noorders", "приём: нет заявок"):
                fired = True
                await _broadcast(context, (
                    f"🚨 <b>Приём встал — нет заявок</b>\n\n"
                    f"За {HEARTBEAT_WINDOW} мин: <b>0 заявок</b> (вчера в это же время {vc.get('yest_win')}).\n"
                    f"Похоже, лёг приём / интеграция / ингест.\n\n⏰ {ts}"))
                logger.info("Heartbeat: нет заявок")
        elif "hb_noorders" in state and w["total"] > 0:
            rec = state.pop("hb_noorders"); fired = True
            await _broadcast(context, _format_recovery(rec.get("label", "приём"), rec.get("since")))

        # b) поток есть, но 0 оплат
        if w["issued"] >= HEARTBEAT_MIN_FLOW and w["success"] == 0:
            if _register(state, "hb_nosuccess", "приём: 0 оплат при потоке"):
                fired = True
                await _broadcast(context, (
                    f"🚨 <b>Приём лёг — 0 оплат</b>\n\n"
                    f"За {HEARTBEAT_WINDOW} мин: <b>{w['issued']} выдач, 0 оплат</b>.\n"
                    f"Срочно проверьте приём.\n\n⏰ {ts}"))
                logger.info("Heartbeat: 0 оплат при потоке")
        elif "hb_nosuccess" in state and w["success"] > 0:
            rec = state.pop("hb_nosuccess"); fired = True
            await _broadcast(context, _format_recovery(rec.get("label", "приём"), rec.get("since")))

        if fired:
            _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка heartbeat: {e}")


# ============ АЛЕРТ: ОСТАНОВКА ТРАФИКА (мерч/эндпоинт) ============

def _stop_recent_total(recent, merchant, endpoint=None):
    """Сумма заявок за recent-окно по мерчу (endpoint=None) или конкретному эндпоинту."""
    eps = recent.get(merchant or "", {})
    if endpoint is not None:
        return int((eps.get(endpoint) or {}).get("total") or 0)
    return sum(int((e or {}).get("total") or 0) for e in eps.values())


def _stop_context_block(ctx):
    """Блок «перед остановкой»: конверсия + топ-причины отказа — чтобы видеть, встал ли мерч НА ФОНЕ ошибок."""
    if not ctx or not ctx.get("total"):
        return []
    out = ["", f"📊 За 24ч: опл {ctx['ok']}/{ctx['total']} · конв {ctx['conv']:.0f}%"]
    # «встал из-за ошибок» = конверсия перед остановкой была ниже порога (иначе мерч просто выключился штатно)
    if ctx["conv"] < CONVERSION_THRESHOLD and ctx.get("reasons"):
        out.append("⚠️ Похоже, встал на фоне отказов. Топ-причины:")
        for r in ctx["reasons"][:5]:
            out.append(f"   • <code>{escape(str(r['code']))}</code> [{escape(str(r['gate']))}] — {r['n']}")
    return out


def _format_merchant_stop(merchant, prior_total, stopped_eps, ts, ctx=None):
    """Шапка «0 заявок» считается раздельно по STD и FTD — STD-окно 30м, FTD-окно 3ч.
    Если у мерча оба тира лились — показываем обе строки, чтобы не вводить в заблуждение
    (раньше писали «0 за 3ч» даже когда реально 0 был только за 30м STD-окна)."""
    has_std = any(not _is_ftd_ep(ep) for ep, _ in stopped_eps)
    has_ftd = any(_is_ftd_ep(ep) for ep, _ in stopped_eps)
    std_win = f"{STOP_WINDOW_MIN} мин"
    ftd_win = f"{STOP_WINDOW_FTD_MIN // 60}ч"
    if has_std and has_ftd:
        head_line = (f"🏪 <b>{escape(merchant)}</b> — STD: <b>0 заявок</b> за {std_win} · "
                     f"FTD: <b>0 заявок</b> за {ftd_win}.")
    elif has_ftd:
        head_line = f"🏪 <b>{escape(merchant)}</b> — <b>0 заявок</b> за {ftd_win} (FTD)."
    else:
        head_line = f"🏪 <b>{escape(merchant)}</b> — <b>0 заявок</b> за {std_win} (STD)."
    lines = [
        "🛑 <b>Мерч остановил трафик</b>",
        "",
        head_line,
        f"До этого за ~24ч было: <b>{prior_total}</b> заявок.",
    ]
    if stopped_eps:
        lines.append("")
        lines.append("🧩 <b>Вставшие эндпоинты</b> (заявок перед остановкой):")
        for ep, n in stopped_eps[:12]:
            lines.append(f"   • <code>{escape(ep)}</code> — {n}")
        if len(stopped_eps) > 12:
            lines.append(f"   …и ещё {len(stopped_eps) - 12}")
    lines += _stop_context_block(ctx)
    lines += ["", f"⏰ {ts}"]
    return "\n".join(lines)


def _format_endpoint_stop(merchant, endpoint, prior_ep, merchant_recent, ts, ctx=None, window_min=STOP_WINDOW_MIN):
    win = f"{window_min // 60}ч" if window_min >= 60 else f"{window_min} мин"
    lines = [
        "🛑 <b>Эндпоинт остановил трафик</b>",
        "",
        f"🏪 <b>{escape(merchant)}</b>",
        f"🧩 <code>{escape(endpoint)}</code> — <b>0 заявок</b> за {win} "
        f"(до этого за ~24ч: {prior_ep}).",
        f"ℹ️ Мерч в целом ещё льёт: {merchant_recent} заявок (по своим окнам).",
    ]
    lines += _stop_context_block(ctx)
    lines += ["", f"⏰ {ts}"]
    return "\n".join(lines)


def _format_stop_reminder(label, since):
    return (f"⏳ <b>Всё ещё нет трафика</b>: {escape(label)}\n"
            f"Неактивен уже ~{_dur(since)}.\n\n"
            f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")


def _format_traffic_resumed(label, since):
    return (f"✅ <b>Трафик возобновился</b>: {escape(label)}\n"
            f"Простой был ~{_dur(since)}.\n\n"
            f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")


async def check_traffic_stop(context):
    """Каждые ~10 мин: мерч/эндпоинт лил и резко встал — 0 заявок за STOP_WINDOW_MIN при потоке в предыдущие ~2.5ч.
    Один алерт при остановке; пока трафика нет — напоминание раз в STOP_REMIND_HOURS с длительностью простоя; ✅ при возврате.
    Анти-спам: жёсткий ноль + базлайн-гейт (только для НОВОГО детекта) + дедуп иерархии + сопровождение по реальному трафику."""
    try:
        recent_std = mb.get_recent_by_merchant_endpoint(STOP_WINDOW_MIN)        # 30 мин — для STD
        recent_ftd = mb.get_recent_by_merchant_endpoint(STOP_WINDOW_FTD_MIN)    # 180 мин — для FTD
        base = mb.get_recent_by_merchant_endpoint(STOP_BASE_MIN)                # 24ч
        statuses = get_merchants_status_map()
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        now = datetime.now().timestamp()
        fired = False

        def _ep_recent(m, ep):
            """Заявок за окно тишины конкретного эндпоинта (FTD=3ч, STD=30м)."""
            return _stop_recent_total(recent_ftd if _is_ftd_ep(ep) else recent_std, m, ep)

        def _merch_recent(m, eps_iter):
            """Заявок мерча с учётом per-endpoint окон (для смешанных STD+FTD мерчей)."""
            return sum(_ep_recent(m, ep) for ep in eps_iter)

        def _enough_rate(base_n, window_min):
            """Хватает ли интенсивности базы, чтобы тишина в окне была сигналом, а не нормой."""
            return base_n * window_min / STOP_BASE_MIN >= STOP_MIN_EXPECTED

        def _in_cooldown(key):
            cd = state.get(f"stopcool_{key}")
            return bool(cd and now - (cd.get("t") or 0) < STOP_RESUME_COOLDOWN_MIN * 60)

        # 1) ДЕТЕКТ НОВЫХ остановок (был поток за сутки → 0 сейчас)
        for m, eps_base in base.items():
            if _is_excluded_merchant(m) or statuses.get(m) == 'disconnected':
                continue
            base_total = sum(int((e or {}).get("total") or 0) for e in eps_base.values())
            recent_total = _merch_recent(m, eps_base.keys())
            prior_total = base_total - recent_total

            # a) встал весь мерч → один алерт со списком вставших эндпоинтов инлайн
            merch_window = STOP_WINDOW_FTD_MIN if all(_is_ftd_ep(ep) for ep in eps_base) else STOP_WINDOW_MIN
            if recent_total == 0 and prior_total >= STOP_MERCHANT_BASE_MIN \
                    and _enough_rate(prior_total, merch_window):
                # поглощаем отдельные эндпоинт-инциденты этого мерча (не дублируем)
                for k in [k for k in list(state) if k.startswith("stop_ep_") and state[k].get("merchant") == m]:
                    del state[k]
                key = f"stop_merchant_{m}"
                if key not in state and not _in_cooldown(key):
                    stopped_eps = sorted(
                        ((ep, int((eb or {}).get("total") or 0)) for ep, eb in eps_base.items()
                         if int((eb or {}).get("total") or 0) > 0),
                        key=lambda x: x[1], reverse=True)
                    try:
                        ctx = mb.get_decline_context(merchant=m, minutes=STOP_BASE_MIN)
                    except Exception:
                        ctx = None
                    state[key] = {"label": f"мерч {m}", "since": now, "last": now, "merchant": m}
                    fired = True
                    await _broadcast(context, _format_merchant_stop(
                        m, prior_total, stopped_eps, ts, ctx))
                continue

            # b) встал отдельный эндпоинт, а мерч в целом ещё льёт
            for ep, eb in eps_base.items():
                base_ep = int((eb or {}).get("total") or 0)
                recent_ep = _ep_recent(m, ep)
                prior_ep = base_ep - recent_ep
                ep_window = STOP_WINDOW_FTD_MIN if _is_ftd_ep(ep) else STOP_WINDOW_MIN
                if recent_ep == 0 and prior_ep >= STOP_ENDPOINT_BASE_MIN \
                        and _enough_rate(prior_ep, ep_window):
                    key = f"stop_ep_{m}_{ep}"
                    if key not in state and not _in_cooldown(key):
                        try:
                            ctx = mb.get_decline_context(merchant=m, minutes=STOP_BASE_MIN, endpoint=ep)
                        except Exception:
                            ctx = None
                        state[key] = {"label": f"эндпоинт {ep} ({m})", "since": now, "last": now,
                                      "merchant": m, "endpoint": ep}
                        fired = True
                        await _broadcast(context, _format_endpoint_stop(
                            m, ep, prior_ep, recent_total, ts, ctx,
                            window_min=STOP_WINDOW_FTD_MIN if _is_ftd_ep(ep) else STOP_WINDOW_MIN))

        # 2) СОПРОВОЖДЕНИЕ открытых инцидентов: вернулся трафик → ✅;
        #    иначе напоминание раз в STOP_REMIND_HOURS, но НЕ дольше STOP_REMIND_CAP_HOURS от старта инцидента
        #    (после кап-окна тихо ждём возврата — без спама).
        for key in [k for k in list(state) if k.startswith("stop_")]:
            rec = state[key]
            ep_name = rec.get("endpoint")
            m_name = rec.get("merchant", "")
            if ep_name:
                rec_count = _ep_recent(m_name, ep_name)
            else:
                rec_count = _merch_recent(m_name, base.get(m_name, {}).keys())
            if rec_count > 0:
                del state[key]
                state[f"stopcool_{key}"] = {"t": now}   # кулдаун на повторный детект (анти-пинг-понг)
                fired = True
                await _broadcast(context, _format_traffic_resumed(rec.get("label", key), rec.get("since")))
                continue
            inactive_sec = now - (rec.get("since") or now)
            if inactive_sec < STOP_REMIND_CAP_HOURS * 3600 \
                    and now - (rec.get("last") or 0) >= STOP_REMIND_HOURS * 3600:
                rec["last"] = now
                fired = True
                await _broadcast(context, _format_stop_reminder(rec.get("label", key), rec.get("since")))

        if fired:
            _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка check_traffic_stop: {e}")


# ── Спец-алерт: NSPK VIP — milestone по сумме успехов + спецсообщение около цели ──
# Настройки можно задавать ПЕР-ЭНДПОИНТ (step / cap / hot), иначе берутся дефолты ниже.
NSPK_VIP_STEP_RUB = 100_000        # дефолтный шаг milestone
NSPK_VIP_HOT_TARGET = 700_000      # дефолтная "уже близко" отметка
NSPK_VIP_HOT_BAND = 3_000          # ±3к вокруг цели
# Список эндпоинтов под алерт. Расширять — просто добавлять словари.
#   step — шаг алертов; cap — ограничитель (выше не алертить); hot — отметка "уже близко" (None = выкл).
NSPK_VIP_ENDPOINTS = [
    # Alpha NSPK VIP: дефолт — шаг 100к, "уже близко" 700к
    {"product_id": "00000000-0000-4000-8000-000000000001", "label": "Alpha_RUB_QR_NSPK_VIP_In"},
    # Alpha NSPK VIP 999: шаг 50к, ограничитель 200к
    {"product_id": "00000000-0000-4000-8000-000000000002", "label": "Alpha_RUB_QR_NSPK_VIP_999_In",
     "step": 50_000, "cap": 200_000, "hot": None},
    {"product_id": "00000000-0000-4000-8000-000000000003", "label": "Charlie_RUB_QR_NSPK_VIP_In"},
]


def _nspk_vip_today_stats(product_id: str) -> dict:
    """Метрики эндпоинта за сегодня МСК: paid_sum, total, issued, success + конверсии.
    issued = выдача рек (deeplink OR route_result.success OR status 3/6)."""
    rows = mb.execute_question_raw_sql(f"""
        SELECT
            COALESCE(SUM(i.amount_paid) FILTER (WHERE i.payment_status_id IN (3,6)),0)::numeric AS paid_sum,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE
                 (i.data->'beneficiary'->>'deeplink') IS NOT NULL
              OR COALESCE(i.data->'route_result'->>'status'='success', false)
              OR i.payment_status_id IN (3,6)) AS issued,
            COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) AS success
        FROM pricing_invoice i
        WHERE i.primary_product_id::text = '{product_id}'
          AND (i.created_at AT TIME ZONE 'Europe/Moscow')::date
              = (now() AT TIME ZONE 'Europe/Moscow')::date
          AND i.is_test = false
    """)
    if not rows:
        return {"paid_sum": 0.0, "total": 0, "issued": 0, "success": 0,
                "conv_issue_pct": 0.0, "conv_pay_pct": 0.0}
    r = rows[0]
    total = int(r.get("total") or 0)
    issued = int(r.get("issued") or 0)
    success = int(r.get("success") or 0)
    paid_sum = float(r.get("paid_sum") or 0)
    conv_issue = (issued / total * 100) if total else 0.0   # выдач / всего
    conv_pay   = (success / issued * 100) if issued else 0.0  # опл / выдач
    return {"paid_sum": paid_sum, "total": total, "issued": issued, "success": success,
            "conv_issue_pct": round(conv_issue, 1), "conv_pay_pct": round(conv_pay, 1)}


async def check_nspk_vip_milestones(context):
    """Шлёт алерт на каждой ступени суммы успехов (шаг step, не выше cap) по каждому NSPK VIP
    эндпоинту за сегодня + опц. спецсообщение около hot. Шаг/cap/hot — пер-эндпоинт (см. NSPK_VIP_ENDPOINTS).
    State в alert_state.json, ключи per (date, product_id) → авто-сброс на следующих сутках."""
    today_msk = datetime.now(MSK).strftime("%Y-%m-%d")
    ts = datetime.now(MSK).strftime("%d.%m.%Y %H:%M:%S")
    state = _load_state()
    fired = False

    for ep in NSPK_VIP_ENDPOINTS:
        pid, label = ep["product_id"], ep["label"]
        step = int(ep.get("step", NSPK_VIP_STEP_RUB))
        cap  = ep.get("cap")                                  # None = без ограничителя
        hot  = ep.get("hot", NSPK_VIP_HOT_TARGET)             # None = "уже близко" выкл
        try:
            st = _nspk_vip_today_stats(pid)
            s = st["paid_sum"]
            if s <= 0:
                continue
            key_m   = f"nspk_vip_milestone:{pid}:{today_msk}"
            key_hot = f"nspk_vip_hot:{pid}:{hot}:{today_msk}"
            cur = (int(s) // step) * step
            if cap is not None:                              # ограничитель: выше cap не алертим
                cur = min(cur, int(cap))

            # Первая инициализация эндпоинта за сутки — НЕ шлём ретроспективно.
            if key_m not in state:
                state[key_m] = cur
                fired = True
                last = cur
            else:
                last = int(state[key_m] or 0)

            stats_line = (
                f"Выдача: <b>{st['issued']}/{st['total']}</b> ({st['conv_issue_pct']:.1f}%) · "
                f"Успех: <b>{st['success']}/{st['issued']}</b> ({st['conv_pay_pct']:.1f}%)"
            )

            # Шаги — одно сообщение на каждую пересечённую ступень (с учётом ограничителя cap).
            if cur > last:
                for m in range(last + step, cur + 1, step):
                    capnote = " (ограничитель)" if cap is not None and m >= int(cap) else ""
                    msg = (
                        f"🎯 <b>{label}</b>\n"
                        f"Пересёк отметку <b>{m:,} ₽</b>{capnote} по сумме успехов.\n"
                        f"Сейчас: <b>{int(s):,} ₽</b>\n"
                        f"{stats_line}\n"
                        f"\n⏰ {ts}"
                    ).replace(",", " ")
                    await _broadcast(context, msg)
                state[key_m] = cur
                fired = True

            # Hot-зона около цели (±3к) — отдельный спец-алерт, один раз за сутки. None = выкл.
            if hot is not None and (hot - NSPK_VIP_HOT_BAND) <= s <= (hot + NSPK_VIP_HOT_BAND) \
                    and not state.get(key_hot):
                msg = (
                    f"🔥 <b>{label}</b> — ~<b>{hot:,} ₽</b> уже близко\n"
                    f"Сейчас: <b>{int(s):,} ₽</b> ({int(s)-hot:+,} от {hot//1000}к)\n"
                    f"{stats_line}\n"
                    f"\n⏰ {ts}"
                ).replace(",", " ")
                await _broadcast(context, msg)
                state[key_hot] = True
                fired = True
        except Exception as e:
            logger.error(f"Ошибка check_nspk_vip_milestones для {label}: {e}")

    if fired:
        _save_state(state)


# ── Финансовые алерты: нулевая ставка и отрицательная маржа ──
FIN_WINDOW_MIN = 60          # окно проверки успешных операций
ZEROFEE_THROTTLE_H = 6       # повтор по тому же продукту не чаще раза в 6ч
NEGMARGIN_THROTTLE_H = 6     # повтор по той же паре продукт×терминал не чаще раза в 6ч
# Эндпоинты, по которым НЕ алертить отрицательную маржу.
# Echo MK ВЕРНУЛИ в маржа-алерты — оставлен только спам-юзер.
NEGMARGIN_EXCLUDE_PRODUCTS = set()
# Теги в финанс-алертах — пинговать AVM и финансы
FINANCE_TAGS = "@finance_lead @finance_team"
# Пороговый пуш по объёму Echo МК (P2P_MC/Sigma):
# ДНЕВНОЙ счётчик оборота с 00:00 МСК (авто-сброс в полночь) + сброс кнопкой «возмещение отдано»
# (reset_at=now). Добил порог → пуш с кнопкой. После сброса/полуночи копит заново → снова пуш.
PFMK_VOLUME_TARGET = 1100000  # ₽ — порог накопленного оборота для пуша (1.1 млн)
# Пороговый пуш по балансу провайдера Vega (gate-vega, NSPK).
# Ручной счётчик (НЕ дневной): копим с последнего сброса, пуш при достижении порога в fin-чат,
# кнопка «обнулить» сбрасывает (reset_at=now) → считаем заново.
# Порог: «либо 10к баксов, либо 750к рублей» (Finance). Vega считает в RUB → берём 750к ₽.
FORA_BALANCE_TARGET = 750000  # ₽ — порог баланса Vega для пуша (≈ $10k)


async def check_finance_risks(context):
    """Каждые 30 мин: 1) успешные операции по продуктам без проставленной ставки
    (комиссия с мерча = 0 → теряем деньги), 2) операции с отрицательной маржой
    (ставка мерча ниже ставки гейта). Шлётся в алертный чат, дедуп в alert_state."""
    try:
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        # Все финанс-алерты идут в отдельную группу «fin alert»
        from config import FIN_ALERT_CHAT_ID
        fin_chat = FIN_ALERT_CHAT_ID or ALERT_CHAT_ID

        # 1) нулевая ставка на мерче
        try:
            for r in mb.get_zero_fee_success(FIN_WINDOW_MIN):
                merch = r.get("merchant") or "—"
                if _is_excluded_merchant(merch):
                    continue
                prod = r.get("product") or "—"
                cur = (r.get("cur") or "RUB").upper()
                key = f"zerofee_{prod}"
                if _register(state, key, f"{merch} · {prod}", hours=ZEROFEE_THROTTLE_H):
                    await _broadcast(context,
                        f"💸 <b>Операции с нулевой ставкой</b>\n\n"
                        f"{escape(merch)} · <code>{escape(prod)}</code>\n"
                        f"За {_win_label(FIN_WINDOW_MIN)}: <b>{r.get('n')}</b> успешных на "
                        f"<b>{_fmt_rub(float(r.get('vol') or 0))} {_CUR_SYM.get(cur, cur)}</b>, "
                        f"комиссия с мерча не берётся — ставка fee_from_merchant не проставлена.\n"
                        f"Нужно проставить ставку на продукт.\n\n{FINANCE_TAGS}\n⏰ {ts}",
                        chat_ids=[fin_chat])
        except Exception as e:
            logger.error(f"zero-fee чек: {e}")

        # 2) отрицательная маржа
        try:
            for r in mb.get_negative_margin(FIN_WINDOW_MIN):
                merch = r.get("merchant") or "—"
                if _is_excluded_merchant(merch):
                    continue
                prod = r.get("product") or "—"
                if prod.strip().lower() in NEGMARGIN_EXCLUDE_PRODUCTS:
                    continue
                term = r.get("terminal") or "—"
                cur = (r.get("cur") or "RUB").upper()
                key = f"negmargin_{prod}_{term}"
                if _register(state, key, f"{merch} · {prod}", hours=NEGMARGIN_THROTTLE_H):
                    await _broadcast(context,
                        f"📉 <b>Операции с отрицательной маржой</b>\n\n"
                        f"{escape(merch)} · <code>{escape(prod)}</code>\n"
                        f"Терминал: <code>{escape(term)}</code>\n"
                        f"Ставка мерча <b>{r.get('merch_pct')}%</b> &lt; ставка гейта <b>{r.get('gate_pct')}%</b>.\n"
                        f"За {_win_label(FIN_WINDOW_MIN)}: <b>{r.get('n')}</b> операций, маржа "
                        f"<b>{_fmt_rub(float(r.get('margin') or 0))} {_CUR_SYM.get(cur, cur)}</b>.\n"
                        f"Проверить ставки на продукте/терминале.\n\n{FINANCE_TAGS}\n⏰ {ts}",
                        chat_ids=[fin_chat])
        except Exception as e:
            logger.error(f"negative-margin чек: {e}")

        # 3) пороговый пуш по объёму Echo МК (Sigma P2P_MC): ДНЕВНОЙ счётчик с 00:00 МСК
        #    + сброс кнопкой возмещения (считаем с 00:00 МСК).
        try:
            today0 = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
            pcfg = state.get("pfmk_counter")
            if not isinstance(pcfg, dict):
                pcfg = {}
            ra = pcfg.get("reset_at")
            # авто-сброс на новые сутки МСК (reset_at с прошлого дня) ИЛИ миграция на дневной режим
            if pcfg.get("mode") != "daily" or not ra or datetime.fromisoformat(ra) < today0:
                pcfg = {"reset_at": today0.isoformat(), "fired": False, "mode": "daily"}
            pf = mb.get_echo_mk_volume_since(pcfg.get("reset_at"))
            vol = float(pf.get("vol") or 0)
            if vol >= PFMK_VOLUME_TARGET and not pcfg.get("fired"):
                pcfg["fired"] = True
                pcfg["fired_vol"] = vol
                pcfg["fired_at"] = datetime.now(MSK).isoformat()
                cur = (pf.get("cur") or "RUB").upper()
                sym = _CUR_SYM.get(cur, cur)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                    "✅ Возмещение отдано — сбросить счётчик", callback_data="pfmk:reset")]])
                await _broadcast(context,
                    f"🎯 <b>Echo МК: накоплено {_fmt_rub(vol)} {sym}</b>\n\n"
                    f"Метод P2P_MC (Sigma), оплаченный оборот с 00:00 МСК (или последнего возмещения) добил порог "
                    f"<b>{_fmt_rub(PFMK_VOLUME_TARGET)} {sym}</b> ({pf.get('n')} успешных операций).\n"
                    f"Нужно возмещение. Как отдадите — жмите кнопку, счётчик обнулится и начнёт копить заново.\n\n"
                    f"{FINANCE_TAGS}\n⏰ {ts}", reply_markup=kb, chat_ids=[fin_chat])
            state["pfmk_counter"] = pcfg
        except Exception as e:
            logger.error(f"pf-mk объём чек: {e}")

        # 4) пороговый пуш по балансу Vega (gate-vega, NSPK): копим с последнего сброса,
        #    пуш при достижении порога, кнопка обнуляет. Сброс ТОЛЬКО ручной.
        try:
            fcfg = state.get("vega_counter")
            if not isinstance(fcfg, dict) or not fcfg.get("reset_at"):
                fcfg = {"reset_at": datetime.now(MSK).isoformat(), "fired": False}
            fb = mb.get_vega_balance_since(fcfg.get("reset_at"))
            fvol = float(fb.get("vol") or 0)
            if fvol >= FORA_BALANCE_TARGET and not fcfg.get("fired"):
                fcfg["fired"] = True
                fcfg["fired_vol"] = fvol
                fcfg["fired_at"] = datetime.now(MSK).isoformat()
                cur = (fb.get("cur") or "RUB").upper()
                sym = _CUR_SYM.get(cur, cur)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                    "🔄 Обнулить счётчик — считать заново", callback_data="vega:reset")]])
                await _broadcast(context,
                    f"🎯 <b>Vega: баланс {_fmt_rub(fvol)} {sym}</b>\n\n"
                    f"Провайдер Vega (NSPK) накопил с последнего сброса "
                    f"<b>{_fmt_rub(fvol)} {sym}</b> ({fb.get('n')} успешных операций) — "
                    f"порог <b>{_fmt_rub(FORA_BALANCE_TARGET)} {sym}</b> (≈ $10k) достигнут.\n"
                    f"Как разберётесь — жмите кнопку, счётчик обнулится и начнёт считать заново.\n\n"
                    f"{FINANCE_TAGS}\n⏰ {ts}", reply_markup=kb, chat_ids=[fin_chat])
            state["vega_counter"] = fcfg
        except Exception as e:
            logger.error(f"vega баланс чек: {e}")

        _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка check_finance_risks: {e}")


# ── Алерт «реквизит сдох» (кнопки + авто-удаление протухших) ──
DEADREQ_DAYS = 1             # окно анализа: сутки
DEADREQ_MIN_ISSUED = 7       # минимум выдач (стрик 7 сам по себе требует 7)
DEADREQ_STREAK = 7           # стрик 7+ отмен ПОДРЯД в любом месте окна — алерт сразу
DEADREQ_THROTTLE_H = 12      # повтор по тому же реквизиту не чаще раза в 12ч
DEADREQ_LIST_CAP = 20        # максимум реквизитов за один прогон (по одному сообщению на рек)
DEADREQ_IDLE_HOURS = 3       # нет новых сделок по реку столько часов → алерт протух, удаляем


def _dead_req_verdict(flags):
    """(алертить?, длина стрика, продолжается ли). Правило Head Support: стрик
    DEADREQ_STREAK+ неуспехов ПОДРЯД где угодно в окне — даже если после него был успех
    (кейс «стрик 15, потом 1 успех» тоже должен ловиться). Возвращаем максимальный стрик
    и признак, что рек до сих пор не ожил (стрик хвостовой)."""
    best = cur = 0
    for f in flags:
        cur = 0 if f else cur + 1
        best = max(best, cur)
    return best >= DEADREQ_STREAK, best, (cur >= DEADREQ_STREAK)


async def _send_deadreq(context, text):
    """Отдельный канал для реков (Head Support: «не солянка»). Если DEADREQ_CHAT_ID
    не задан — fallback в общий алертный поток. Если задан, но отправка упала —
    в общий чат НЕ дублируем (реки в основную группу не слать)."""
    from config import DEADREQ_CHAT_ID
    if DEADREQ_CHAT_ID:
        for attempt in range(3):
            try:
                await context.bot.send_message(chat_id=DEADREQ_CHAT_ID, text=text, parse_mode="HTML")
                return
            except RetryAfter as e:
                await asyncio.sleep(float(getattr(e, "retry_after", 3)) + 1)
            except Exception as e:
                logger.error(f"Ошибка отправки deadreq -> {DEADREQ_CHAT_ID}: {e}")
                return
        return
    await _broadcast(context, text)


def _deadreq_text(r, streak, ongoing, conv):
    """Текст одного рек-алерта. Гейт/провайдер — чтобы саппорт сразу видел, куда идти."""
    bank = f" · {escape(r['bank'])}" if r.get('bank') else ""
    gate = f"🏦 Провайдер: <b>{escape(r['gates'])}</b>\n" if r.get('gates') else ""
    st = "🔴 стрик продолжается" if ongoing else "🟡 после стрика был успех"
    return (f"⚰️ <b>Реквизит со стриком {DEADREQ_STREAK}+ отмен подряд</b>\n\n"
            f"<code>{escape(r['req'])}</code>{bank}\n"
            f"{gate}"
            f"🏪 Мерчи: {escape(r['merchants'])}\n"
            f"📊 За сутки: выдач {r['total']}, конв {conv}%, макс. стрик {streak} — {st}\n\n"
            f"Саппортам: проверить реквизит на стороне провайдера.\n"
            f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")


def _deadreq_kb(req):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Взял в работу", callback_data=f"rq:take:{req}"),
        InlineKeyboardButton("🔄 Проверить", callback_data=f"rq:check:{req}"),
        InlineKeyboardButton("🗑 Закрыть", callback_data=f"rq:close:{req}"),
    ]])


async def _send_deadreq_one(context, req, text):
    """Отправить рек-алерт отдельным сообщением с кнопками. Возвращает message_id или None."""
    from config import DEADREQ_CHAT_ID
    chat = DEADREQ_CHAT_ID or ALERT_CHAT_ID
    for attempt in range(3):
        try:
            msg = await context.bot.send_message(chat_id=chat, text=text, parse_mode="HTML",
                                                 reply_markup=_deadreq_kb(req))
            return msg.message_id, chat
        except RetryAfter as e:
            await asyncio.sleep(float(getattr(e, "retry_after", 3)) + 1)
        except Exception as e:
            logger.error(f"Ошибка отправки deadreq -> {chat}: {e}")
            return None, None
    return None, None


async def check_dead_requisites(context):
    """Каждые 30 мин: реквизиты со стриком 7+ отмен подряд за сутки — ОДИН пуш на рек
    (повтора каждые 12ч больше нет). Маркер deadreq_
    снимается, когда рек ожил (стрик прервался) или затих (нет сделок), тогда при новом
    стрике придёт свежий алерт. Сообщение из группы убирается при затихании."""
    try:
        rows = mb.get_requisite_flags(DEADREQ_DAYS, DEADREQ_MIN_ISSUED)
        state = _load_state()

        # дохлые прямо сейчас (рек -> данные)
        dead_now = {}
        for r in rows:
            dead, streak, ongoing = _dead_req_verdict(r["flags"])
            if dead:
                dead_now[r["req"]] = (r, streak, ongoing)

        # 1) снять маркеры реков, которые больше НЕ в стрике (ожили) или затихли;
        #    убрать висящее сообщение, если по реку нет свежих сделок
        for key in [k for k in list(state) if k.startswith("deadreq_")]:
            req = key[len("deadreq_"):]
            if req in dead_now:
                continue  # всё ещё дохлый — пуш висит, ничего не делаем
            msgkey = f"deadmsg_{req}"
            rec = state.get(msgkey)
            if rec:
                try:
                    act = mb.get_requisite_recent(req, DEADREQ_IDLE_HOURS * 60)
                except Exception:
                    act = {"total": 1}  # не смогли проверить — не трогаем сообщение
                if act["total"] == 0:  # затих — убираем сообщение
                    try:
                        await context.bot.delete_message(chat_id=rec["chat"], message_id=rec["mid"])
                    except Exception:
                        pass
                    state.pop(msgkey, None)
                    state.pop(key, None)
                    logger.info(f"deadreq снят (затих): {req}")
                else:  # ожил (стрик прервался, но трафик идёт) — маркер снимаем, сообщение оставляем как есть
                    state.pop(key, None)
                    logger.info(f"deadreq снят (ожил): {req}")
            else:
                state.pop(key, None)  # сообщения нет (закрыли) — просто чистим маркер

        # 2) новые дохлые — ОДИН пуш, если по реку маркера ещё нет
        fresh = 0
        for r, streak, ongoing in sorted(dead_now.values(), key=lambda x: (not x[2], -x[1]))[:DEADREQ_LIST_CAP]:
            req = r["req"]
            if f"deadreq_{req}" in state:
                continue  # уже алертили, повтора не шлём
            conv = round(r["succ"] * 100.0 / r["total"], 1) if r["total"] else 0
            mid, chat = await _send_deadreq_one(context, req, _deadreq_text(r, streak, ongoing, conv))
            state[f"deadreq_{req}"] = {"label": req, "since": datetime.now().timestamp()}
            if mid:
                state[f"deadmsg_{req}"] = {"req": req, "mid": mid, "chat": chat,
                                          "t": datetime.now().timestamp()}
            fresh += 1
        _save_state(state)
        logger.info(f"check_dead_requisites: в зачёте {len(rows)}, дохлых {len(dead_now)}, новых пушей {fresh}")
    except Exception as e:
        logger.error(f"Ошибка check_dead_requisites: {e}")


def _strip_status(html_text):
    """Отрезать ранее добавленную строку статуса (взял/закрыл), чтобы не плодить дубли."""
    for marker in ("\n\n✅ В работе:", "\n\n🗑 Закрыл:", "\n\n✅ Возмещение отдано:", "\n\n✅ Обнулено:"):
        idx = html_text.find(marker)
        if idx != -1:
            html_text = html_text[:idx]
    return html_text


async def handle_pfmk_callback(update, context):
    """Кнопка под пороговым алертом Echo МК: «возмещение отдано» → обнулить счётчик
    (reset_at=now) и копить заново до следующего порога."""
    q = update.callback_query
    if (getattr(q, "data", "") or "") != "pfmk:reset":
        await q.answer()
        return
    state = _load_state()
    pcfg = state.get("pfmk_counter")
    if not isinstance(pcfg, dict):
        pcfg = {}
    pcfg["reset_at"] = datetime.now(MSK).isoformat()
    pcfg["fired"] = False
    pcfg["mode"] = "daily"
    pcfg.pop("fired_vol", None)
    pcfg.pop("fired_at", None)
    state["pfmk_counter"] = pcfg
    _save_state(state)
    who = q.from_user.first_name or q.from_user.username or str(q.from_user.id)
    ts = datetime.now(MSK).strftime('%d.%m %H:%M')
    try:
        base = _strip_status(q.message.text_html)
        await q.edit_message_text(
            base + f"\n\n✅ Возмещение отдано: {escape(who)} ({ts}) — счётчик обнулён, копим заново",
            parse_mode="HTML")  # без reply_markup → кнопка исчезает
    except Exception:
        pass
    await q.answer("Счётчик сброшен, копим с нуля")


async def handle_vega_callback(update, context):
    """Кнопка под пороговым алертом Vega: обнулить счётчик баланса (reset_at=now) → считать заново."""
    q = update.callback_query
    if (getattr(q, "data", "") or "") != "vega:reset":
        await q.answer()
        return
    state = _load_state()
    state["vega_counter"] = {"reset_at": datetime.now(MSK).isoformat(), "fired": False}
    _save_state(state)
    who = q.from_user.first_name or q.from_user.username or str(q.from_user.id)
    ts = datetime.now(MSK).strftime('%d.%m %H:%M')
    try:
        base = _strip_status(q.message.text_html)
        await q.edit_message_text(
            base + f"\n\n✅ Обнулено: {escape(who)} ({ts}) — счётчик сброшен, считаем заново",
            parse_mode="HTML")  # без reply_markup → кнопка исчезает
    except Exception:
        pass
    await q.answer("Счётчик Vega сброшен, считаем с нуля")


async def handle_req_callback(update, context):
    """Кнопки под рек-алертами в группе реквизитов: взял / проверить / закрыть."""
    q = update.callback_query
    try:
        _, action, req = q.data.split(":", 2)
    except ValueError:
        await q.answer()
        return
    try:
        if action == "take":
            who = q.from_user.first_name or q.from_user.username or str(q.from_user.id)
            base = _strip_status(q.message.text_html)
            try:
                await q.edit_message_text(
                    base + f"\n\n✅ В работе: {escape(who)}",
                    parse_mode="HTML", reply_markup=_deadreq_kb(req))
            except Exception:
                pass
            await q.answer("Отметил, что ты взял в работу")
        elif action == "check":
            act = mb.get_requisite_recent(req, 60)
            if act["total"] == 0:
                txt = f"За последний час по реку 0 заявок — похоже, уже не выдаётся."
            else:
                conv = round(act["succ"] * 100.0 / act["total"]) if act["total"] else 0
                txt = (f"За час: {act['total']} заявок, {act['succ']} оплат "
                       f"({conv}%), отказов {act['failed']}.")
            await q.answer(txt, show_alert=True)
        elif action == "close":
            state = _load_state()
            state.pop(f"deadmsg_{req}", None)
            _save_state(state)
            who = q.from_user.first_name or q.from_user.username or str(q.from_user.id)
            base = _strip_status(q.message.text_html)
            try:
                await q.edit_message_text(
                    base + f"\n\n🗑 <b>Закрыл: {escape(who)}</b>",
                    parse_mode="HTML")  # без reply_markup → кнопки исчезают
            except Exception:
                pass
            await q.answer("Закрыто")
        else:
            await q.answer()
    except Exception as e:
        logger.error(f"Ошибка handle_req_callback: {e}")
        try:
            await q.answer()
        except Exception:
            pass


# ── Мониторинг выплат (пробел: жалоба по выплате скандальнее депозита) ──
WD_STUCK_HOURS = 2
WD_REJECT_WINDOW_MIN = 120
WD_REJECT_MIN = 3


async def check_withdrawals(context):
    """Каждые 30 мин: зависшие выплаты (нефинальный статус > 2ч) и серии reject'ов."""
    try:
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        for r in mb.get_stuck_withdrawals(WD_STUCK_HOURS):
            merch = r.get("merchant") or "—"
            if _is_excluded_merchant(merch):
                continue
            cur = (r.get("cur") or "RUB").upper()
            if _register(state, f"wdstuck_{merch}_{r.get('status')}", merch, hours=3):
                await _broadcast(context,
                    f"🧊 <b>Зависшие выплаты: {escape(merch)}</b>\n\n"
                    f"Статус <code>{escape(str(r.get('status')))}</code>: <b>{r.get('n')}</b> выплат на "
                    f"<b>{_fmt_rub(float(r.get('amount') or 0))} {_CUR_SYM.get(cur, cur)}</b>, "
                    f"самая старая висит ~{r.get('oldest_h')}ч.\n"
                    f"Проверить обработку выплат, пока мерч не написал сам.\n\n⏰ {ts}")
        for r in mb.get_withdrawal_rejects(WD_REJECT_WINDOW_MIN, WD_REJECT_MIN):
            merch = r.get("merchant") or "—"
            if _is_excluded_merchant(merch):
                continue
            cur = (r.get("cur") or "RUB").upper()
            if _register(state, f"wdrej_{merch}", merch, hours=3):
                await _broadcast(context,
                    f"🚫 <b>Серия отказов по выплатам: {escape(merch)}</b>\n\n"
                    f"За {_win_label(WD_REJECT_WINDOW_MIN)}: <b>{r.get('n')}</b> reject на "
                    f"<b>{_fmt_rub(float(r.get('amount') or 0))} {_CUR_SYM.get(cur, cur)}</b>.\n"
                    f"Проверить причину на стороне гейта/баланса.\n\n⏰ {ts}")
        _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка check_withdrawals: {e}")


# ── Относительная просадка гейта vs 7-дневная норма ──
GATE_REL_DROP_PP = 25        # просадка от своей нормы
GATE_REL_MIN_CALLS = 10      # минимум заявок в окне
GATE_REL_MIN_BASE = 200      # минимум заявок в базе за 7д
_GATE_BASE_CACHE = {"ts": 0.0, "data": None}


async def check_gate_relative(context):
    """Каждые 15 мин: гейт ниже СВОЕЙ недельной нормы на 25+ пп (абсолютный порог 10% это не ловит)."""
    try:
        now_ts = datetime.now().timestamp()
        if _GATE_BASE_CACHE["data"] is None or now_ts - _GATE_BASE_CACHE["ts"] > 3600:
            _GATE_BASE_CACHE["data"] = mb.get_gate_baselines(7)
            _GATE_BASE_CACHE["ts"] = now_ts
        base = _GATE_BASE_CACHE["data"] or {}
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        for r in mb.get_recent_gate_conversion(60):
            gate = r.get("gate") or "(?)"
            if gate == "(нет гейта)" or "test" in gate.lower():
                continue
            total = int(r.get("total") or 0)
            succ = int(r.get("success") or 0)
            b = base.get(gate)
            if total < GATE_REL_MIN_CALLS or not b or b["total"] < GATE_REL_MIN_BASE:
                continue
            conv = round(succ * 100.0 / total, 1)
            if b["conv"] - conv >= GATE_REL_DROP_PP:
                if _register(state, f"gaterel_{gate}", gate, hours=3):
                    await _broadcast(context,
                        f"📐 <b>Гейт ниже своей нормы: {escape(gate)}</b>\n\n"
                        f"За час: <b>{conv}%</b> ({succ}/{total}) при норме за 7 дней "
                        f"<b>{b['conv']}%</b> — просадка {round(b['conv']-conv)} пп.\n"
                        f"Абсолютный порог ещё не пробит, но гейт явно деградирует.\n\n⏰ {ts}")
        _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка check_gate_relative: {e}")


# ── Сводка-светофор раз в 3 часа ──
def _tl_icon(conv):
    return "🟢" if conv >= 50 else ("🟡" if conv >= 30 else "🔴")


async def send_traffic_light(context):
    """Раз в 3 часа: светофор по терминалам и эндпоинтам (окно 3ч vs то же окно вчера)."""
    try:
        d = mb.get_traffic_light(180)
        if not d["terminals"] and not d["endpoints"]:
            return
        lines = [f"🚦 <b>Сводка за 3 часа</b> (vs то же окно вчера)", ""]

        def fmt(rows, cap):
            out = []
            for r in rows[:cap]:
                tn, sn = int(r["t_now"]), int(r["s_now"])
                ty, sy = int(r["t_yest"]), int(r["s_yest"])
                cn = round(sn*100.0/tn, 1) if tn else 0
                cy = round(sy*100.0/ty, 1) if ty else 0
                d_pp = f"{'+' if cn >= cy else ''}{round(cn-cy)} пп" if ty else "нов."
                out.append(f"{_tl_icon(cn)} {escape(str(r['name'])[:32])}: <b>{cn}%</b> ({sn}/{tn}) · {d_pp}")
            return out

        lines += ["<b>Терминалы:</b>"] + fmt(d["terminals"], 15)
        lines += ["", "<b>Эндпоинты:</b>"] + fmt(d["endpoints"], 15)
        lines.append(f"\n⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M')}")
        await _broadcast(context, "\n".join(lines))
        logger.info("Светофор отправлен")
    except Exception as e:
        logger.error(f"Ошибка send_traffic_light: {e}")


# ── Надзор за новыми эндпоинтами (первые 48ч после включения) ──
NEWEP_HOURS = 48
NEWEP_MIN_HOUR = 5           # минимум заявок за час для оценки
NEWEP_BAD_CONV = 30          # порог тревоги для нового эндпоинта (STD-логика)


async def check_new_endpoints(context):
    """Каждые 15 мин: новые эндпоинты (продукт создан < 48ч назад). Привет при первом
    трафике + жёсткий присмотр: конверсия часа < 30% при 5+ заявках — пинг сразу."""
    try:
        rows = mb.get_new_endpoints(NEWEP_HOURS)
        if not rows:
            return
        state = _load_state()
        ts = datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        for r in rows:
            ep = r.get("endpoint") or "—"
            merch = r.get("merchant") or "—"
            if _is_excluded_merchant(merch) or "test" in ep.lower():
                continue
            if _register(state, f"newep_seen_{ep}", ep, hours=NEWEP_HOURS):
                await _broadcast(context,
                    f"🆕 <b>Новый эндпоинт пошёл в трафик</b>\n\n"
                    f"{escape(merch)} · <code>{escape(ep)}</code>\n"
                    f"Первые заявки пошли (всего {r.get('total')}). Следим первые {NEWEP_HOURS}ч "
                    f"жёстче обычного.\n\n⏰ {ts}")
            th, sh = int(r.get("t_hour") or 0), int(r.get("s_hour") or 0)
            conv_h = round(sh * 100.0 / th, 1) if th else 0
            is_ftd = "_ftd_" in ep.lower()
            bad = conv_h < (10 if is_ftd else NEWEP_BAD_CONV)
            if th >= NEWEP_MIN_HOUR and bad:
                if _register(state, f"newep_bad_{ep}", ep, hours=2):
                    await _broadcast(context,
                        f"🚨 <b>Новый эндпоинт буксует: {escape(merch)}</b>\n\n"
                        f"<code>{escape(ep)}</code> (включён &lt; {NEWEP_HOURS}ч назад)\n"
                        f"За час: <b>{conv_h}%</b> ({sh}/{th}).\n"
                        f"Первые часы решают впечатление мерча — разобрать сразу.\n\n⏰ {ts}")
        _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка check_new_endpoints: {e}")


# ── Недельный тренд-отчёт по мерчам (понедельник 10:00 МСК) ──
async def send_weekly_trends(context):
    """Понедельник 10:00 МСК: 3 полных недели по мерчам — кто растёт, кто затухает,
    у кого конверсия сползает. Готовая повестка для AM."""
    try:
        rows = mb.get_merchant_weekly(3)
        bym = {}
        for r in rows:
            bym.setdefault(r["merchant"], []).append(r)
        lines = [f"📊 <b>Недельные тренды по мерчам</b> (3 полных недели, пн-вс МСК)", ""]
        items = []
        for m, wk in bym.items():
            if _is_excluded_merchant(m) or len(wk) < 2:
                continue
            wk.sort(key=lambda x: x["wk"])
            last, prev = wk[-1], wk[-2]
            convs = [round(int(w["succ"])*100.0/int(w["total"]), 1) if int(w["total"]) else 0 for w in wk]
            vol_d = (float(last["vol"]) - float(prev["vol"])) / float(prev["vol"]) * 100 if float(prev["vol"]) else 0
            sliding = len(convs) >= 3 and convs[0] > convs[1] > convs[2]
            items.append((m, int(last["total"]), convs, vol_d, sliding))
        items.sort(key=lambda x: -x[1])
        for m, tot, convs, vol_d, sliding in items[:12]:
            trend = " → ".join(f"{c}%" for c in convs)
            mark = " ⚠️ конверсия сползает 3 недели" if sliding else ""
            arrow = "📈" if vol_d >= 0 else "📉"
            lines.append(f"• <b>{escape(m)}</b>: {tot} заявок/нед, конв {trend}, "
                         f"оборот {arrow} {vol_d:+.0f}%{mark}")
        if len(items) == 0:
            return
        lines.append(f"\n⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M')} МСК")
        await _broadcast(context, "\n".join(lines))
        logger.info("Недельный тренд-отчёт отправлен")
    except Exception as e:
        logger.error(f"Ошибка send_weekly_trends: {e}")


# ── Алерт «юзер-плохиш» (выявление на постоянной основе) ──
SPAMUSER_WINDOW_MIN = 60
SPAMUSER_MIN_ORDERS = 5
SPAMUSER_THROTTLE_H = 12


def _spamuser_kb(cust):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Взял в работу", callback_data=f"su:take:{cust}"),
        InlineKeyboardButton("🔄 Проверить", callback_data=f"su:check:{cust}"),
        InlineKeyboardButton("🗑 Закрыть", callback_data=f"su:close:{cust}"),
    ]])


def _spamuser_text(r):
    merch = r.get("merchant") or "—"
    orders = "\n".join(f"   • <code>{escape(o)}</code>" for o in (r.get("orders") or []))
    req_count = int(r.get("req_count") or 0)
    vol = float(r.get("vol") or 0)
    # умная пометка: все заявки на ОДИН реквизит → это скорее дохлый рек, а не плохиш
    if req_count == 1:
        verdict = ("⚠️ Все заявки на ОДИН реквизит — возможно, дело в реквизите "
                   "(не выдаётся/не принимает), а не в клиенте. Стоит проверить рек.")
    else:
        verdict = f"Реквизитов было выдано: {req_count} — клиент брал разные и не платил."
    return (f"👤 <b>Юзер-плохиш: {escape(merch)}</b>\n\n"
            f"Клиент <code>{escape(r['cust'][:13])}</code>: <b>{r['n']}</b> заявок за час, "
            f"ноль оплат (в пендинге: {r.get('pending')}).\n"
            f"Эндпоинт: <code>{escape(r.get('endpoints') or '—')}</code>\n"
            f"Заявлено на сумму: <b>{_fmt_rub(vol)} ₽</b>\n"
            f"{verdict}\n\n"
            f"Примеры ордеров для блока у мерча:\n{orders}\n\n"
            f"⏰ {datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}")


async def check_spam_users(context):
    """Каждые 15 мин: юзер с 5+ заявками за час и нулём оплат — кандидат на блок у мерча.
    По одному сообщению с кнопками + авто-удаление, если клиент затих (нет заявок 3ч)."""
    try:
        from config import DEADREQ_CHAT_ID
        chat = DEADREQ_CHAT_ID or ALERT_CHAT_ID
        state = _load_state()

        # 1) авто-удаление протухших (клиент больше не льёт)
        for key in [k for k in list(state) if k.startswith("spammsg_")]:
            rec = state[key]
            try:
                act = mb.get_user_recent(rec.get("cust"), DEADREQ_IDLE_HOURS * 60)
            except Exception:
                continue
            if act["total"] == 0:
                try:
                    await context.bot.delete_message(chat_id=rec["chat"], message_id=rec["mid"])
                except Exception:
                    pass
                del state[key]

        # 2) новые плохиши
        rows = mb.get_spam_users(SPAMUSER_WINDOW_MIN, SPAMUSER_MIN_ORDERS)
        for r in rows:
            merch = r.get("merchant") or "—"
            if _is_excluded_merchant(merch):
                continue
            # PF MK — алерты отключены: если весь трафик клиента на исключённых
            # эндпоинтах (Echo P2P_MC), юзер-плохиша по нему не шлём
            eps = [e.strip() for e in (r.get("endpoints") or "").split(",") if e.strip()]
            if eps and all(_is_excluded_alert_target(e) for e in eps):
                continue
            if not _register(state, f"spamuser_{r['cust']}", merch, hours=SPAMUSER_THROTTLE_H):
                continue
            for attempt in range(3):
                try:
                    msg = await context.bot.send_message(chat_id=chat, text=_spamuser_text(r),
                            parse_mode="HTML", reply_markup=_spamuser_kb(r['cust']))
                    state[f"spammsg_{r['cust']}"] = {"cust": r['cust'], "mid": msg.message_id,
                            "chat": chat, "t": datetime.now().timestamp()}
                    break
                except RetryAfter as e:
                    await asyncio.sleep(float(getattr(e, "retry_after", 3)) + 1)
                except Exception as e:
                    logger.error(f"Ошибка отправки spamuser -> {chat}: {e}")
                    break
        _save_state(state)
    except Exception as e:
        logger.error(f"Ошибка check_spam_users: {e}")


async def handle_su_callback(update, context):
    """Кнопки под юзер-плохишами: взял / проверить / закрыть."""
    q = update.callback_query
    try:
        _, action, cust = q.data.split(":", 2)
    except ValueError:
        await q.answer()
        return
    try:
        if action == "take":
            who = q.from_user.first_name or q.from_user.username or str(q.from_user.id)
            try:
                await q.edit_message_text(_strip_status(q.message.text_html) + f"\n\n✅ В работе: {escape(who)}",
                    parse_mode="HTML", reply_markup=_spamuser_kb(cust))
            except Exception:
                pass
            await q.answer("Отметил, что ты взял в работу")
        elif action == "check":
            act = mb.get_user_recent(cust, 60)
            if act["total"] == 0:
                txt = "За последний час у клиента 0 заявок — затих."
            else:
                conv = round(act["succ"] * 100.0 / act["total"]) if act["total"] else 0
                txt = f"За час: {act['total']} заявок, {act['succ']} оплат ({conv}%), отказов {act['failed']}."
            await q.answer(txt, show_alert=True)
        elif action == "close":
            state = _load_state()
            state.pop(f"spammsg_{cust}", None)
            _save_state(state)
            who = q.from_user.first_name or q.from_user.username or str(q.from_user.id)
            try:
                await q.edit_message_text(_strip_status(q.message.text_html) + f"\n\n🗑 <b>Закрыл: {escape(who)}</b>",
                    parse_mode="HTML")
            except Exception:
                pass
            await q.answer("Закрыто")
        else:
            await q.answer()
    except Exception as e:
        logger.error(f"Ошибка handle_su_callback: {e}")
        try:
            await q.answer()
        except Exception:
            pass


# ── Алерт «мерч прокидывает тест» (ловить тесты сразу) ──
TEST_WINDOW_MIN = 6          # окно запроса (с запасом к интервалу джобы)


async def check_test_traffic(context):
    """Каждые 2 мин: тестовая заявка от реального мерча — алертим сразу, с эндпоинтом/валютой/суммой.
    Наши внутренние тест-прогоны исключены. Дедуп по id заявки (TTL 6ч)."""
    try:
        rows = mb.get_merchant_test_invoices(TEST_WINDOW_MIN)
        if not rows:
            return
        state = _load_state()
        seen = state.get("test_seen", {})
        now = datetime.now().timestamp()
        seen = {k: v for k, v in seen.items() if now - v < 6 * 3600}  # чистим старше 6ч
        sent = 0
        for r in rows:
            iid = r["id"]
            if iid in seen:
                continue
            seen[iid] = now
            cur = (r.get("cur") or "").upper()
            await _broadcast(context,
                f"🧪 <b>Тест от мерча: {escape(str(r.get('merch')))}</b>\n\n"
                f"<code>{escape(str(r.get('endpoint')))}</code>\n"
                f"Сумма: <b>{_fmt_rub(float(r.get('amt') or 0))} {escape(cur)}</b> · "
                f"статус {escape(str(r.get('status')))} · {escape(str(r.get('t')))}\n"
                f"Мерч прокидывает тест — проверьте приём, поможем если что")
            sent += 1
        state["test_seen"] = seen
        _save_state(state)
        if sent:
            logger.info(f"check_test_traffic: новых тест-заявок {sent}")
    except Exception as e:
        logger.error(f"Ошибка check_test_traffic: {e}")
