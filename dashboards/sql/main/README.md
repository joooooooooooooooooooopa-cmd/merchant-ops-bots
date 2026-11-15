# Скрипты — дашборд «Основной дашборд» (Acme)

Сборка/пересборка основного дашборда конверсии в Metabase Acme через API.

## Что собирает
- Коллекция «Основной дашборд — карточки» внутри общей коллекции «Бизнес».
- 17 карточек (KPI + графики + таблицы).
- Дашборд «Основной дашборд» с 5 фильтрами: Дата, Мерчант, Метод, Провайдер, Валюта.

## Доступ (Teleport)
Metabase за Teleport — обычный логин/пароль не проходит. Нужна cookie живой сессии из браузера:
1. Открой Metabase в Chrome, DevTools (⌘⌥I) → вкладка **Network**.
2. Обнови (⌘R), кликни любой запрос `api/...`, правый клик → **Copy as cURL (bash)**.
3. Из команды возьми строку после `-b '...'` (cookie) и:
```bash
export MB_COOKIE='metabase.SESSION=...; __Host-grv_app_session=...; metabase.DEVICE=...'
python3 build_main_dashboard.py
```
Cookie живёт недолго (TTL Teleport) — для разовой сборки норм. В файлах НЕ хранить.

## Метрики (логика)
- **успех** = `payment_status_id IN (3,6)` (complete + partial_complete).
- **выдан реквизит** = есть строка в `pricing_invoicerequisite`.
- **метод** = `COALESCE(requisite_type, route_result→terminal_data→provider_payment_type)`.
- всегда `is_test = false`.

## Нюансы Metabase Acme (важно)
- Версия MLv2: GET карточки отдаёт `dataset_query` в формате `stages`. На запись слать **legacy** `{type:native,native:{query,template-tags}}`.
- Сервисный пользователь Metabase НЕ админ → Data Model / `/api/field` дают 403 (FK-ремаппинг недоступен).
- Дропдаун-фильтр: для FK-полей — `values_source_type:"static-list"` + `values_query_type:"list"` (показывает имена, права админа не нужны).
- Фильтр «Метод» — вычисляемое значение → реализован как опциональная raw-переменная `[[ AND ... = {{method}} ]]` (пусто = не влияет на агрегаты).
- Таблицу `pricing_invoice` НЕ алиасить (ломает field-filter).
