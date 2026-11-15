"""Слой надёжности экосистемы: бэкап state-файлов всех ботов + watchdog (живость сервисов,
реплика, диск, флаппинг). Без OpenAI — чистая проверка. Запускается джобами ассистента."""
import os
import glob
import shutil
import zipfile
import subprocess
from datetime import datetime, timezone, timedelta

import data

MSK = timezone(timedelta(hours=3))
HOME = os.getenv("BOTS_HOME", "/home/ubuntu")
BACKUP_DIR = os.getenv("BACKUP_DIR", f"{HOME}/ops_backups")
KEEP_BACKUPS = 7

SERVICES = ["crmbot", "ratebot", "reconbot", "assistantbot"]

# Что бэкапим: критичные state-файлы по ботам (ключи доступа, заметки, трекер, БД CRM).
BACKUP_GLOBS = [
    f"{HOME}/CRMBot/access_keys.json",
    f"{HOME}/CRMBot/alert_state.json",
    f"{HOME}/CRMBot/audit_log.json",
    f"{HOME}/CRMBot/merchants.db",
    f"{HOME}/RateBot/rate_keys.json",
    f"{HOME}/ReconciliationBot/sverka_keys.json",
    f"{HOME}/AssistantBot/notes.json",
    f"{HOME}/AssistantBot/integration.json",
    f"{HOME}/AssistantBot/reminders.json",
    f"{HOME}/AssistantBot/monitor_state.json",
]

DISK_WARN_PCT = 90


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as e:
        return f"err:{e}"


# ── Бэкап ──────────────────────────────────────────────────────
def make_backup():
    """Зипует существующие state-файлы. Возвращает путь к архиву (или None, если нечего)."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(MSK).strftime("%Y%m%d_%H%M")
    path = f"{BACKUP_DIR}/ops_state_{stamp}.zip"
    files = []
    for g in BACKUP_GLOBS:
        files += glob.glob(g)
    if not files:
        return None, 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            # сохраняем как <папка-бота>/<файл>
            arc = os.path.relpath(f, HOME)
            z.write(f, arc)
    _rotate()
    return path, len(files)


def _rotate():
    backups = sorted(glob.glob(f"{BACKUP_DIR}/ops_state_*.zip"))
    for old in backups[:-KEEP_BACKUPS]:
        try:
            os.remove(old)
        except Exception:
            pass


# ── Watchdog ───────────────────────────────────────────────────
def _svc_active(svc):
    return _run(["systemctl", "is-active", svc]) == "active"


def _svc_restarts(svc):
    """Кол-во авто-рестартов (флаппинг-индикатор)."""
    out = _run(["systemctl", "show", svc, "-p", "NRestarts", "--value"])
    try:
        return int(out)
    except (ValueError, TypeError):
        return 0


def _disk_pct():
    out = _run(["df", "--output=pcent", "/"])
    for line in out.splitlines():
        line = line.strip().rstrip("%")
        if line.isdigit():
            return int(line)
    return 0


def health_report():
    """Полная картина для кнопки «Статус системы»."""
    lines = []
    for s in SERVICES:
        ok = _svc_active(s)
        rs = _svc_restarts(s)
        flag = "🟢" if ok else "🔴"
        extra = f" · рестартов: {rs}" if rs else ""
        lines.append(f"{flag} <code>{s}</code>: {'active' if ok else 'НЕ РАБОТАЕТ'}{extra}")
    rep_ok = False
    try:
        rep_ok = data.test_connection()
    except Exception:
        rep_ok = False
    lines.append(f"{'🟢' if rep_ok else '🔴'} реплика БД: {'отвечает' if rep_ok else 'НЕ отвечает'}")
    dp = _disk_pct()
    lines.append(f"{'🟢' if dp < DISK_WARN_PCT else '🔴'} диск: {dp}% занято")
    bks = sorted(glob.glob(f"{BACKUP_DIR}/ops_state_*.zip"))
    if bks:
        last = os.path.basename(bks[-1]).replace("ops_state_", "").replace(".zip", "")
        lines.append(f"💾 последний бэкап: {last} (всего {len(bks)})")
    else:
        lines.append("💾 бэкапов ещё нет")
    return "\n".join(lines)


def detect_problems(prev_restarts):
    """Возвращает (problems:list[str], restarts:dict) — для авто-алерта watchdog."""
    problems = []
    restarts = {}
    for s in SERVICES:
        if not _svc_active(s):
            problems.append(f"🔴 Сервис <code>{s}</code> НЕ работает!")
        r = _svc_restarts(s)
        restarts[s] = r
        if prev_restarts and r > prev_restarts.get(s, r):
            problems.append(f"⚠️ <code>{s}</code> перезапустился (рестартов: {r}) — возможен краш-луп.")
    try:
        if not data.test_connection():
            problems.append("🔴 Реплика БД не отвечает — боты не видят данные!")
    except Exception:
        problems.append("🔴 Реплика БД: ошибка подключения.")
    dp = _disk_pct()
    if dp >= DISK_WARN_PCT:
        problems.append(f"🔴 Диск заполнен на {dp}% — нужно чистить.")
    return problems, restarts
