# ReportBot

Приватный бот выгрузок owner. По одному действию собирает Excel за период в **3 форматах**:

- 📤 **для мерча** — без `gate`/`terminal`/`external_id`
- 🏦 **для провайдера** — по гейту/терминалу, без `merchant_order`
- 📦 **внутренняя** — всё, со ставкой `fee_from_merchant`

В каждом файле 4 листа: **Параметры · Сводка · По дням · Анализ причин · Транзакции**.

## Стек

- Python 3.12, python-telegram-bot 21.5, psycopg2, openpyxl
- Данные: read-only реплика Core (`ANALYTICS_DB_URL`, VPC-only)
- Деплой: ВПС (`HOST=user@host`), systemd `reportbot`

## Кто видит бота

Только owner (`OWNER_ID=`). Любой другой `user_id` блокируется
до ввода `ACCESS_KEY` (мастер-ключ, как у CRM/Sverka/Test-ботов).

## SQL-формулы

Портированы 1:1 из `CRMBot/metabase.py` — цифры **бьются** с CRM-ботом и Metabase:

```
успех        = payment_status_id IN (3,6)        — complete + partial_complete
выдача (ISS) = card-реквизит OR route_result.status='success' OR успех
EISS         = эффективная выдача (для потоков без выдачи → total)
метод        = COALESCE(requisite_type, provider_payment_type, коридор-из-имени-продукта)
мерчант      = mr.iname (фильтр), отображение COALESCE(brand_name, iname)
тесты        = NOTEST_FULL (is_test=false + блок мерчей с 'test' в имени)
время        = (created_at AT TIME ZONE 'Europe/Moscow')::date
```

## Развёртывание

### Первый раз на ВПС:

```bash
# 1) Скопировать .env (BOT_TOKEN/OWNER_ID/ACCESS_KEY/ANALYTICS_DB_URL)
scp -i ~/.ssh/ops_vps .env $HOST:/home/ubuntu/ReportBot/.env

# 2) venv
ssh -i ~/.ssh/ops_vps $HOST
  mkdir -p ~/ReportBot && cd ~/ReportBot
  ~/.local/bin/uv venv -p 3.12 .venv

# 3) Один раз — поставить systemd unit:
./systemd_unit.sh

# 4) Запуск:
./deploy.sh
```

### Обновление:

```bash
./deploy.sh
```

`deploy.sh` сам делает rsync (handlers/ в подпапку, иначе rsync ложит файлы в корень — баг,
проверенный на CRM-боте) и `sudo systemctl restart reportbot`.

## Логи

```bash
ssh -i ~/.ssh/ops_vps $HOST 'journalctl -u reportbot -f'
```

## Локально (для отладки)

```bash
cp .env.example .env  # вписать BOT_TOKEN и ANALYTICS_DB_URL (через SSH-туннель)
uv venv -p 3.12 .venv
source .venv/bin/activate
uv pip install -r requirements.txt
python bot.py
```

**Замечание:** реплика VPC-only (приватный IP 10.0.x), с ноутбука недоступна.
Локально можно поднять SSH-туннель: `ssh -i ~/.ssh/ops_vps -L 5432:replica.example.internal:5432 $HOST`,
тогда в `.env` поставить `ANALYTICS_DB_URL=postgresql://read_user:PASSWORD@localhost:5432/core`.

## Структура

```
bot.py              — entry point (polling, обработчики)
config.py           — env, OWNER_ID, ACCESS_KEY, OUTPUT_DIR
db.py               — SQL-слой к реплике Core (формулы 1:1 с CRM)
builder.py          — Excel-сборка (3 шаблона: merchant/provider/internal)
keys.py             — доступ: OWNER lock + мастер-ключ → allowlist
handlers/
  ├── flow.py       — wizard: тип → сущность → аудитория → период → файл
  ├── keyboards.py  — inline-кнопки, главное меню
  └── utils.py      — парсинг дат, форматтеры, MSK-tz
deploy.sh           — rsync + systemctl restart
systemd_unit.sh     — установка unit (один раз)
start.sh            — локальный запуск
.env.example
```

## UX (что видит юзер)

1. `/start` → главное меню (📦 Новая выгрузка / 🔁 Повторить / 🩺 Реплика / ℹ️ Что умеет).
2. **📦 Новая выгрузка**:
   - тип → 🏪 По мерчу / 🏦 По провайдеру / 🌍 Все мерчанты
   - пикер сущности (список с трафиком за 30д, поиск по подстроке)
   - аудитория → ДЛЯ МЕРЧА / ДЛЯ ПРОВАЙДЕРА / ВНУТРЕННЯЯ
   - период → Сегодня / Вчера / 7д / 30д / 📅 произвольный (`DD.MM` или `DD.MM-DD.MM`)
3. Бот шлёт `.xlsx` файл.

## Safeguard

Если за период > 200 000 заявок — бот предлагает сузить период (Telegram-лимит 50 MB
+ openpyxl грузит всё в память).

## Backup

Файлы выгрузок — в `/tmp/ops_reports/` на ВПС (`OUTPUT_DIR` в `.env`). Не бекапим.
Allowlist `report_keys.json` — в корне бота.
