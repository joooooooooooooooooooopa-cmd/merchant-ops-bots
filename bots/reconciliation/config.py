import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ANALYTICS_DB_URL = os.getenv("ANALYTICS_DB_URL", "")
ACCESS_KEY = os.getenv("ACCESS_KEY", "")  # мастер-ключ только из .env; пусто = отключён
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
KEYS_FILE = os.getenv("KEYS_FILE", "sverka_keys.json")
