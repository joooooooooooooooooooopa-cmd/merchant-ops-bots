"""Хелперы UX: парсинг дат, форматтеры, состояния."""
import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

# Состояния (user_data["mode"])
MODE_MERCHANT_SEARCH = "merchant_search"
MODE_GATE_SEARCH = "gate_search"
MODE_DATE_CUSTOM = "date_custom"


def today_msk() -> date:
    return datetime.now(MSK).date()


def yesterday_msk() -> date:
    return today_msk() - timedelta(days=1)


def days_ago(n: int) -> date:
    return today_msk() - timedelta(days=int(n))


_DATE_RE = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s*$")
_RANGE_RE = re.compile(r"^\s*(\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?)\s*[-–—]\s*(\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?)\s*$")


def _parse_one(s: str) -> Optional[date]:
    m = _DATE_RE.match(s)
    if not m:
        return None
    d, mo, y = m.groups()
    year = int(y) if y else today_msk().year
    if year < 100:
        year += 2000
    try:
        return date(year, int(mo), int(d))
    except ValueError:
        return None


def parse_date_input(text: str) -> Optional[Tuple[date, date]]:
    """Принимает:
      - DD.MM или DD.MM.YYYY (1 день),
      - DD.MM-DD.MM или с годами (диапазон).
    Возвращает (d_from, d_to) либо None."""
    if not text:
        return None
    text = text.strip()
    m = _RANGE_RE.match(text)
    if m:
        d1 = _parse_one(m.group(1))
        d2 = _parse_one(m.group(2))
        if d1 and d2:
            if d1 > d2:
                d1, d2 = d2, d1
            return d1, d2
        return None
    one = _parse_one(text)
    if one:
        return one, one
    return None


def fmt_number(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except Exception:
        return str(n)


def fmt_period(d_from: date, d_to: date) -> str:
    if d_from == d_to:
        return d_from.strftime("%d.%m.%Y")
    if d_from.year == d_to.year:
        return f"{d_from.strftime('%d.%m')}—{d_to.strftime('%d.%m.%Y')}"
    return f"{d_from.strftime('%d.%m.%Y')}—{d_to.strftime('%d.%m.%Y')}"
