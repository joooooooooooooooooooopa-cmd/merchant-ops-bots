"""Конфиг дашборда. Всё из env (.env)."""
import os
from dotenv import load_dotenv

load_dotenv()

# Read-only реплика Core (VPC-only, доступна только с ВПС Acme)
ANALYTICS_DB_URL = os.getenv("ANALYTICS_DB_URL", "")

# Локальная БД дашборда (конструктор интеграции, passkey-креды, сессии)
DB_PATH = os.getenv("DASH_DB_PATH", "dashboard.db")

# Сеть: слушаем ТОЛЬКО localhost — доступ снаружи только через SSH-туннель
HOST = os.getenv("DASH_HOST", "127.0.0.1")
PORT = int(os.getenv("DASH_PORT", "8077"))

# WebAuthn / passkey. RP_ID и ORIGIN должны совпадать с тем, что видит браузер.
# При доступе через SSH-туннель браузер открывает http://localhost:PORT — это secure-context.
RP_ID = os.getenv("DASH_RP_ID", "localhost")
RP_NAME = "OpsDashboard"
ORIGIN = os.getenv("DASH_ORIGIN", f"http://localhost:{PORT}")

# Секрет для подписи сессионных кук (openssl rand -hex 32)
SESSION_SECRET = os.getenv("DASH_SESSION_SECRET", "")
SESSION_MAX_AGE = int(os.getenv("DASH_SESSION_MAX_AGE", str(12 * 3600)))  # 12 ч
