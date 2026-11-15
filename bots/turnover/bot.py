"""Acme Turnover Bot — каждые 3 часа постит в группу оборот с 00:00 МСК по валютам.
Команды по запросу (в группе или у бота в ЛС от админа): /oborot → оборот с 00:00 до сейчас;
/hourly → почасовой общий объём за сегодня + сравнение с вчера. Источник — read-only реплика Core."""
import os, time, json, urllib.request, urllib.parse, logging
from datetime import datetime, timezone, timedelta
import psycopg2, psycopg2.extras
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
DB_URL = os.environ["ANALYTICS_DB_URL"]
MSK = timezone(timedelta(hours=3))

# Кто может вызвать /oborot вне основной группы (в ЛС). Группа CHAT_ID разрешена всем участникам.
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
# Триггеры команды. Латинские работают в группе при любом privacy mode; кириллические — бонус.
CMD_TRIGGERS = {"/oborot", "/report", "/now", "/turnover", "/svod",
                "/оборот", "/сводка", "оборот"}
# Почасовой объём за сегодня (заявки/оплаты/оборот по часам + сравнение с вчера).
HOURLY_TRIGGERS = {"/hourly", "/pochasovaya", "/hours", "/trend", "/chas",
                   "/почасовая", "/час", "почасовая"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("turnover")

SYM = {"RUB": "₽", "TRY": "₺", "KZT": "₸", "UZS": "soʻm", "TJS": "SM",
       "AZN": "₼", "USD": "$", "EUR": "€", "AMD": "֏", "KRW": "₩", "KGS": "сом"}
FLAG = {"RUB": "🇷🇺", "TRY": "🇹🇷", "KZT": "🇰🇿", "UZS": "🇺🇿", "TJS": "🇹🇯",
        "AZN": "🇦🇿", "USD": "🇺🇸", "EUR": "🇪🇺", "AMD": "🇦🇲", "KRW": "🇰🇷", "KGS": "🇰🇬"}


def fetch(target_date):
    """Оборот за конкретную дату МСК (по валютам + по мерчантам)."""
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute("""
                SELECT i.currency_code_id AS cur,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) AS success,
                       COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE i.payment_status_id IN (3,6))),0) AS paid
                FROM pricing_invoice i
                LEFT JOIN pricing_merchant mr ON mr.id = i.merchant_id
                WHERE (i.created_at AT TIME ZONE 'Europe/Moscow')::date = %s
                  AND i.is_test = false
                  AND COALESCE(mr.iname, '') NOT ILIKE '%%test%%'
                  AND COALESCE(mr.brand_name, '') NOT ILIKE '%%test%%'
                GROUP BY 1
                ORDER BY paid DESC
            """, (target_date,))
            by_cur = [dict(r) for r in c.fetchall()]

            c.execute("""
                SELECT COALESCE(mr.brand_name, mr.iname, '—') AS merchant,
                       i.currency_code_id AS cur,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) AS success,
                       COALESCE(ROUND(SUM(i.amount_paid) FILTER (WHERE i.payment_status_id IN (3,6))),0) AS paid
                FROM pricing_invoice i
                LEFT JOIN pricing_merchant mr ON mr.id = i.merchant_id
                WHERE (i.created_at AT TIME ZONE 'Europe/Moscow')::date = %s
                  AND i.is_test = false
                  AND COALESCE(mr.iname, '') NOT ILIKE '%%test%%'
                  AND COALESCE(mr.brand_name, '') NOT ILIKE '%%test%%'
                GROUP BY 1, 2
                ORDER BY paid DESC, total DESC
            """, (target_date,))
            by_merch = [dict(r) for r in c.fetchall()]
        return by_cur, by_merch
    finally:
        conn.close()


def report_target_date(now=None):
    """Дата суток для отчёта.
    В 00:00 МСК подытоживаем ЗАВЕРШИВШИЕСЯ сутки (вчерашняя дата) — иначе пост в полночь
    приходит с пустотой («заявок ещё не было»). Во все прочие часы (03/06/.../21) — текущая дата."""
    now = now or datetime.now(MSK)
    return (now - timedelta(days=1)).date() if now.hour == 0 else now.date()


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def build_msg():
    now_dt = datetime.now(MSK)
    target = report_target_date(now_dt)
    by_cur, by_merch = fetch(target)
    is_full_day = (now_dt.hour == 0)
    if is_full_day:
        header = f"📊 <b>Оборот (успешные оплаты) за {target.strftime('%d.%m')} — итог суток (00:00–23:59 МСК)</b>"
        empty_msg = f"За {target.strftime('%d.%m')} заявок не было."
    else:
        header = f"📊 <b>Оборот (успешные оплаты) с 00:00 МСК — на {now_dt.strftime('%H:%M %d.%m')}</b>"
        empty_msg = "Сегодня заявок ещё не было."
    lines = [header, ""]
    if not by_cur:
        lines.append(empty_msg)
        return "\n".join(lines)
    # под каждой валютой — её мерчи (кто сколько прокачал)
    merch_by_cur = {}
    for m in by_merch:
        merch_by_cur.setdefault(m["cur"], []).append(m)
    for r in by_cur:
        cur = r["cur"]
        flag = FLAG.get(cur, "🏳️")
        sym = SYM.get(cur, cur)
        lines.append(f"{flag} <b>{cur}</b>: оборот {fmt(r['paid'])} {sym} · "
                     f"обработано {r['success']} из {r['total']} заявок")
        for m in merch_by_cur.get(cur, []):
            lines.append(f"   • {m['merchant']}: {fmt(m['paid'])} {sym} · "
                         f"{m['success']}/{m['total']}")
        lines.append("")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def fetch_hourly():
    """Почасовой срез за сегодня и вчера (МСК): заявки/оплаты/оборот RUB по часам."""
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute("""
                SELECT (i.created_at AT TIME ZONE 'Europe/Moscow')::date::text AS d,
                       EXTRACT(hour FROM (i.created_at AT TIME ZONE 'Europe/Moscow'))::int AS h,
                       COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE i.payment_status_id IN (3,6)) AS success,
                       COALESCE(ROUND(SUM(i.amount_paid) FILTER (
                           WHERE i.payment_status_id IN (3,6) AND i.currency_code_id='RUB')),0) AS paid_rub
                FROM pricing_invoice i
                LEFT JOIN pricing_merchant mr ON mr.id = i.merchant_id
                WHERE (i.created_at AT TIME ZONE 'Europe/Moscow')::date
                      >= (now() AT TIME ZONE 'Europe/Moscow')::date - 1
                  AND i.is_test = false
                  AND COALESCE(mr.iname, '') NOT ILIKE '%%test%%'
                  AND COALESCE(mr.brand_name, '') NOT ILIKE '%%test%%'
                GROUP BY 1, 2
            """)
            return [dict(r) for r in c.fetchall()]
    finally:
        conn.close()


def build_hourly_msg():
    """Почасовой объём заявок за сегодня vs вчера (выровненная таблица, видно где просадка)."""
    now_dt = datetime.now(MSK)
    today = now_dt.date().isoformat()
    yest = (now_dt - timedelta(days=1)).date().isoformat()
    rows = fetch_hourly()
    t = {int(r['h']): r for r in rows if r['d'] == today}
    y = {int(r['h']): r for r in rows if r['d'] == yest}
    if not t:
        return "Сегодня заявок ещё не было."
    # таблица в моноширинном <pre> — колонки выравниваются: час · сегодня · вчера · разница
    tbl = [f"{'час':<4}{'сег':>6}{'вчера':>7}{'Δ':>8}"]
    tot = succ = vol = ytot = 0
    for h in range(now_dt.hour + 1):
        r = t.get(h)
        if not r:
            continue
        n = int(r['total']); s = int(r['success']); v = int(r['paid_rub'])
        yh = int((y.get(h) or {}).get('total') or 0)
        tot += n; succ += s; vol += v; ytot += yh
        d = n - yh
        mark = " ▼" if (yh and d <= -0.3 * yh) else (" ▲" if (yh and d >= 0.3 * yh) else "")
        tbl.append(f"{('%02d' % h):<4}{n:>6}{yh:>7}{d:>+8}{mark}")
    tbl.append("─" * 25)
    dt = tot - ytot
    tbl.append(f"{'итог':<4}{tot:>6}{ytot:>7}{dt:>+8}")
    pct = round(dt * 100.0 / ytot) if ytot else 0
    head = f"🕐 <b>Объём заявок по часам — сегодня vs вчера (МСК), на {now_dt.strftime('%H:%M %d.%m')}</b>"
    foot = (f"Заявок сегодня <b>{fmt(tot)}</b> vs вчера {fmt(ytot)} "
            f"(<b>{'+' if dt >= 0 else ''}{pct}%</b>)\n"
            f"Оплачено {fmt(succ)} · оборот {fmt(vol)} ₽\n"
            f"▼ — час просел к вчера на 30%+")
    return head + "\n\n<pre>" + "\n".join(tbl) + "</pre>\n" + foot


def send(text, chat_id=None):
    data = urllib.parse.urlencode({"chat_id": chat_id or CHAT_ID, "text": text,
                                   "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def get_updates(offset, timeout=10):
    """Long-poll входящих апдейтов (для команды /oborot). Без вебхука — только sendMessage иначе."""
    params = urllib.parse.urlencode({"offset": offset, "timeout": timeout,
                                     "allowed_updates": json.dumps(["message"])})
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/getUpdates?{params}")
    with urllib.request.urlopen(req, timeout=timeout + 15) as r:
        return json.loads(r.read())


def _allowed(chat_id, user_id):
    """Команда разрешена в основной группе (всем) и в ЛС от админов."""
    if str(chat_id) == str(CHAT_ID):
        return True
    return bool(user_id and user_id in ADMIN_IDS)


def handle_update(upd):
    """Если пришла команда-триггер от разрешённого источника — шлём отчёт в тот же чат."""
    msg = upd.get("message")
    if not msg:
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return
    first = text.split()[0].split("@")[0].lower()   # '/oborot@TurnoverBot' → '/oborot'
    is_hourly = first in HOURLY_TRIGGERS
    if first not in CMD_TRIGGERS and not is_hourly:
        return
    chat_id = msg["chat"]["id"]
    user_id = (msg.get("from") or {}).get("id")
    if not _allowed(chat_id, user_id):
        return
    try:
        send(build_hourly_msg() if is_hourly else build_msg(), chat_id=chat_id)
        log.info(f"Команда {first} от {user_id} → {'почасовая' if is_hourly else 'оборот'} в чат {chat_id}")
    except Exception as e:
        log.error(f"Ответ на команду не отправлен: {e}")


def next_tick():
    """Следующий момент HH ∈ {0,3,6,9,12,15,18,21} МСК."""
    now = datetime.now(MSK)
    h = (now.hour // 3 + 1) * 3
    base = now.replace(minute=0, second=0, microsecond=0)
    return (base + timedelta(days=1)).replace(hour=0) if h >= 24 else base.replace(hour=h)


if __name__ == "__main__":
    log.info("Acme Turnover Bot запущен — плановые посты каждые 3ч + команда /oborot по запросу")
    # Пропускаем накопленный бэклог апдейтов, чтобы на старте не ответить на старые команды.
    offset = 0
    try:
        d0 = get_updates(0, timeout=0)
        if d0.get("ok") and d0.get("result"):
            offset = d0["result"][-1]["update_id"] + 1
    except Exception as e:
        log.error(f"Старт getUpdates: {e}")

    nxt = next_tick()
    log.info(f"Следующий плановый пост: {nxt.strftime('%H:%M %d.%m')} МСК")
    while True:
        # 1) плановый пост, если подошло время
        if datetime.now(MSK) >= nxt:
            for attempt in (1, 2, 3):
                try:
                    send(build_msg())
                    log.info("Плановый пост отправлен.")
                    break
                except Exception as e:
                    log.error(f"Ошибка планового поста (попытка {attempt}/3): {e}")
                    time.sleep(10)
            nxt = next_tick()
            log.info(f"Следующий плановый пост: {nxt.strftime('%H:%M %d.%m')} МСК")

        # 2) слушаем команды (long-poll до 10с — он же задаёт гранулярность проверки тика)
        try:
            data = get_updates(offset, timeout=10)
            if data.get("ok"):
                for upd in data["result"]:
                    offset = upd["update_id"] + 1
                    handle_update(upd)
        except Exception as e:
            log.error(f"getUpdates: {e}")
            time.sleep(3)
