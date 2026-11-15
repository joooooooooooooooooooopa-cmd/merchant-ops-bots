import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))   # приватный бот: только этот ID
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
ANALYTICS_DB_URL = os.getenv("ANALYTICS_DB_URL", "")
# Приоритетные мерчанты (фокус БДМ)
PRIORITY = [m.strip() for m in os.getenv("PRIORITY_MERCHANTS", "Bravo,Alpha").split(",") if m.strip()]
# Целевая конверсия (для бенчмарка) и порог эскалации тех/гейт-проблемы (минуты)
TARGET_CONV = float(os.getenv("TARGET_CONV", "75"))
ESCALATE_MIN = int(os.getenv("ESCALATE_MIN", "40"))
# Тихие часы (МСК): ночью алерты копятся и отдаются утром
QUIET_START = int(os.getenv("QUIET_START", "23"))
QUIET_END = int(os.getenv("QUIET_END", "9"))
# Интеграция: через сколько дней без контакта напоминать
INTEG_NUDGE_DAYS = float(os.getenv("INTEG_NUDGE_DAYS", "2"))
# Слежка за ВСЕМИ мерчами: порог просадки (п.п.) и мин. выдач за окно
ALL_MERCH_DROP_PP = float(os.getenv("ALL_MERCH_DROP_PP", "20"))
ALL_MERCH_MIN_REQ = int(os.getenv("ALL_MERCH_MIN_REQ", "40"))
# Монитор провайдеров
PROVIDER_MIN_TOTAL = int(os.getenv("PROVIDER_MIN_TOTAL", "30"))   # мин. заявок гейта за 2ч
PROVIDER_DROP_PP = float(os.getenv("PROVIDER_DROP_PP", "20"))     # просадка конв vs базы 7д
PROVIDER_NOACCT_SHARE = float(os.getenv("PROVIDER_NOACCT_SHARE", "50"))  # % фейлов без счёта
# Монитор антифрода
ANTIFRAUD_MIN_TOTAL = int(os.getenv("ANTIFRAUD_MIN_TOTAL", "40"))
ANTIFRAUD_RATE = float(os.getenv("ANTIFRAUD_RATE", "25"))        # % реза за 2ч
