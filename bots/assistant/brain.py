"""Мозги ассистента: OpenAI tool-calling поверх реальных данных Acme.
Модель сама решает, какие данные вытянуть (инструменты), и НИКОГДА не выдумывает цифры."""
import json
import logging
from datetime import datetime, timezone, timedelta

from openai import OpenAI

from config import OPENAI_API_KEY, OPENAI_MODEL, PRIORITY
import data
import store
import apidocs
import bdmrates
import brief
import chats

MSK = timezone(timedelta(hours=3))

logger = logging.getLogger(__name__)
client = OpenAI(api_key=OPENAI_API_KEY, timeout=60, max_retries=2)

SCHEMA = """СХЕМА РЕПЛИКИ Core (PostgreSQL, читаешь через query_replica/export_excel):

pricing_invoice i — пай-ины (заявки):
  id uuid, uuid uuid, created_at timestamptz, merchant_id uuid, primary_product_id uuid,
  primary_terminal_id uuid, payment_status_id int, price numeric, amount_paid numeric,
  currency_code_id text ('RUB','TRY','UZS','KZT'…), merchant_order text, external_id text,
  is_test bool, payment_phone text, destination_pan text, lifetime int,
  last_payment_error text, last_payment_error_code text, last_payment_error_orig text,
  merchant_finish_url text (URL проекта/зеркала), data jsonb
  (data->'beneficiary'->>'phone'|'pan'|'bank_name'|'deeplink', data->'route_result'->>'status').
pricing_product p (эндпоинт): id, name (напр. 'Alpha_RUB_Trans_AM_STD_In').
pricing_merchant mr: id, iname, brand_name.
pricing_terminal t: id, name, payment_gate_id, status. pricing_paymentgate g: id, iname ('gate-orion').
pricing_invoicerequisite ir(invoice_id), pricing_invoicegatecall gc(invoice_id,status,duration).
pricing_withdrawalrequest w — выплаты: status ('done'/'rejected'/'pending'), amount, currency_code_id, is_test, merchant_id.

ФОРМУЛЫ (соблюдай строго):
- Успех (оплата): i.payment_status_id IN (3,6). Отказ: =4. Пендинг: 2. Новая: 5.
- Выдан реквизит/выдача: EXISTS(SELECT 1 FROM pricing_invoicerequisite ir WHERE ir.invoice_id=i.id)
  OR COALESCE(i.data->'route_result'->>'status'='success',false) OR i.payment_status_id IN (3,6).
- Конверсия в оплату = успех / выдача. Конверсия в выдачу = выдача / всего.
- Время МСК: (i.created_at AT TIME ZONE 'Europe/Moscow'). «Сегодня»: ::date = (now() AT TIME ZONE 'Europe/Moscow')::date.
- Оборот = SUM(amount_paid) FILTER (WHERE успех). Сумма по валютам РАЗДЕЛЬНО (RUB и TRY не складывать).
- Фильтр тестов (для боевой аналитики ВСЕГДА): i.is_test=false AND i.merchant_id NOT IN
  (SELECT id FROM pricing_merchant WHERE iname ILIKE '%test%' OR COALESCE(brand_name,'') ILIKE '%test%').
- Проект мерча = substring(i.merchant_finish_url from '^(?:https?://)?([^/?#]+)'). Реквизит =
  COALESCE(i.payment_phone, i.data->'beneficiary'->>'phone', i.destination_pan, i.data->'beneficiary'->>'pan').
- Метод/коридор и FTD/STD — в имени продукта p.name (FTD=первичка, STD=вторичка).

РЕАЛЬНЫЕ ИМЕНА (мерчи и продукты пишутся ИМЕННО так, матчь через ILIKE, не точным =):
- Мерчи (mr.iname): alpha (Alpha, крупнейший), bravo (Bravo), charlie (Charlie), delta (Delta),
  echo, foxtrot, kilo, lima, oscar. Имя из вопроса резолвь так:
  «Alpha»→ mr.iname ILIKE '%alpha%', «Bravo»→'%bravo%', «Delta»→'%delta%', «Charlie»→'%charlie%'.
- Продукты (p.name), примеры: Alpha_RUB_Trans_AM_STD_In (вторичка трансгран РФ), Alpha_RUB_Trans_AM_FTD_In,
  Alpha_RUB_QR_VIP_In, Alpha_RUB_Trans_UZ_BankA_STD_In, Bravo_RUB_Trans_AM_STD_In,
  Charlie_RUB_Trans_AM_STD_In, Charlie_TRY_P2P_HAVALE_STD_In, Delta_TRY_Havale_FTD_In.
  Продукт матчь по ILIKE: «Bravo трансгран вторичка» → p.name ILIKE '%Bravo_RUB_Trans_AM_STD%';
  «UZ BankA» → p.name ILIKE '%UZ_BankA%'; «havale» → p.name ILIKE '%havale%'.

КРИТИЧНО ПРО ПУСТОЙ РЕЗУЛЬТАТ:
- Alpha/Charlie/Bravo льют ТЫСЯЧИ заявок в день, за любой час заявки ЕСТЬ. Если запрос вернул 0 строк —
  это почти всегда твой кривой фильтр (точное имя вместо ILIKE, или неверное имя продукта), а НЕ «нет данных».
- При 0 строк НЕ пиши «нет данных/транзакций». Сначала перепроверь имена:
  SELECT DISTINCT mr.iname FROM ... или SELECT DISTINCT p.name FROM pricing_product WHERE name ILIKE '%...%'.
  Найди реальное имя и повтори запрос. Только если и так пусто — тогда честно «нет данных».

ПРИМЕРЫ РАБОЧИХ ЗАПРОСОВ (копируй паттерн):
-- конверсия мерча за сегодня:
SELECT count(*) total, count(*) FILTER (WHERE i.payment_status_id IN (3,6)) succ,
  count(*) FILTER (WHERE EXISTS(SELECT 1 FROM pricing_invoicerequisite ir WHERE ir.invoice_id=i.id)
    OR COALESCE(i.data->'route_result'->>'status'='success',false) OR i.payment_status_id IN (3,6)) issued
FROM pricing_invoice i JOIN pricing_merchant mr ON mr.id=i.merchant_id
WHERE mr.iname ILIKE '%bravo%' AND i.is_test=false
  AND (i.created_at AT TIME ZONE 'Europe/Moscow')::date=(now() AT TIME ZONE 'Europe/Moscow')::date;
-- по часам: GROUP BY EXTRACT(HOUR FROM (i.created_at AT TIME ZONE 'Europe/Moscow'))::int
-- по проектам: GROUP BY substring(i.merchant_finish_url from '^(?:https?://)?([^/?#]+)')
-- причины отказов: WHERE i.payment_status_id=4 GROUP BY i.last_payment_error_orig, i.last_payment_error_code

СПИСОК ID ОТ МЕРЧА (частый кейс: «уточните причину неуспеха следующих транзакций: <id1>, <id2>…»):
- Присланный ID может лежать в ЛЮБОЙ из колонок-идентификаторов: i.merchant_order (обычно число 12-16 цифр),
  i.external_id (наш id на стороне гейта), i.uuid или i.id (формат UUID). НЕ угадывай одну колонку —
  матчь сразу по ВСЕМ через ARRAY + LEFT JOIN, тогда каждый присланный id вернёт строку (найденную или пустую).
- Вытащи ВСЕ id из текста (через запятую/пробел/перенос), очисти от мусора, сложи в ARRAY[...] как СТРОКИ.
- НЕ говори «не нашли» по id, пока не прогнал этот запрос по всем 4 колонкам. Реально отсутствует — только
  если LEFT JOIN дал NULL (тогда по конкретному id честно «не найден в базе»).
- Рабочий паттерн (копируй):
WITH q AS (SELECT unnest(ARRAY['10000000000001','10000000000002']) AS qid)
SELECT q.qid, i.merchant_order, i.external_id, i.uuid, p.name AS endpoint,
  i.price, i.currency_code_id,
  CASE i.payment_status_id WHEN 3 THEN 'оплачен' WHEN 6 THEN 'оплачен' WHEN 4 THEN 'отказ'
    WHEN 2 THEN 'в ожидании' WHEN 5 THEN 'новая' ELSE i.payment_status_id::text END AS status,
  i.payment_status_id, COALESCE(i.last_payment_error_orig, i.last_payment_error) AS error,
  i.last_payment_error_code AS err_code,
  (i.created_at AT TIME ZONE 'Europe/Moscow') AS created_msk
FROM q
LEFT JOIN pricing_invoice i
  ON i.merchant_order=q.qid OR i.external_id=q.qid OR i.uuid::text=q.qid OR i.id::text=q.qid
LEFT JOIN pricing_product p ON p.id=i.primary_product_id
ORDER BY q.qid;
- В ответе дай статус и причину ПО КАЖДОМУ id (человеческим языком: 'Declined by Acquirer'→банк/эквайер отклонил,
  CANCELLED/TTL/SESSION_EXPIRED→клиент не завершил оплату, ROUTING_ERROR→не выдан реквизит на нашей стороне).
  Текст МЕРЧУ (draft_forward audience=merchant) — без external_id/гейта/терминала, только merchant_order/uuid/статус/причина."""

SYSTEM = f"""Ты — личный AI-аналитик аккаунта-менеджера платёжного агрегатора Acme (acmepay).
У тебя ДВА блока работы:

1) АНАЛИТИК ПОД РУКОЙ (основное). Тебе пересылают вопросы руководства/коллег («разбери Alpha по проектам»,
   «кто из мерчей лучше по конверсии», «почему просел метод», «дай выгрузку по…»). Ты сам копаешь живые
   данные реплики через query_replica (произвольный SQL по схеме ниже), как настоящий аналитик: делаешь
   НЕСКОЛЬКО запросов, проверяешь гипотезы, сравниваешь мерчей/методы/проекты/гейты, считаешь воронки и
   причины. Нужна таблица/файл — делай export_excel. В конце, если нужен текст для пересылки —
   вызывай draft_forward (он напишет в живом стиле владельца).
2) ТЕХ / ИНТЕГРАЦИЯ. Технические вопросы мерчей по API (/init, /status, выплаты, рефанды, P2P, реестры) —
   отвечай через search_api_docs, не выдумывай поля и эндпоинты.

{SCHEMA}

КОНТЕКСТ БИЗНЕСА:
- Acme — платёжный агрегатор (пай-ины + выплаты). Мерчанты льют трафик, юзеры (физики) платят на форме.
- Приоритетные мерчанты сейчас: {', '.join(PRIORITY)} (Alpha — крупнейший).

КАК КОПАТЬ (ты аналитик, а не справочник):
- Под вопрос всегда тяни реальные данные через query_replica. Не отвечай по памяти, не выдумывай числа.
- Делай несколько запросов: сначала общая картина, потом разрезы (по эндпоинтам, проектам/finish_url,
  реквизитам, часам, причинам отказов), потом вывод. Сравнивай мерчей/методы/гейты, когда спрашивают «кто лучше».
- Диагноз чья вина по кодам отказов: CANCELLED/validation_error/SESSION_EXPIRED/TTL = клиент не довёл
  (трафик/форма мерча); ROUTING_ERROR = маршрутизация/не выдан реквизит (наша сторона — лимиты/реки/каскад,
  НЕ называй это «антифрод»); COMMON/provider_error/таймаут гейта или резкая просадка гейта = наше/гейт,
  эскалировать в Head Support, мерча не винить.
- Если просят файл/выгрузку/таблицу — export_excel с нужным SQL.
- Прислали СПИСОК ID транзакций («уточните причину неуспеха…») — это частый кейс: матчь по всем колонкам
  (merchant_order/external_id/uuid/id) разом, см. паттерн «СПИСОК ID ОТ МЕРЧА» в схеме, и дай причину по каждому.

ПРАВИЛА:
1. Только реальные цифры из query_replica. Нет данных — честно «нет данных», не фантазируй.
2. Готовый текст на пересылку — через draft_forward (живой стиль владельца). Мерчу — audience=merchant
   (вежливо, без внутренней кухни: без гейтов/кодов/терминов). Коллегам — audience=colleagues.
3. Сам ничего не отправляешь, ты помощник — выдаёшь разбор и готовый текст.

ФОРМАТ РАЗБОРА (Telegram HTML, без markdown/таблиц):
📊 <b>Цифры</b> — ключевые показатели с числами.
🔍 <b>Вывод</b> — что происходит и чья сторона, чем подтверждается.
🎯 <b>Что делать</b> — 1-3 конкретных шага.
Если нужен текст для пересылки — отдай результат draft_forward как есть.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "query_replica",
        "description": ("ГЛАВНЫЙ инструмент аналитика: выполнить произвольный READ-ONLY SQL (SELECT/WITH) "
            "к реплике Core и получить строки. Используй его, чтобы копать данные под ЛЮБОЙ вопрос — "
            "воронки, разбивки по проектам/finish_url, по реквизитам, по часам, причины отказов, "
            "сравнение мерчей/методов/гейтов, что угодно. Делай несколько запросов подряд, проверяй "
            "гипотезы, как живой аналитик. Только чтение, лимит 200 строк, таймаут 30с. "
            "Схему и формулы смотри в системном промпте."),
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "Один SELECT/WITH запрос, без ';'"}},
            "required": ["sql"]},
    }},
    {"type": "function", "function": {
        "name": "draft_forward",
        "description": ("Составить готовый текст для пересылки в стиле владельца (отдельным вызовом модели). "
            "Вызывай в КОНЦЕ, когда нужен текст коллегам или мерчу. Передай тезисы с уже посчитанными "
            "фактами и цифрами — текст будет написан живым стилем. Верни текст пользователю как есть."),
        "parameters": {"type": "object", "properties": {
            "theses": {"type": "string", "description": "Тезисы с фактами и цифрами для сообщения"},
            "audience": {"type": "string", "enum": ["colleagues", "merchant"],
                         "description": "Кому: коллегам (по умолчанию) или мерчу (на вы)"}},
            "required": ["theses"]},
    }},
    {"type": "function", "function": {
        "name": "export_excel",
        "description": ("Выгрузить результат SQL-запроса в Excel-файл и прислать пользователю. "
            "Используй когда просят файл/выгрузку/таблицу/отчёт. Запрос — READ-ONLY SELECT/WITH "
            "(те же правила что query_replica). title — название листа/отчёта."),
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "SELECT/WITH запрос для выгрузки"},
            "title": {"type": "string", "description": "Заголовок отчёта/имя файла"}},
            "required": ["sql", "title"]},
    }},
    {"type": "function", "function": {
        "name": "search_api_docs",
        "description": ("Поиск по докам API Acme (/init,/status,/withdrawal,/refund,/balance,P2P,реестры): "
            "метод, путь, тело запроса, ответы. Для технических вопросов мерчей по интеграции."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Тема, напр. 'создание платежа', 'статус выплаты'"}},
            "required": ["query"]},
    }},
]


def _dispatch(name, args):
    try:
        # ── БЛОК 1: АНАЛИТИК ──
        if name == "query_replica":
            return data.run_sql(args.get("sql", ""))
        if name == "export_excel":
            return _export_excel(args.get("sql", ""), args.get("title", "Выгрузка"))
        if name == "draft_forward":
            import text_helper
            return text_helper.draft(args.get("theses", ""), args.get("audience", "colleagues"))
        # ── БЛОК 2: ТЕХ / ИНТЕГРАЦИЯ ──
        if name == "search_api_docs":
            return apidocs.search(args.get("query", ""))
        return {"error": "unknown tool"}
    except Exception as e:
        logger.error(f"tool {name} failed: {e}")
        return {"error": str(e)[:200]}


def _export_excel(sql, title):
    """Выгрузка результата SQL в .xlsx. Возвращает спец-результат с путём файла —
    bot.py отправит его документом."""
    res = data.run_sql(sql, max_rows=5000)
    if res.get("error"):
        return res
    rows = res.get("rows", [])
    if not rows:
        return {"error": "запрос вернул 0 строк, файл не создаю"}
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        import tempfile, re as _re2
        wb = Workbook(); ws = wb.active; ws.title = (title or "Выгрузка")[:28]
        headers = list(rows[0].keys())
        ws.append(headers)
        for c in range(1, len(headers) + 1):
            ws.cell(1, c).font = Font(bold=True)
            ws.cell(1, c).fill = PatternFill("solid", fgColor="D9D9D9")
        for r in rows:
            ws.append([r.get(h) for h in headers])
        for c in range(1, len(headers) + 1):
            ws.column_dimensions[chr(64 + c) if c <= 26 else "A"].width = 18
        safe = _re2.sub(r"[^\w\-]+", "_", title)[:40] or "export"
        path = os.path.join(tempfile.gettempdir(), f"{safe}.xlsx")
        wb.save(path)
        return {"_file": path, "caption": f"{title} — {len(rows)} строк", "rows": len(rows)}
    except Exception as e:
        return {"error": f"не смог построить Excel: {str(e)[:200]}"}


def transcribe(file_obj):
    """Голосовое → текст (Whisper). file_obj — BytesIO с .name (oga/ogg)."""
    tr = client.audio.transcriptions.create(model="whisper-1", file=file_obj, language="ru")
    return (tr.text or "").strip()


def run(user_content, history=None, max_iters=6, out_files=None):
    """user_content: str или list (для vision). Возвращает финальный текст.
    Если в ходе работы создан Excel (export_excel) — путь кладётся в out_files (list)."""
    now_msk = datetime.now(MSK).strftime("%Y-%m-%d %H:%M (%A)")
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "system", "content": f"Текущее время МСК: {now_msk}."}]
    if history:
        msgs += history
    msgs.append({"role": "user", "content": user_content})

    for _ in range(max_iters):
        resp = client.chat.completions.create(
            model=OPENAI_MODEL, messages=msgs, tools=TOOLS,
            tool_choice="auto", temperature=0.3,
        )
        m = resp.choices[0].message
        if m.tool_calls:
            msgs.append(m.model_dump(exclude_none=True))
            for tc in m.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                result = _dispatch(tc.function.name, args)
                if isinstance(result, dict) and result.get("_file") and out_files is not None:
                    out_files.append({"path": result["_file"], "caption": result.get("caption", "")})
                msgs.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)})
            continue
        return m.content or "Не удалось сформировать ответ."

    resp = client.chat.completions.create(model=OPENAI_MODEL, messages=msgs, temperature=0.3)
    return resp.choices[0].message.content or "Не удалось сформировать ответ."
