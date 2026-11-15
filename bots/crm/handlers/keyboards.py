"""Keyboard builder functions for the Merchant CRM bot."""

from telegram import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from handlers.utils import (
    BTN_TODAY,
    BTN_CANCELED,
    BTN_ALERTS,
    BTN_CONVERSION,
    BTN_LOW_CONVERSION,
    BTN_BAD_CLIENTS,
    BTN_SEARCH,
    BTN_EXPORT,
    BTN_RATES,
    BTN_ADD_MERCHANT,
    BTN_EDIT_MERCHANT,
    BTN_INCIDENTS,
    BTN_NEW_INCIDENT,
    BTN_UPLOAD_EXPORT,
    BTN_ACTIVE_SUMMARY,
    BTN_METHOD_OVERVIEW,
    BTN_ENDPOINT_OVERVIEW,
    BTN_ENDPOINT_DETAIL,
    BTN_SNAPSHOT,
    BTN_ACTIVE_REQ,
    BTN_BEHAVIOR,
    BTN_LASTHOUR,
    BTN_DAILY,
    BTN_LAST_REQS, BTN_TURNOVER, BTN_CONV_VIEWS, BTN_GATE_LIMITS,
    BTN_ADMIN,
)

import database

# ── Incident-list helpers ──────────────────────────────────
INC_PAGE_SIZE = 5
PRIORITY_EMOJI = {'P0': '🔴', 'P1': '🟠', 'P2': '🟡', 'P3': '⚪'}


# ── Keyboards ──────────────────────────────────────────────

def kb_main(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Главное меню: live-мониторинг → аналитика → сводка → админ."""
    buttons = [
        # live-мониторинг
        [KeyboardButton(BTN_SNAPSHOT), KeyboardButton(BTN_LASTHOUR)],
        [KeyboardButton(BTN_ACTIVE_REQ), KeyboardButton(BTN_LOW_CONVERSION)],
        [KeyboardButton(BTN_ENDPOINT_OVERVIEW)],
        # аналитика
        [KeyboardButton(BTN_CONVERSION), KeyboardButton(BTN_BEHAVIOR)],
        [KeyboardButton(BTN_METHOD_OVERVIEW), KeyboardButton(BTN_ENDPOINT_DETAIL)],
        # мерчанты / проблемы
        [KeyboardButton(BTN_SEARCH), KeyboardButton(BTN_ALERTS)],
        # сводка
        [KeyboardButton(BTN_ACTIVE_SUMMARY), KeyboardButton(BTN_DAILY)],
        # drill-down по ордерам
        [KeyboardButton(BTN_LAST_REQS), KeyboardButton(BTN_TURNOVER)],
        [KeyboardButton(BTN_CONV_VIEWS), KeyboardButton(BTN_GATE_LIMITS)],
    ]
    if is_admin:
        buttons.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


def kb_refresh(callback_data: str) -> InlineKeyboardMarkup:
    """Кнопка обновления"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить данные", callback_data=callback_data)]
    ])


def kb_active_req(minutes: int = 15) -> InlineKeyboardMarkup:
    """Переключение окна + обновление для «Активных рек»."""
    def b(lbl, val):
        return InlineKeyboardButton(("• " if val == minutes else "") + lbl,
                                    callback_data=f"active_req:{val}")
    return InlineKeyboardMarkup([
        [b("15м", 15), b("1ч", 60), b("24ч", 1440)],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"active_req:{minutes}")],
    ])


def kb_behavior(hours: int = 24) -> InlineKeyboardMarkup:
    """Переключение периода + обновление для «Поведение юзеров»."""
    def b(lbl, val):
        return InlineKeyboardButton(("• " if val == hours else "") + lbl,
                                    callback_data=f"behavior:{val}")
    return InlineKeyboardMarkup([
        [b("24ч", 24), b("3 дня", 72), b("7 дней", 168)],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"behavior:{hours}")],
    ])


# Периоды для «Эндпоинты детально» (pcode → подпись). td = календарные сутки МСК, остальные — скользящие окна.
EPD_PERIODS = [("1ч", "h1"), ("3ч", "h3"), ("24ч", "h24"), ("Сегодня", "td")]


def _epd_period_row(active_pcode: str, cb_fn):
    return [InlineKeyboardButton(("• " if code == active_pcode else "") + lbl, callback_data=cb_fn(code))
            for lbl, code in EPD_PERIODS]


def kb_epd_picker(merchants: list, pcode: str = "td") -> InlineKeyboardMarkup:
    """Пикер мерчей + переключатель периода для «Эндпоинты детально»."""
    rows = [_epd_period_row(pcode, lambda c: f"epd:pick:{c}")]
    btns = [InlineKeyboardButton(name[:24], callback_data=f"epd:v:{i}:{pcode}")
            for i, name in enumerate(merchants)]
    for i in range(0, len(btns), 2):
        rows.append(btns[i:i + 2])
    rows.append([InlineKeyboardButton("📋 Все мерчи (сводка)", callback_data=f"epd:all:{pcode}")])
    return InlineKeyboardMarkup(rows)


def kb_epd_view(idx: int, pcode: str = "td") -> InlineKeyboardMarkup:
    """Карточка мерча: переключатель периода (рендерит этого же мерча) + по часам + назад к списку."""
    return InlineKeyboardMarkup([
        _epd_period_row(pcode, lambda c: f"epd:v:{idx}:{c}"),
        [InlineKeyboardButton("🕐 По часам (сегодня)", callback_data=f"epd:h:{idx}:{pcode}")],
        [InlineKeyboardButton("⬅️ К списку мерчей", callback_data=f"epd:pick:{pcode}")],
    ])


def kb_epd_hourly(idx: int, pcode: str = "td") -> InlineKeyboardMarkup:
    """Почасовая карточка мерча: назад к карточке / к списку."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Карточка мерча", callback_data=f"epd:v:{idx}:{pcode}")],
        [InlineKeyboardButton("📋 К списку мерчей", callback_data=f"epd:pick:{pcode}")],
    ])


def kb_epd_all(pcode: str = "td") -> InlineKeyboardMarkup:
    """Сводка по всем: переключатель периода + назад к списку."""
    return InlineKeyboardMarkup([
        _epd_period_row(pcode, lambda c: f"epd:all:{c}"),
        [InlineKeyboardButton("⬅️ К списку мерчей", callback_data=f"epd:pick:{pcode}")],
    ])


def kb_export() -> InlineKeyboardMarkup:
    """Меню экспорта"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Мерчанты (CSV)", callback_data="export_merchants")],
        [InlineKeyboardButton("💳 Методы оплаты (CSV)", callback_data="export_methods")],
        [InlineKeyboardButton("🕐 По часам (CSV)", callback_data="export_hourly")],
        [InlineKeyboardButton("📈 Конверсия за неделю (CSV)", callback_data="export_conv_history")],
        [InlineKeyboardButton("🚨 Инциденты за месяц (CSV)", callback_data="export_incidents")],
        [InlineKeyboardButton("📋 Скачать всё (ZIP)", callback_data="export_all")],
    ])


def kb_cancel() -> ReplyKeyboardMarkup:
    """Кнопка отмены"""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("◀️ Вернуться в меню")]],
        resize_keyboard=True
    )


def kb_admin() -> InlineKeyboardMarkup:
    """Админ-панель"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Создать ключ", callback_data="admin_newkey")],
        [InlineKeyboardButton("📋 Список ключей", callback_data="admin_keys")],
        [InlineKeyboardButton("👥 Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton("📊 Статистика ключей", callback_data="admin_stats")],
    ])


def kb_merchant_card(merchant_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Статус", callback_data=f"medit_status:{merchant_id}"),
         InlineKeyboardButton("📝 Заметки", callback_data=f"medit_notes:{merchant_id}")],
        [InlineKeyboardButton("🏷️ Добавить тег", callback_data=f"medit_tag:{merchant_id}"),
         InlineKeyboardButton("📞 Контакт", callback_data=f"medit_contact:{merchant_id}")],
        [InlineKeyboardButton("💰 Ставка", callback_data=f"medit_rate:{merchant_id}"),
         InlineKeyboardButton("📊 История конв.", callback_data=f"mhist:{merchant_id}")],
        [InlineKeyboardButton("🚨 Создать инцидент", callback_data=f"inc_new_from_card:{merchant_id}")],
    ])


def kb_alert(merchant_name: str):
    # CRM-кнопки (инцидент/статус/карточка) убраны — вернём, когда будет мини-апп.
    return None


def kb_incidents_list(incidents: list, page: int = 0) -> InlineKeyboardMarkup:
    total = len(incidents)
    total_pages = (total + INC_PAGE_SIZE - 1) // INC_PAGE_SIZE if total else 1
    page_items = incidents[page * INC_PAGE_SIZE:(page + 1) * INC_PAGE_SIZE]
    rows = []
    for inc in page_items:
        pe = PRIORITY_EMOJI.get(inc.get('priority', 'P2'), '⚪')
        name = inc.get('merchant_name', '')[:15]
        comment_count = database.get_incident_comment_count(inc['id'])
        rows.append([
            InlineKeyboardButton(f"{pe} {name}", callback_data=f"inc_view:{inc['id']}"),
            InlineKeyboardButton("✋", callback_data=f"inc_take:{inc['id']}"),
            InlineKeyboardButton("✅", callback_data=f"inc_close:{inc['id']}"),
            InlineKeyboardButton(f"💬{comment_count}", callback_data=f"inc_comments:{inc['id']}"),
        ])
    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"inc_page:{page - 1}"))
    if total_pages > 1:
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="inc_page_noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"inc_page:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("➕ Создать инцидент", callback_data="inc_create_start")])
    return InlineKeyboardMarkup(rows)


# ── «Последние реки мерча» — drill-down ──────────────────────────
def kb_lr_merchants(merchants: list) -> InlineKeyboardMarkup:
    """Список мерчей (по 2 в ряд). callback: lr:m:<iname>"""
    rows, row = [], []
    for m in merchants:
        label = (m.get("name") or m.get("iname") or "—")[:16]
        row.append(InlineKeyboardButton(label, callback_data=f"lr:m:{m['iname']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def kb_lr_endpoints(merchant_iname: str, endpoints: list) -> InlineKeyboardMarkup:
    """Список эндпоинтов мерча. callback: lr:e:<iname>:<ep>"""
    rows = []
    for e in endpoints:
        ep = e["endpoint"]
        rows.append([InlineKeyboardButton(
            f"{ep} · {e['cnt']}", callback_data=f"lr:e:{merchant_iname}:{ep}"
        )])
    rows.append([InlineKeyboardButton("⬅️ К мерчам", callback_data="lr:back:m")])
    return InlineKeyboardMarkup(rows)


def kb_lr_count(merchant_iname: str, endpoint: str) -> InlineKeyboardMarkup:
    """Выбор количества. callback: lr:c:<iname>:<ep>:<lim>"""
    base = f"lr:c:{merchant_iname}:{endpoint}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("5", callback_data=f"{base}:5"),
         InlineKeyboardButton("10", callback_data=f"{base}:10"),
         InlineKeyboardButton("20", callback_data=f"{base}:20")],
        [InlineKeyboardButton("⬅️ К эндпоинтам", callback_data=f"lr:back:e:{merchant_iname}")],
    ])


def kb_lr_kind(merchant_iname: str, endpoint: str, limit: int) -> InlineKeyboardMarkup:
    """Выбор фильтра. kind: y=issued (выдан рек), n=not_issued, a=all"""
    base = f"lr:s:{merchant_iname}:{endpoint}:{limit}"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🟡 Рек выдан", callback_data=f"{base}:y")],
        [InlineKeyboardButton("⚪ Рек не выдан", callback_data=f"{base}:n")],
        [InlineKeyboardButton("📋 Просто последние", callback_data=f"{base}:a")],
        [InlineKeyboardButton("⬅️ К количеству", callback_data=f"lr:back:c:{merchant_iname}:{endpoint}")],
    ])


def kb_lr_list(merchant_iname: str, endpoint: str, limit: int, kind: str) -> InlineKeyboardMarkup:
    """Список ордеров — навигация."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Обновить",
                              callback_data=f"lr:s:{merchant_iname}:{endpoint}:{limit}:{kind}")],
        [InlineKeyboardButton("⬅️ Сменить фильтр",
                              callback_data=f"lr:c:{merchant_iname}:{endpoint}:{limit}"),
         InlineKeyboardButton("🧾 К мерчам", callback_data="lr:back:m")],
    ])


# ── Оборот по эндпоинту: мерч → эндпоинт → период → оборот + конв в успех ──
TOV_PERIODS = [("Сутки", "d0"), ("7 дней", "d7"), ("30 дней", "d30")]


def kb_tov_merchants(merchants: list) -> InlineKeyboardMarkup:
    """Список мерчей (по 2 в ряд). callback: tov:m:<iname>"""
    rows, row = [], []
    for m in merchants:
        label = (m.get("name") or m.get("iname") or "—")[:16]
        row.append(InlineKeyboardButton(label, callback_data=f"tov:m:{m['iname']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def kb_tov_endpoints(merchant_iname: str, endpoints: list) -> InlineKeyboardMarkup:
    """Список эндпоинтов мерча. callback: tov:e:<iname>:<ep>"""
    rows = []
    for e in endpoints:
        ep = e["endpoint"]
        rows.append([InlineKeyboardButton(
            f"{ep} · {e['cnt']}", callback_data=f"tov:e:{merchant_iname}:{ep}"
        )])
    rows.append([InlineKeyboardButton("⬅️ К мерчам", callback_data="tov:back:m")])
    return InlineKeyboardMarkup(rows)


def _tov_period_row(merchant_iname: str, endpoint: str, active: str = "") -> list:
    return [InlineKeyboardButton(("• " if code == active else "") + lbl,
                                 callback_data=f"tov:s:{merchant_iname}:{endpoint}:{code}")
            for lbl, code in TOV_PERIODS]


def kb_tov_period(merchant_iname: str, endpoint: str) -> InlineKeyboardMarkup:
    """Выбор периода (первый показ). callback: tov:s:<iname>:<ep>:<pcode>"""
    return InlineKeyboardMarkup([
        _tov_period_row(merchant_iname, endpoint),
        [InlineKeyboardButton("⬅️ К эндпоинтам", callback_data=f"tov:back:e:{merchant_iname}")],
    ])


def kb_tov_result(merchant_iname: str, endpoint: str, pcode: str) -> InlineKeyboardMarkup:
    """Результат: переключатель периода + назад."""
    return InlineKeyboardMarkup([
        _tov_period_row(merchant_iname, endpoint, pcode),
        [InlineKeyboardButton("⬅️ К эндпоинтам", callback_data=f"tov:back:e:{merchant_iname}"),
         InlineKeyboardButton("💸 К мерчам", callback_data="tov:back:m")],
    ])


# ── Конверсии (наша / мерча / без дублей): мерч → период → карточка ──
def kb_cv_merchants(merchants: list) -> InlineKeyboardMarkup:
    """Список мерчей. callback: cv:m:<iname>"""
    rows, row = [], []
    for m in merchants:
        label = (m.get("name") or m.get("iname") or "—")[:16]
        row.append(InlineKeyboardButton(label, callback_data=f"cv:m:{m['iname']}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def kb_cv_result(merchant_iname: str, pcode: str = "d0") -> InlineKeyboardMarkup:
    """Карточка конверсий: переключатель периода + назад к мерчам."""
    row = [InlineKeyboardButton(("• " if code == pcode else "") + lbl,
                                callback_data=f"cv:s:{merchant_iname}:{code}")
           for lbl, code in TOV_PERIODS]
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton("📑 Ошибки в Excel", callback_data=f"er:{merchant_iname}:{pcode}"),
         InlineKeyboardButton("🚫 Фрод-юзеры", callback_data=f"fr:{merchant_iname}:{pcode}")],
        [InlineKeyboardButton("🔢 К мерчам", callback_data="cv:back:m")],
    ])
