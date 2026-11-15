"""Inline-клавиатуры."""
from typing import List

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup)

# Reply (главное меню) — должны совпадать с проверками в bot.py:on_text_message
BTN_REPORT = "Новая выгрузка"
BTN_LAST = "Повторить последнюю"
BTN_HEALTH = "Проверить реплику"
BTN_CANCEL = "Отмена"
BTN_ADMIN = "Админ"


def main_menu(is_owner: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_REPORT)],
        [KeyboardButton(BTN_LAST), KeyboardButton(BTN_HEALTH)],
    ]
    if is_owner:
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def kb_admin() -> InlineKeyboardMarkup:
    """Inline-меню админки — открывается по кнопке 'Админ' (только у OWNER)."""
    rows = [
        [InlineKeyboardButton("Создать одноразовый ключ", callback_data="adm:new")],
        [InlineKeyboardButton("Все ключи", callback_data="adm:keys")],
        [InlineKeyboardButton("Кто имеет доступ", callback_data="adm:users")],
    ]
    return InlineKeyboardMarkup(rows)


def cancel_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(BTN_CANCEL)]], resize_keyboard=True)


# Inline: тип выгрузки
def kb_scope() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("По мерчу", callback_data="sc:merchant")],
        [InlineKeyboardButton("По провайдеру (гейту)", callback_data="sc:gate")],
        [InlineKeyboardButton("По всем мерчам (сводно)", callback_data="sc:all")],
    ]
    return InlineKeyboardMarkup(rows)


# Inline: аудитория
def kb_audience(scope: str) -> InlineKeyboardMarkup:
    if scope == "merchant":
        rows = [
            [InlineKeyboardButton("Для мерча (без gate/terminal)", callback_data="au:merchant")],
            [InlineKeyboardButton("Для провайдера (по гейтам)", callback_data="au:provider")],
            [InlineKeyboardButton("Внутренняя (полная)", callback_data="au:internal")],
        ]
    elif scope == "gate":
        rows = [
            [InlineKeyboardButton("Для провайдера (без merchant_order)", callback_data="au:provider")],
            [InlineKeyboardButton("Внутренняя (полная)", callback_data="au:internal")],
        ]
    else:  # all
        rows = [
            [InlineKeyboardButton("Внутренняя (полная)", callback_data="au:internal")],
            [InlineKeyboardButton("Для провайдера (по гейтам)", callback_data="au:provider")],
        ]
    rows.append([InlineKeyboardButton("Назад", callback_data="back:scope")])
    return InlineKeyboardMarkup(rows)


# Inline: пикер мерча/гейта (страница) + поиск
def kb_picker(items: List[dict], page: int, page_size: int, kind: str) -> InlineKeyboardMarkup:
    """items = [{iname, name?, cnt}], kind: 'm' (merchant) | 'g' (gate)."""
    start = page * page_size
    chunk = items[start:start + page_size]
    rows = []
    for i, it in enumerate(chunk):
        label = it.get("name") or it.get("iname")
        cnt = it.get("cnt") or 0
        cnt_fmt = f"{cnt:,}".replace(",", " ")
        rows.append([InlineKeyboardButton(
            f"{label} — {cnt_fmt}",
            callback_data=f"pk:{kind}:{start + i}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("Назад", callback_data=f"pg:{kind}:{page-1}"))
    nav.append(InlineKeyboardButton(f"стр. {page + 1}", callback_data="noop"))
    if start + page_size < len(items):
        nav.append(InlineKeyboardButton("Вперёд", callback_data=f"pg:{kind}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Поиск по имени", callback_data=f"sr:{kind}")])
    rows.append([InlineKeyboardButton("В начало", callback_data="back:scope")])
    return InlineKeyboardMarkup(rows)


# Inline: период
def kb_period() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Сегодня", callback_data="pd:today"),
         InlineKeyboardButton("Вчера", callback_data="pd:yest")],
        [InlineKeyboardButton("7 дней", callback_data="pd:7"),
         InlineKeyboardButton("30 дней", callback_data="pd:30")],
        [InlineKeyboardButton("Произвольный период (ввод)", callback_data="pd:custom")],
        [InlineKeyboardButton("Назад", callback_data="back:picker")],
    ]
    return InlineKeyboardMarkup(rows)
