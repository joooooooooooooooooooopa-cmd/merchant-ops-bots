"""
Сборка Excel-отчётов в трёх шаблонах:

  merchant — ДЛЯ МЕРЧА. Без terminal/gate/external_id (см. feedback_merchant_file_no_provider).
              Колонки: время, merchant_order, uuid, продукт, метод, валюта, сумма,
              оплачено, статус, код+текст ошибки.
  provider — ДЛЯ ПРОВАЙДЕРА. По гейту/терминалу/external_id. БЕЗ merchant_order.
              Колонки: время, external_id, uuid, gate, terminal, метод, валюта, суммы,
              статус, код+текст ошибки.
  internal — ДЛЯ НАС/Lead/Head Support. Всё подряд: мерч + продукт + метод + gate + terminal +
              external_id + merchant_order + uuid + суммы + статус + ошибка + ставка.

Листы (для всех аудиторий):
  1) Параметры — мета (объект/период/комментарии по формулам)
  2) Сводка — KPI по валютам, разбивка по методам/продуктам/часам [+ по гейтам для provider/internal]
  3) По дням — Дата, Заявок, Оплачено, Конв %, Оборот + мини-бар + встроенный LineChart
  4) Анализ причин — поведение (юзер/антифрод/тех) + топ-кодов + по эндпоинтам (internal/provider)
  5) Транзакции — потранзакционка с цветной маркировкой статусов
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import db
from config import OUTPUT_DIR

log = logging.getLogger(__name__)

# ── Стили ──────────────────────────────────────────────────────
COLOR_HEADER = "4C8BF5"
COLOR_HEADER_FG = "FFFFFF"
COLOR_GROUP = "1F2937"
COLOR_GROUP_FG = "FFFFFF"
COLOR_SUCCESS = "D1FAE5"   # complete
COLOR_FAIL = "FECACA"      # failed
COLOR_PENDING = "FEF3C7"   # pending
COLOR_NEW = "E5E7EB"       # new
COLOR_ZEBRA = "F8FAFC"

THIN = Side(border_style="thin", color="D1D5DB")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

HEADER_FONT = Font(name="Calibri", size=11, bold=True, color=COLOR_HEADER_FG)
GROUP_FONT = Font(name="Calibri", size=11, bold=True, color=COLOR_GROUP_FG)
BODY_FONT = Font(name="Calibri", size=10)
TOTAL_FONT = Font(name="Calibri", size=10, bold=True)

CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
RIGHT = Alignment(horizontal="right", vertical="center")

# ── Конфиг колонок на 3 аудитории ───────────────────────────────
# (header, sql_key, width, fmt)   fmt: 'int' | 'money' | 'dt' | None
COLS_MERCHANT = [
    ("Создано (МСК)",   "created_at_msk",   18, "dt"),
    ("Завершено (МСК)", "finalized_at_msk", 18, "dt"),
    ("merchant_order",  "merchant_order",   30, None),
    ("UUID",            "uuid",             36, None),
    ("Продукт",         "product",          26, None),
    ("Метод",           "method",           12, None),
    ("Валюта",          "currency",          8, None),
    ("Сумма",           "price",            14, "money"),
    ("Оплачено",        "amount_paid",      14, "money"),
    ("Статус",          "status_label",     14, None),
    ("Код ошибки",      "error_code",       18, None),
    ("Текст ошибки",    "error_text",       50, None),
]

COLS_PROVIDER = [
    ("Создано (МСК)",   "created_at_msk",   18, "dt"),
    ("Завершено (МСК)", "finalized_at_msk", 18, "dt"),
    ("external_id",     "external_id",      24, None),
    ("UUID",            "uuid",             36, None),
    ("Терминал",        "terminal",         34, None),
    ("Метод",           "method",           12, None),
    ("Валюта",          "currency",          8, None),
    ("Сумма",           "price",            14, "money"),
    ("Оплачено",        "amount_paid",      14, "money"),
    ("Статус",          "status_label",     14, None),
    ("Код ошибки",      "error_code",       18, None),
    ("Текст ошибки",    "error_text",       50, None),
]

COLS_INTERNAL = [
    ("Создано (МСК)",   "created_at_msk",   18, "dt"),
    ("Завершено (МСК)", "finalized_at_msk", 18, "dt"),
    ("Мерчант",         "merchant",         18, None),
    ("merchant_order",  "merchant_order",   30, None),
    ("UUID",            "uuid",             36, None),
    ("external_id",     "external_id",      24, None),
    ("Продукт",         "product",          26, None),
    ("Метод",           "method",           12, None),
    ("Гейт",            "gate",             18, None),
    ("Терминал",        "terminal",         34, None),
    ("Валюта",          "currency",          8, None),
    ("Сумма",           "price",            14, "money"),
    ("Оплачено",        "amount_paid",      14, "money"),
    ("Статус",          "status_label",     14, None),
    ("Код ошибки",      "error_code",       18, None),
    ("Текст ошибки",    "error_text",       50, None),
    ("fee_from_merch",  "fee_from_merchant", 14, "money"),
]

COLS_BY_AUDIENCE = {
    "merchant": COLS_MERCHANT,
    "provider": COLS_PROVIDER,
    "internal": COLS_INTERNAL,
}


# ── Утилиты форматирования ─────────────────────────────────────
def _status_fill(status_id: Optional[int]) -> Optional[PatternFill]:
    if status_id in (3, 6):
        return PatternFill("solid", fgColor=COLOR_SUCCESS)
    if status_id == 4:
        return PatternFill("solid", fgColor=COLOR_FAIL)
    if status_id == 2:
        return PatternFill("solid", fgColor=COLOR_PENDING)
    if status_id == 5:
        return PatternFill("solid", fgColor=COLOR_NEW)
    return None


def _row_cell_value(tx: dict, key: str):
    if key == "status_label":
        return db.status_label(tx.get("status_id"))
    return tx.get(key)


def _apply_money_fmt(cell, currency: Optional[str] = None):
    code = (currency or "").upper()
    fmt = "#,##0.00"
    if code:
        suffix = {
            "RUB": " ₽", "TRY": " ₺", "KZT": " ₸", "UZS": " soʻm",
            "USD": " $", "EUR": " €", "AMD": " ֏", "AZN": " ₼", "TJS": " SM",
        }.get(code, f" {code}")
        fmt = f"#,##0.00\"{suffix}\""
    cell.number_format = fmt


def _write_header(ws, headers: List[str]) -> None:
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = HEADER_FONT
        cell.fill = PatternFill("solid", fgColor=COLOR_HEADER)
        cell.alignment = CENTER
        cell.border = BORDER
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"


def _autowidth(ws, cols: List[tuple]) -> None:
    for c, spec in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(c)].width = spec[2]


def _section(ws, row: int, title: str) -> int:
    cell = ws.cell(row=row, column=1, value=title)
    cell.font = GROUP_FONT
    cell.fill = PatternFill("solid", fgColor=COLOR_GROUP)
    return row + 1


def _table_header(ws, row: int, headers: List[str]) -> int:
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font = HEADER_FONT
        cell.fill = PatternFill("solid", fgColor=COLOR_HEADER)
        cell.alignment = CENTER
        cell.border = BORDER
    return row + 1


def _anon_product(name: Optional[str]) -> str:
    """Анонимизация имени продукта для выгрузки провайдеру: отрезаем первый токен
    (имя мерча). Пример: 'Alpha_RUB_Trans_AM_STD_In' -> 'RUB_Trans_AM_STD_In'.
    Имена без '_' оставляем как есть."""
    if not name:
        return "—"
    s = str(name)
    if "_" in s:
        return s.split("_", 1)[1]
    return s


def _bar(value: int, max_value: int, width: int = 16) -> str:
    if max_value <= 0:
        return ""
    n = max(0, min(width, round(value * width / max_value)))
    return "█" * n + "░" * (width - n)


# ── Лист 1: Параметры ──────────────────────────────────────────
def _sheet_meta(wb: Workbook, scope: str, key: Optional[str], audience: str,
                d_from: date, d_to: date, total_rows: int) -> None:
    ws = wb.create_sheet("Параметры", 0)
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 70

    def put(r, k, v):
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        ws.cell(row=r, column=2, value=v)

    # Для provider audience не светим имя мерча даже если выборка была по нему.
    if audience == "provider" and scope == "merchant":
        obj_label = "Выборка по одному мерчу (скрыт)"
    elif audience == "provider" and scope == "all":
        obj_label = "Все мерчанты (имена скрыты)"
    else:
        obj_label = {
            "merchant": f"Мерчант: {key}",
            "gate":     f"Гейт: {key}",
            "all":      "Все мерчанты",
        }.get(scope, scope)

    aud_label = {
        "merchant": "Для мерча (без gate/terminal/external_id)",
        "provider": "Для провайдера (без merchant_order)",
        "internal": "Внутренняя (полная, со ставкой)",
    }.get(audience, audience)

    total_fmt = f"{total_rows:,}".replace(",", " ")

    put(1, "Отчёт", "ReportBot — выгрузка по заявкам")
    put(2, "Сгенерировано", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    put(3, "Аудитория", aud_label)
    put(4, "Объект", obj_label)
    put(5, "Период (МСК)", f"{d_from.isoformat()} — {d_to.isoformat()}")
    put(6, "Транзакций", total_fmt)
    put(7, "Фильтр", "is_test=false + блок тестовых мерчей (NOTEST_FULL)")
    put(8, "Конверсия",
        "Конв. в оплату = success / выдача (eff.); "
        "конв. от всех = success / total. "
        "Для потоков без выдачи (Havale и т.п.) eff. выдача = total, "
        "чтобы не было ложных 100%.")
    put(9, "Время", "Все даты — Europe/Moscow (БД хранится в UTC, конвертация в SQL).")


# ── Лист 2: Сводка ─────────────────────────────────────────────
def _sheet_summary(wb: Workbook, kpi: List[dict], by_method: List[dict],
                   by_product: List[dict], by_hour: List[dict],
                   by_gate: Optional[List[dict]], audience: str,
                   title: str) -> None:
    ws = wb.create_sheet("Сводка")
    ws.column_dimensions["A"].width = 26
    for c in range(2, 12):
        ws.column_dimensions[get_column_letter(c)].width = 16

    row = 1
    ws.cell(row=row, column=1, value=title).font = Font(bold=True, size=13)
    row += 2

    # KPI по валютам
    row = _section(ws, row, "KPI по валютам")
    row = _table_header(ws, row, [
        "Валюта", "Заявок", "Выдач (eff.)", "Успехов",
        "Конв. в оплату (S/выдача)", "Конв. от всех (S/всего)",
        "Оборот (сумма успехов)", "Сумма запросов",
    ])
    for k in kpi:
        cur = (k.get("cur") or "?").upper()
        ws.cell(row=row, column=1, value=cur).font = TOTAL_FONT
        ws.cell(row=row, column=2, value=int(k.get("total") or 0)).number_format = "#,##0"
        ws.cell(row=row, column=3, value=int(k.get("eff_issued") or 0)).number_format = "#,##0"
        ws.cell(row=row, column=4, value=int(k.get("success") or 0)).number_format = "#,##0"
        c5 = ws.cell(row=row, column=5, value=float(k.get("conv_to_pay") or 0) / 100)
        c5.number_format = "0.00%"
        c6 = ws.cell(row=row, column=6, value=float(k.get("conv_total") or 0) / 100)
        c6.number_format = "0.00%"
        c7 = ws.cell(row=row, column=7, value=float(k.get("paid_sum") or 0))
        _apply_money_fmt(c7, cur)
        c8 = ws.cell(row=row, column=8, value=float(k.get("price_sum") or 0))
        _apply_money_fmt(c8, cur)
        for c in range(1, 9):
            ws.cell(row=row, column=c).border = BORDER
        row += 1
    row += 2

    # По методам
    row = _section(ws, row, "По методам")
    row = _table_header(ws, row, [
        "Метод", "Валюта", "Заявок", "Выдач (eff.)", "Успехов",
        "Конв. в опл", "Конв. от всех", "Оборот",
    ])
    for m in by_method:
        ws.cell(row=row, column=1, value=m.get("method") or "не определён")
        ws.cell(row=row, column=2, value=(m.get("currency") or "?").upper())
        ws.cell(row=row, column=3, value=int(m.get("total") or 0)).number_format = "#,##0"
        ws.cell(row=row, column=4, value=int(m.get("eff_issued") or 0)).number_format = "#,##0"
        ws.cell(row=row, column=5, value=int(m.get("success") or 0)).number_format = "#,##0"
        c6 = ws.cell(row=row, column=6, value=float(m.get("conv_to_pay") or 0) / 100)
        c6.number_format = "0.00%"
        c7 = ws.cell(row=row, column=7, value=float(m.get("conv_total") or 0) / 100)
        c7.number_format = "0.00%"
        c8 = ws.cell(row=row, column=8, value=float(m.get("paid_sum") or 0))
        _apply_money_fmt(c8, m.get("currency"))
        for c in range(1, 9):
            ws.cell(row=row, column=c).border = BORDER
        row += 1
    row += 2

    # По продуктам (эндпоинтам) — для provider скрываем (эндпоинт = сущность мерча).
    if audience != "provider":
        row = _section(ws, row, "По продуктам (эндпоинтам)")
        row = _table_header(ws, row, [
            "Продукт", "Валюта", "Заявок", "Выдач (eff.)", "Успехов",
            "Конв. в опл", "Конв. от всех", "Оборот",
        ])
        for p in by_product:
            ws.cell(row=row, column=1, value=p.get("product") or "—")
            ws.cell(row=row, column=2, value=(p.get("currency") or "?").upper())
            ws.cell(row=row, column=3, value=int(p.get("total") or 0)).number_format = "#,##0"
            ws.cell(row=row, column=4, value=int(p.get("eff_issued") or 0)).number_format = "#,##0"
            ws.cell(row=row, column=5, value=int(p.get("success") or 0)).number_format = "#,##0"
            c6 = ws.cell(row=row, column=6, value=float(p.get("conv_to_pay") or 0) / 100)
            c6.number_format = "0.00%"
            c7 = ws.cell(row=row, column=7, value=float(p.get("conv_total") or 0) / 100)
            c7.number_format = "0.00%"
            c8 = ws.cell(row=row, column=8, value=float(p.get("paid_sum") or 0))
            _apply_money_fmt(c8, p.get("currency"))
            for c in range(1, 9):
                ws.cell(row=row, column=c).border = BORDER
            row += 1
        row += 2

    # По часам
    row = _section(ws, row, "По часам (МСК, агрегат за период)")
    row = _table_header(ws, row, [
        "Час", "Заявок", "Выдач (eff.)", "Успехов",
        "Конв. в опл", "Конв. от всех", "Оборот",
    ])
    for h in by_hour:
        ws.cell(row=row, column=1, value=f"{int(h.get('hour') or 0):02d}:00")
        ws.cell(row=row, column=2, value=int(h.get("total") or 0)).number_format = "#,##0"
        ws.cell(row=row, column=3, value=int(h.get("eff_issued") or 0)).number_format = "#,##0"
        ws.cell(row=row, column=4, value=int(h.get("success") or 0)).number_format = "#,##0"
        c5 = ws.cell(row=row, column=5, value=float(h.get("conv_to_pay") or 0) / 100)
        c5.number_format = "0.00%"
        c6 = ws.cell(row=row, column=6, value=float(h.get("conv_total") or 0) / 100)
        c6.number_format = "0.00%"
        c7 = ws.cell(row=row, column=7, value=float(h.get("paid_sum") or 0))
        c7.number_format = "#,##0.00"
        for c in range(1, 8):
            ws.cell(row=row, column=c).border = BORDER
        row += 1
    row += 2

    # По гейтам/терминалам:
    #  - internal: показываем обе колонки (внутренний разрез);
    #  - provider: только терминалы (своё), колонку "Гейт" не светим;
    #  - merchant: вообще не показываем.
    if by_gate is not None and audience == "internal":
        row = _section(ws, row, "По гейтам / терминалам")
        row = _table_header(ws, row, [
            "Гейт", "Терминал", "Валюта", "Заявок", "Выдач (eff.)",
            "Успехов", "Конв. в опл", "Оборот",
        ])
        for g in by_gate:
            ws.cell(row=row, column=1, value=g.get("gate") or "—")
            ws.cell(row=row, column=2, value=g.get("terminal") or "—")
            ws.cell(row=row, column=3, value=(g.get("currency") or "?").upper())
            ws.cell(row=row, column=4, value=int(g.get("total") or 0)).number_format = "#,##0"
            ws.cell(row=row, column=5, value=int(g.get("eff_issued") or 0)).number_format = "#,##0"
            ws.cell(row=row, column=6, value=int(g.get("success") or 0)).number_format = "#,##0"
            c7 = ws.cell(row=row, column=7, value=float(g.get("conv_to_pay") or 0) / 100)
            c7.number_format = "0.00%"
            c8 = ws.cell(row=row, column=8, value=float(g.get("paid_sum") or 0))
            _apply_money_fmt(c8, g.get("currency"))
            for c in range(1, 9):
                ws.cell(row=row, column=c).border = BORDER
            row += 1
    elif by_gate is not None and audience == "provider":
        # Аггрегируем по терминалам (валюте), скрывая колонку "Гейт".
        from collections import defaultdict
        agg = defaultdict(lambda: {"total": 0, "success": 0, "eff_issued": 0, "paid_sum": 0.0})
        for g in by_gate:
            k = (g.get("terminal") or "—", (g.get("currency") or "?").upper())
            a = agg[k]
            a["total"] += int(g.get("total") or 0)
            a["success"] += int(g.get("success") or 0)
            a["eff_issued"] += int(g.get("eff_issued") or 0)
            a["paid_sum"] += float(g.get("paid_sum") or 0)
        row = _section(ws, row, "По терминалам")
        row = _table_header(ws, row, [
            "Терминал", "Валюта", "Заявок", "Выдач (eff.)",
            "Успехов", "Конв. в опл", "Оборот",
        ])
        rows_sorted = sorted(agg.items(), key=lambda kv: kv[1]["total"], reverse=True)
        for (terminal, cur), a in rows_sorted:
            ws.cell(row=row, column=1, value=terminal)
            ws.cell(row=row, column=2, value=cur)
            ws.cell(row=row, column=3, value=a["total"]).number_format = "#,##0"
            ws.cell(row=row, column=4, value=a["eff_issued"]).number_format = "#,##0"
            ws.cell(row=row, column=5, value=a["success"]).number_format = "#,##0"
            conv = (a["success"] / a["eff_issued"]) if a["eff_issued"] else 0.0
            c6 = ws.cell(row=row, column=6, value=conv)
            c6.number_format = "0.00%"
            c7 = ws.cell(row=row, column=7, value=a["paid_sum"])
            _apply_money_fmt(c7, cur)
            for c in range(1, 8):
                ws.cell(row=row, column=c).border = BORDER
            row += 1


# ── Лист 3: По дням ────────────────────────────────────────────
def _sheet_by_days(wb: Workbook, rows: List[dict]) -> None:
    """Стандарт owner: Дата | Заявок | Опл | Конв % | Оборот + мини-бар + LineChart."""
    ws = wb.create_sheet("По дням")
    ws.column_dimensions["A"].width = 13
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 14
    ws.column_dimensions["E"].width = 18
    ws.column_dimensions["F"].width = 22
    ws.column_dimensions["G"].width = 10

    _write_header(ws, [
        "Дата", "Заявок", "Оплачено", "Конверсия %", "Оборот",
        "Объём (заявки)", "Валюта",
    ])

    max_total = max((int(r.get("total") or 0) for r in rows), default=1)
    cutoff_idx = max(0, len(rows) - 3)  # последние 3 строки подсвечиваем

    for ridx, r in enumerate(rows, start=2):
        day = r.get("day")
        ws.cell(row=ridx, column=1, value=day).number_format = "yyyy-mm-dd"
        c2 = ws.cell(row=ridx, column=2, value=int(r.get("total") or 0))
        c2.number_format = "#,##0"
        c3 = ws.cell(row=ridx, column=3, value=int(r.get("success") or 0))
        c3.number_format = "#,##0"
        c4 = ws.cell(row=ridx, column=4, value=float(r.get("conv") or 0) / 100)
        c4.number_format = "0.0%"
        c5 = ws.cell(row=ridx, column=5, value=float(r.get("paid_sum") or 0))
        _apply_money_fmt(c5, r.get("currency"))
        # Мини-бар — только если есть несколько строк (на одной дне он бессмыслен)
        if len(rows) >= 2:
            bar = _bar(int(r.get("total") or 0), max_total, width=16)
            ws.cell(row=ridx, column=6, value=bar).font = Font(name="Consolas", size=10)
        ws.cell(row=ridx, column=7, value=(r.get("currency") or "?").upper())
        for c in range(1, 8):
            ws.cell(row=ridx, column=c).border = BORDER
            if (ridx - 2) >= cutoff_idx and len(rows) >= 4:
                ws.cell(row=ridx, column=c).fill = PatternFill("solid", fgColor=COLOR_ZEBRA)

    if len(rows) >= 2:
        chart = LineChart()
        chart.title = "Объём по дням"
        chart.y_axis.title = "Заявки / Оплачено"
        chart.x_axis.title = "Дата"
        data = Reference(ws, min_col=2, min_row=1, max_col=3, max_row=1 + len(rows))
        cats = Reference(ws, min_col=1, min_row=2, max_row=1 + len(rows))
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        chart.height = 10
        chart.width = 22
        ws.add_chart(chart, "I2")


# ── Лист 4: Анализ причин ──────────────────────────────────────
def _sheet_behavior(wb: Workbook, beh: dict, audience: str) -> None:
    ws = wb.create_sheet("Анализ причин")
    ws.column_dimensions["A"].width = 26
    for c in range(2, 10):
        ws.column_dimensions[get_column_letter(c)].width = 14

    row = 1
    ws.cell(row=row, column=1, value="Поведение / причины отказа").font = Font(bold=True, size=13)
    row += 2

    t = beh.get("totals") or {}
    total = int(t.get("total") or 0) or 1
    paid = int(t.get("paid") or 0)
    closed = int(t.get("closed_form") or 0)
    idle = int(t.get("idle") or 0)
    af = int(t.get("antifraud") or 0)
    tech = int(t.get("tech") or 0)
    pend = int(t.get("pending") or 0)
    user_bad = closed + idle

    row = _table_header(ws, row, ["Категория", "Заявок", "% от всех", "Комментарий"])
    cats = [
        ("Оплачено",              paid,     "complete + partial_complete"),
        ("Юзер закрыл/протупил",  user_bad, "CANCELLED, validation_error, SESSION_EXPIRED"),
        ("Антифрод",              af,       "ROUTING_ERROR (наша автопроверка, часто бот)"),
        ("Технические / гейт",    tech,     "COMMON, provider_error, provider_request_timeout"),
        ("Pending без кода",      pend,     "Создано/не дошло до кода ошибки"),
    ]
    for label, n, comment in cats:
        ws.cell(row=row, column=1, value=label)
        ws.cell(row=row, column=2, value=n).number_format = "#,##0"
        c3 = ws.cell(row=row, column=3, value=n / total)
        c3.number_format = "0.0%"
        ws.cell(row=row, column=4, value=comment)
        for c in range(1, 5):
            ws.cell(row=row, column=c).border = BORDER
        row += 1
    row += 2

    row = _section(ws, row, "Топ кодов ошибок")
    cols = ["Код", "Категория"]
    # Колонку "Гейт" показываем ТОЛЬКО для internal — провайдеру и мерчу её не светим.
    if audience == "internal":
        cols += ["Гейт"]
    cols += ["Заявок", "% отказов"]
    row = _table_header(ws, row, cols)
    fails = max(1, total - paid)
    for c in beh.get("top_codes") or []:
        code = c.get("code") or "(пусто)"
        n = int(c.get("n") or 0)
        gate = c.get("gate") or "—"
        cat = "—"
        if code in db.USER_CODES:
            cat = "Юзер"
        elif code in db.ANTIFRAUD_CODES:
            cat = "Антифрод"
        elif code in db.TECH_CODES:
            cat = "Тех / гейт"
        elif code == "(пусто)":
            cat = "Pending"
        col = 1
        ws.cell(row=row, column=col, value=code); col += 1
        ws.cell(row=row, column=col, value=cat); col += 1
        if audience == "internal":
            ws.cell(row=row, column=col, value=gate); col += 1
        ws.cell(row=row, column=col, value=n).number_format = "#,##0"; col += 1
        c_pct = ws.cell(row=row, column=col, value=n / fails)
        c_pct.number_format = "0.0%"
        for cc in range(1, col + 1):
            ws.cell(row=row, column=cc).border = BORDER
        row += 1
    row += 2

    if audience == "internal":
        # internal — внутренняя, всё видно. С колонкой "Мерчант".
        row = _section(ws, row, "По мерчам / эндпоинтам (топ-40)")
        row = _table_header(ws, row, [
            "Мерчант", "Эндпоинт", "Заявок", "Опл %", "Юзер %", "Тех %", "Антифрод %",
        ])
        for r in beh.get("by_endpoint") or []:
            tot = int(r.get("total") or 0) or 1
            p_ = int(r.get("paid") or 0)
            cf = int(r.get("closed_form") or 0)
            idl = int(r.get("idle") or 0)
            af_ = int(r.get("antifraud") or 0)
            tech_ = int(r.get("tech") or 0)
            ws.cell(row=row, column=1, value=r.get("merchant") or "—")
            ws.cell(row=row, column=2, value=r.get("endpoint") or "—")
            ws.cell(row=row, column=3, value=int(r.get("total") or 0)).number_format = "#,##0"
            ws.cell(row=row, column=4, value=p_ / tot).number_format = "0.0%"
            ws.cell(row=row, column=5, value=(cf + idl) / tot).number_format = "0.0%"
            ws.cell(row=row, column=6, value=tech_ / tot).number_format = "0.0%"
            ws.cell(row=row, column=7, value=af_ / tot).number_format = "0.0%"
            for c in range(1, 8):
                ws.cell(row=row, column=c).border = BORDER
            row += 1
    # provider — секцию "По эндпоинтам" не показываем (эндпоинт = сущность мерча).


# ── Лист 5: Транзакции ─────────────────────────────────────────
def _sheet_transactions(wb: Workbook, txs: List[dict], audience: str) -> None:
    cols = COLS_BY_AUDIENCE[audience]
    ws = wb.create_sheet("Транзакции")
    _write_header(ws, [c[0] for c in cols])
    _autowidth(ws, cols)

    for ridx, tx in enumerate(txs, start=2):
        status_id = tx.get("status_id")
        fill = _status_fill(status_id)
        for cidx, (_header, key, _w, fmt) in enumerate(cols, start=1):
            cell = ws.cell(row=ridx, column=cidx, value=_row_cell_value(tx, key))
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = LEFT
            if fmt == "dt":
                cell.number_format = "yyyy-mm-dd hh:mm:ss"
            elif fmt == "money":
                _apply_money_fmt(cell, tx.get("currency"))
                cell.alignment = RIGHT
            elif fmt == "int":
                cell.number_format = "#,##0"
                cell.alignment = RIGHT
            if fill:
                cell.fill = fill
    ws.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{max(1, len(txs) + 1)}"


# ── Главный entrypoint ─────────────────────────────────────────
def build(scope: str,
          key: Optional[str],
          audience: str,
          d_from: date,
          d_to: date,
          out_dir: Optional[Path] = None) -> Path:
    """
    Собирает Excel и возвращает путь к файлу.

    scope:    'merchant' | 'gate' | 'all'
    key:      iname мерча / iname гейта / None
    audience: 'merchant' | 'provider' | 'internal'
    """
    if audience not in COLS_BY_AUDIENCE:
        raise ValueError(f"unknown audience: {audience}")
    if scope not in ("merchant", "gate", "all"):
        raise ValueError(f"unknown scope: {scope}")

    log.info(f"build: scope={scope} key={key} audience={audience} {d_from}->{d_to}")

    txs = db.fetch_transactions(scope, key, d_from, d_to)
    kpi = db.summary_kpi(scope, key, d_from, d_to)
    by_method = db.summary_by_method(scope, key, d_from, d_to)
    by_product = db.summary_by_product(scope, key, d_from, d_to)
    by_hour = db.summary_by_hour(scope, key, d_from, d_to)
    by_gate = db.summary_by_gate(scope, key, d_from, d_to) if audience != "merchant" else None
    days = db.by_days(scope, key, d_from, d_to)
    beh = db.behavior(scope, key, d_from, d_to)

    wb = Workbook()
    wb.remove(wb.active)

    _sheet_meta(wb, scope, key, audience, d_from, d_to, len(txs))

    label_obj = {
        "merchant": f"мерчант {key}",
        "gate":     f"гейт {key}",
        "all":      "все мерчанты",
    }[scope]
    title = f"Сводка — {label_obj} — {d_from.isoformat()} — {d_to.isoformat()}"

    _sheet_summary(wb, kpi, by_method, by_product, by_hour, by_gate, audience, title)
    _sheet_by_days(wb, days)
    _sheet_behavior(wb, beh, audience)
    _sheet_transactions(wb, txs, audience)

    out_dir = Path(out_dir or OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_key = (key or "all").replace("/", "_").replace(" ", "_")
    period = (d_from.isoformat() if d_from == d_to
              else f"{d_from.isoformat()}_{d_to.isoformat()}")
    fname = f"ops_{audience}_{scope}_{safe_key}_{period}.xlsx"
    out_path = out_dir / fname
    wb.save(out_path)
    log.info(f"saved: {out_path} ({len(txs)} tx)")
    return out_path
