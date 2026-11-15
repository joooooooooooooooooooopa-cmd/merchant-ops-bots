"""Анализатор полной TG-выгрузки.

При получении полного экспорта (result.json) от owner'а:
  1) парсит все чаты, игнорирует OPS ALERT и техфейковые
  2) собирает свежий контекст по каждому мерчанту (последние 80 сообщений)
  3) вытаскивает свежие распоряжения Lead из «Бизнес Acme»
  4) тянет цифры из реплики (через data.py)
  5) для каждого активного мерча генерит ОДНО утреннее сообщение через GPT-4o (brain.run)
  6) ставит в очередь напоминаний (store.add_reminder) с расписанием по типу бренда
  7) reminder_job (каждые 60с) разошлёт owner'у в нужное время

Идемпотентно: при повторной заливке пакета за тот же день очищает не-fired напоминания
с маркером MORNING_SCHED_TAG.
"""
import logging
import re
import time as t
from collections import defaultdict
from datetime import datetime, timedelta, timezone, time

import brain
import store
import chatdump

logger = logging.getLogger(__name__)

MSK = timezone(timedelta(hours=3))

# Эти чаты в анализ не идут
EXCLUDE_CHATS = {
    "OPS ALERT",
    "Настройка Аналитики Acme",
}
MAIN_GROUPS = {
    "Бизнес Acme",
    "ТехСап (внутренний)",
    "Отдел продаж",
}

# Бренды и ключи для матчинга по имени чата (case-insensitive)
BRAND_KEYS = {
    "Alpha":      ["alpha"],
    "Bravo":      ["bravo"],
    "Charlie":    ["charlie"],
    "Delta":      ["delta"],
    "Echo":       ["echo"],
    "Foxtrot":    ["foxtrot"],
    "Kilo":       ["kilo"],
    "Lima":       ["lima"],
    "Mike":       ["mike"],
    "November":   ["november"],
    "Oscar":      ["oscar"],
    "Papa":       ["papa"],
    "Quebec":     ["quebec"],
    "Romeo":      ["romeo"],
}

# приоритет выбора целевого чата для отправки
TARGET_PRI = ["managers", "[m]", "[b]", "business", "bussines", "integration", "[t]", "tech"]

# Маркер в тексте reminder'а, по которому отличаем «свои» (для идемпотентности)
MORNING_TAG = "[#morning-schedule]"

# Дефолтное время отправки (МСК)
SEND_TIME_DEFAULT = time(9, 30)
SEND_TIME_INTERNAL = time(8, 30)   # внутренние вопросы (BDM/Head Support) — раньше


# ── вспомогательное ───────────────────────────────────────────────

def _detect_brand(chat_name: str):
    nl = chat_name.lower()
    for brand, keys in BRAND_KEYS.items():
        for k in keys:
            if k in nl:
                return brand
    return None


def _find_target_chat(chat_names):
    """Из чатов одного бренда выбрать куда отправлять."""
    for pri in TARGET_PRI:
        for nm in chat_names:
            if pri in nm.lower():
                return nm
    return chat_names[0] if chat_names else None


def _msk_now():
    return datetime.now(MSK)


def _send_time(slot: time) -> float:
    """Ставим на сегодня в slot МСК. Если уже прошло — сейчас+5мин."""
    now = _msk_now()
    target = now.replace(hour=slot.hour, minute=slot.minute, second=0, microsecond=0)
    if now > target:
        target = now + timedelta(minutes=5)
    return target.timestamp()


def _purge_unfired_today():
    """Удаляет из reminders.json все НЕсработавшие записи с MORNING_TAG (идемпотентность)."""
    import json
    import os
    path = store.REM_FILE
    if not os.path.exists(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            arr = json.load(f)
    except Exception:
        return 0
    kept = [r for r in arr if r.get("fired") or MORNING_TAG not in (r.get("text") or "")]
    removed = len(arr) - len(kept)
    if removed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kept, f, ensure_ascii=False)
    return removed


# ── извлечение указаний Lead ──────────────────────────────────────

LEAD_NAME_RE = re.compile(r"\blead\b|\bлид\b", re.I)
DIRECTIVE_RE = re.compile(
    r"\b(не\s*включ|невключ|включа?ть|включай|вкл\s*[аоey]|снимит|снима|остан|выключ|"
    r"отключ|приостанов|приостан|подавай|трафик|каскад|канал|метод|приоритет)\b", re.I)


def _extract_lead_directives(business_msgs, hours=72):
    cut = (_msk_now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")
    out = []
    for m in business_msgs:
        if m["date"][:16] < cut:
            continue
        if not LEAD_NAME_RE.search(m["sender"]):
            continue
        if DIRECTIVE_RE.search(m["text"]):
            out.append(m)
    return out


def _blocked_brands_from_directives(directives):
    """Кто под запретом включения — из директив Lead."""
    blocked = set()
    for d in directives:
        t_low = d["text"].lower()
        # «X не включаем» / «X невключаем» / «не включайте X»
        block_phrase = bool(re.search(
            r"(не\s*включа|невключа|никак|вообще\s*ни|остан\w*\s+всё|снимит\w*\s+(?:все\s+)?терминалы)",
            t_low))
        if not block_phrase:
            continue
        for brand, keys in BRAND_KEYS.items():
            if any(k in t_low for k in keys):
                blocked.add(brand)
    return blocked


# ── ядро анализа ──────────────────────────────────────────────────

def _build_prompt(brand, target_chat, brand_transcript, lead_block, db_facts_str):
    return f"""Ты — Account Manager Acme. Сгенерируй ОДНО утреннее сообщение для отправки в чат с мерчантом {brand} (чат: «{target_chat}»).

ТРЕБОВАНИЯ К ТЕКСТУ:
- Тон: спокойный, деловой, как в реальных переписках AM Acme / AM Acme.
- Начало: «Коллеги, доброго утра!» или «Доброе утро, [Имя]!» (если в чате есть конкретный собеседник по имени).
- Длина 3–7 строк. Без эмодзи (кроме одного в начале при необходимости).
- НЕ выдумывай факты. Используй только то что в КОНТЕКСТЕ и в ЦИФРАХ ниже.
- Если контекст показывает что мерч просил остановить трафик / разбираемся — отметь готовность к возобновлению при их сигнале.
- Если у нас есть свежие цифры за вчера — упомяни их (кратко).
- Закончи открытым вопросом или предложением к действию.

ЕСЛИ ЕСТЬ ВНУТРЕННЕЕ УКАЗАНИЕ LEAD (запрет/особый режим) — ОБЯЗАТЕЛЬНО учти:
{lead_block or "(нет особых указаний)"}

КОНТЕКСТ ЧАТА (последние сообщения):
{brand_transcript[-7000:]}

ЦИФРЫ ИЗ РЕПЛИКИ ЗА 7 ДНЕЙ:
{db_facts_str or "(нет данных)"}

ВЫВЕДИ ТОЛЬКО ТЕКСТ СООБЩЕНИЯ. Без префиксов «Текст:», «Ответ:» и подобных. Один связный отправляемый текст."""


def _format_reminder(brand, target_chat, send_dt, payload_text, ctx_short, lead_note):
    lines = [
        f"🔔 <b>Пора отправить</b> · {brand}",
        f"📍 Чат: <code>{target_chat}</code>",
        f"⏰ {send_dt.strftime('%H:%M МСК · %d.%m')}",
    ]
    if lead_note:
        lines.append(f"⚠️ {lead_note}")
    lines.extend([
        "",
        "🧩 <b>Контекст:</b>",
        ctx_short,
        "",
        "📨 <b>Готовый текст (копируй):</b>",
        "",
        payload_text.strip(),
        "",
        MORNING_TAG,
    ])
    return "\n".join(lines)


def _short_ctx(msgs, n=3):
    last = msgs[-n:]
    out = []
    for m in last:
        snip = re.sub(r"\s+", " ", m["text"])[:160]
        out.append(f"  · {m['date']} {m['sender']}: {snip}")
    return "\n".join(out) or "(нет свежих сообщений)"


def _db_facts_for_brand(brand):
    """Безопасно достаёт сводку по бренду из реплики через data.py.
    Возвращает короткую строку. Не падает если что-то не получилось."""
    try:
        import data as data_mod
    except Exception:
        return ""
    try:
        # пытаемся через resolve_merchant + merchant_overview
        m = data_mod.resolve_merchant(brand)
        if not m:
            return ""
        ov = data_mod.merchant_overview(m)
        last7 = ov.get("last7d") or {}
        parts = []
        if "total" in last7:
            parts.append(f"заявок 7д: {last7.get('total')}")
        if "success" in last7:
            parts.append(f"опл: {last7.get('success')}")
        if "conv_pct" in last7:
            parts.append(f"конв в опл {last7.get('conv_pct'):.1f}%")
        if "paid_sum" in last7:
            parts.append(f"оборот {int(last7.get('paid_sum')):,}".replace(",", " "))
        return " · ".join(parts) if parts else ""
    except Exception as e:
        logger.warning(f"db facts {brand}: {e}")
        return ""


def analyze_and_schedule(raw_bytes):
    """Главная точка входа. raw_bytes — содержимое result.json от owner'а.

    Возвращает dict со сводкой: {n_chats, n_brands, n_scheduled, scheduled[],
    blocked[], directives[], errors[]}."""

    chats = chatdump.parse_all_chats(raw_bytes)
    if not chats:
        return {"error": "Не удалось распарсить выгрузку (пустой chats.list)"}

    # 1) Игнор-чаты
    chats = [c for c in chats if c["chat"] and c["chat"] not in EXCLUDE_CHATS]

    # 2) Главные группы — за 7 дней
    cut_7d = (_msk_now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    main_recent = {}
    for c in chats:
        if c["chat"] in MAIN_GROUPS:
            main_recent[c["chat"]] = [m for m in c["msgs"] if m["date"][:16] >= cut_7d]

    # 3) Директивы Lead — из «Бизнес Acme»
    biz_msgs = main_recent.get("Бизнес Acme") or []
    directives = _extract_lead_directives(biz_msgs)
    blocked = _blocked_brands_from_directives(directives)

    # 4) Мерч-чаты по бренду
    brand_chats = defaultdict(list)
    for c in chats:
        if c["chat"] in MAIN_GROUPS:
            continue
        brand = _detect_brand(c["chat"])
        if brand:
            brand_chats[brand].append(c)

    # 5) Чистим старые non-fired напоминания с нашим тегом (идемпотентность)
    purged = _purge_unfired_today()

    # 6) Для каждого бренда — генерим сообщение и ставим в очередь
    scheduled = []
    skipped = []
    cut_48 = (_msk_now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")

    for brand, chs in brand_chats.items():
        all_msgs = []
        for c in chs:
            all_msgs.extend(c["msgs"])
        all_msgs.sort(key=lambda m: m["date"])
        recent = [m for m in all_msgs if m["date"][:16] >= cut_48]

        if not recent and not all_msgs[-30:]:
            skipped.append((brand, "нет свежей переписки"))
            continue

        chat_names = [c["chat"] for c in chs]
        target_chat = _find_target_chat(chat_names)
        if not target_chat:
            skipped.append((brand, "не найден целевой чат"))
            continue

        # Блок по запрету Lead
        lead_note = ""
        if brand in blocked:
            lead_note = (f"ЗАПРЕТ от Lead по {brand}. "
                         f"В чат сегодня НЕ пишем. Если напишут сами — нейтральный ответ, "
                         f"без обсуждения сроков.")

        # Все директивы Lead упоминающие этот бренд (для prompt'а)
        lead_block = ""
        for d in directives:
            tl = d["text"].lower()
            if any(k in tl for k in BRAND_KEYS[brand]):
                lead_block += f"  - {d['date']} Lead: {d['text'][:300]}\n"

        # Транскрипт для GPT — последние 30 сообщений
        used = recent[-30:] or all_msgs[-30:]
        transcript = "\n".join(f"[{m['date']}] {m['sender']}: {m['text']}" for m in used)
        db_str = _db_facts_for_brand(brand)
        ctx_short = _short_ctx(used)

        # 7) Генерация
        try:
            if brand in blocked:
                # под запретом — текст не нужен, делаем уведомление-предупреждение
                payload = (f"⚠️ В чат сегодня не пишем (запрет Lead).\n"
                           f"Если Romeo напишут сами — нейтрально: "
                           f"«Коллеги, по нашей стороне сейчас идут технические работы, "
                           f"как только будем готовы — обязательно отпишем.»")
                slot = SEND_TIME_INTERNAL
            else:
                prompt = _build_prompt(brand, target_chat, transcript, lead_block, db_str)
                payload = brain.run(prompt)
                slot = SEND_TIME_DEFAULT
        except Exception as e:
            logger.error(f"brain gen {brand}: {e}")
            skipped.append((brand, f"GPT ошибка: {str(e)[:120]}"))
            continue

        send_ts = _send_time(slot)
        send_dt = datetime.fromtimestamp(send_ts, MSK)
        reminder_text = _format_reminder(brand, target_chat, send_dt, payload, ctx_short, lead_note)
        rid = store.add_reminder(send_ts, reminder_text)
        scheduled.append({
            "brand": brand, "target": target_chat,
            "send_at": send_dt.strftime("%H:%M %d.%m"),
            "rid": rid, "blocked": brand in blocked,
        })

    # 8) Соберём короткий summary для ответа в чат
    return {
        "n_chats": len(chats),
        "n_brands": len(brand_chats),
        "n_scheduled": len(scheduled),
        "scheduled": scheduled,
        "blocked": sorted(blocked),
        "directives": [(d["date"], d["sender"], d["text"][:200]) for d in directives[-5:]],
        "purged": purged,
        "skipped": skipped,
    }


def format_summary(res: dict) -> str:
    """Короткое сообщение которое бот отправит owner'у сразу после анализа."""
    if "error" in res:
        return f"❌ {res['error']}"
    lines = [
        f"📂 <b>Утренний анализ готов</b>",
        f"  • чатов проанализировано: {res['n_chats']}",
        f"  • брендов: {res['n_brands']}",
        f"  • поставлено напоминаний на сегодня: <b>{res['n_scheduled']}</b>",
    ]
    if res.get("purged"):
        lines.append(f"  • вычищено старых на день: {res['purged']}")
    if res.get("blocked"):
        lines.append(f"  • ❌ под запретом Lead: {', '.join(res['blocked'])}")
    lines.append("")
    if res["scheduled"]:
        lines.append("🗓 <b>Очередь на сегодня:</b>")
        for s in res["scheduled"]:
            mark = "❌" if s["blocked"] else "🎯"
            lines.append(f"  {mark} {s['send_at']} · <b>{s['brand']}</b> → <code>{s['target']}</code>")
    if res.get("skipped"):
        lines.append("")
        lines.append("⏭ Пропущено:")
        for b, why in res["skipped"][:5]:
            lines.append(f"  • {b} — {why}")
    if res.get("directives"):
        lines.append("")
        lines.append("📢 Свежие распоряжения Lead:")
        for d, u, tx in res["directives"]:
            lines.append(f"  · {d} {u}: {tx[:120]}")
    return "\n".join(lines)
