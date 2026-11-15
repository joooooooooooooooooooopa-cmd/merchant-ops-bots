# OpsDashboard

Закрытый веб-дашборд для AM: карточки мерчей (эндпоинты, пролив за период, методы, оффер),
каталог провайдеров, конструктор интеграции. **Read-only по Core** (наглядность), конструктор —
локальная БД. **Вход только по passkey, наружу не торчит — доступ через SSH-туннель.**

## Архитектура
- FastAPI слушает `127.0.0.1:8077` (НЕ 0.0.0.0) — из интернета недоступен.
- Доступ — только через SSH-туннель к ВПС Acme (твой SSH-ключ) → второй замок — passkey (WebAuthn).
- `replica.py` — read-only SQL к Core (та же реплика, что у CRM-бота, VPC-only).
- `store.py` — локальный SQLite: passkey-креды + конструктор (мерчи на интеграции, проданные методы, путь).

## Доступ (с твоего компа)
```bash
# 1) поднять туннель: локальный 8077 -> localhost:8077 на ВПС
ssh -i ~/.ssh/ops_vps -L 8077:localhost:8077 user@your-vps -N
# 2) открыть в браузере
open http://localhost:8077
```
Первый вход → «Создать passkey» (Touch ID / Face ID / ключ). Дальше — «Войти по passkey».
Регистрация нового passkey закрыта, пока есть хоть один (или нужно быть уже залогиненным).

## Деплой на ВПС
```bash
cd /home/ubuntu/OpsDashboard
uv venv && uv pip install -r requirements.txt   # или python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # вписать ANALYTICS_DB_URL (как у CRM) + DASH_SESSION_SECRET=$(openssl rand -hex 32)
# systemd unit opsdashboard (см. ниже) -> systemctl enable --now opsdashboard
```

systemd:
```
[Unit]
Description=Acme Dashboard (passkey, localhost-only)
After=network.target
[Service]
WorkingDirectory=/home/ubuntu/OpsDashboard
ExecStart=/home/ubuntu/OpsDashboard/.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8077
Restart=on-failure
User=ubuntu
[Install]
WantedBy=multi-user.target
```

## Безопасность
- Слушает только loopback — нет публичного доступа без SSH.
- passkey (FIDO2) — фишинг-устойчиво, без паролей.
- Core только читается; писать можем лишь в свой `dashboard.db`.
