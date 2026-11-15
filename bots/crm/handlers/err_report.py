"""Excel-отчёт по ошибкам мерча (merchant-clean: без гейта/терминала/external_id).
Используется кнопкой в CRM-боте. Данные — metabase.get_error_report."""
import io
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# код ошибки → (человеческое пояснение, категория)
_EXPL = {
    "COMMON": ("Техническая ошибка обработки платежа", "Платёжная система"),
    "COMMON_ERROR": ("Техническая ошибка обработки платежа", "Платёжная система"),
    "PROVIDER_ERROR": ("Ошибка на стороне платёжной системы", "Платёжная система"),
    "PAYMENT_SYSTEM_ERROR": ("Ошибка на стороне платёжной системы", "Платёжная система"),
    "ROUTING_ERROR": ("Не прошёл проверку маршрутизации / безопасности", "Антифрод"),
    "CANCELLED": ("Клиент закрыл форму / отменил оплату", "Клиент"),
    "SESSION_EXPIRED": ("Истекло время оплаты (клиент не оплатил)", "Клиент"),
    "validation_error": ("Сумма вне лимита / не прошла валидацию", "Клиент"),
    "(нет кода)": ("Без кода (истёк таймаут / прочее)", "Прочее"),
}


def _expl(code):
    return _EXPL.get(code, _EXPL.get((code or "").upper(), (code, "Прочее")))


def _fmt(n):
    return f"{int(round(n)):,}".replace(",", " ")


def build_error_xlsx(merchant_name: str, period_label: str, data: dict) -> io.BytesIO:
    s = data["summary"]
    rows = data["rows"]
    NAVY = PatternFill("solid", fgColor="1F3864"); HEAD = PatternFill("solid", fgColor="2E5496")
    GREY = PatternFill("solid", fgColor="F2F4F8"); REDF = PatternFill("solid", fgColor="FCE4E4")
    white = Font(color="FFFFFF", bold=True, size=11); title = Font(color="FFFFFF", bold=True, size=14)
    b = Font(bold=True)
    thin = Side(style="thin", color="D0D5DD"); BORD = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center"); left = Alignment(horizontal="left", vertical="center")

    wb = Workbook(); ws = wb.active; ws.title = "Ошибки"; ws.sheet_view.showGridLines = False
    r = 1
    ws.merge_cells(f"A{r}:F{r}"); c = ws[f"A{r}"]
    c.value = f"{merchant_name} — разбор отказов"; c.fill = NAVY; c.font = title; c.alignment = center
    ws.row_dimensions[r].height = 26; r += 1
    ws.merge_cells(f"A{r}:F{r}"); c = ws[f"A{r}"]
    per = f"{period_label}: {s['dfrom']} — {s['dto']} (МСК)" if s["dfrom"] else period_label
    c.value = per; c.fill = HEAD; c.font = Font(color="FFFFFF", size=10); c.alignment = center; r += 2

    conv = round(100.0 * s["paid"] / s["total"], 1) if s["total"] else 0
    kpis = [("Заявок всего", _fmt(s["total"])), ("Оплачено", _fmt(s["paid"])),
            ("Конверсия в оплату", f"{conv}%"), ("Не оплачено", _fmt(s["failed"])),
            ("Оборот (оплачено), ₽", _fmt(s["turnover"])), ("Сумма неоплаченных, ₽", _fmt(s["failed_amt"]))]
    for i, (k, v) in enumerate(kpis):
        rr = r + i // 2; cc = 1 + (i % 2) * 2
        kc = ws.cell(rr, cc, k); kc.font = b; kc.fill = GREY; kc.border = BORD; kc.alignment = left
        vc = ws.cell(rr, cc + 1, v); vc.border = BORD; vc.alignment = center
    r += len(kpis) // 2 + 1

    ws.merge_cells(f"A{r}:F{r}"); h = ws[f"A{r}"]; h.value = "Структура отказов"; h.fill = NAVY; h.font = white; h.alignment = center; r += 1
    for j, cn in enumerate(["Код ошибки", "Что значит", "Категория", "Кол-во", "Доля %", "Сумма заявок, ₽"], 1):
        c = ws.cell(r, j, cn); c.fill = HEAD; c.font = white; c.alignment = center; c.border = BORD
    r += 1
    tot = sum(e["n"] for e in rows) or 1
    for e in rows:
        txt, cat = _expl(e["code"]); share = round(100.0 * e["n"] / tot, 1)
        for j, val in enumerate([e["code"], txt, cat, e["n"], f"{share}%", _fmt(e["amount"])], 1):
            c = ws.cell(r, j, val); c.border = BORD; c.alignment = center if j != 2 else left
            if cat == "Платёжная система":
                c.fill = REDF
        r += 1
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    tc = ws.cell(r, 1, "ИТОГО отказов"); tc.font = b; tc.fill = GREY; tc.border = BORD
    for j, val in [(4, tot), (5, "100%"), (6, _fmt(s["failed_amt"]))]:
        c = ws.cell(r, j, val); c.font = b; c.alignment = center; c.border = BORD

    for i, w in enumerate([20, 42, 18, 11, 11, 18], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    bio = io.BytesIO(); wb.save(bio); bio.seek(0)
    return bio
