"""ReportBot — конфиг."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Главный (и единственный) владелец. Никто кроме него отвечать не будет.
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Мастер-ключ (как в CRMBot/Sverka/Test) — на случай если зайдёт с другого
# Telegram-аккаунта. Один раз ввёл → user_id добавлен в allowlist.
ACCESS_KEY = os.getenv("ACCESS_KEY", "").strip()

# Реплика Core (VPC-only, доступна только с ВПС).
ANALYTICS_DB_URL = os.getenv("ANALYTICS_DB_URL", "").strip()

# Куда складывать сгенерированные xlsx до отправки (на сервере — /tmp).
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/ops_reports"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Куда писать allowlist (после ввода мастер-ключа).
ALLOWLIST_FILE = ROOT / "report_keys.json"

# Логи
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Сколько мерчей/гейтов показывать в пикере (страница).
PAGE_SIZE = 14
# За сколько дней искать «активные» мерчи/гейты (по объёму трафика).
ACTIVE_DAYS = 30
