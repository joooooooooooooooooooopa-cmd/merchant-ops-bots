"""Хелпер для Metabase API (Acme, за Teleport). Кука читается из cookie.txt рядом."""
import json, os, urllib.request, urllib.error

BASE = os.getenv("METABASE_URL", "https://metabase.example.com")
DB_ID = 2  # Core
HERE = os.path.dirname(os.path.abspath(__file__))
_CK = os.path.join(HERE, "cookie.txt")
COOKIE = open(_CK).read().strip() if os.path.exists(_CK) else ""

HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Cookie": COOKIE,
    "Origin": BASE,
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
}


def api(method, path, body=None):
    if not COOKIE:
        raise SystemExit("Нет cookie.txt — положи свежую куку (Copy as cURL, строка после -b) рядом со скриптом.")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.reason, "on", method, path)
        print(e.read().decode()[:2000])
        raise


def q(sql, db=DB_ID):
    """Выполнить native SQL и вернуть {cols:[...], rows:[[...]]}."""
    body = {"database": db, "type": "native", "native": {"query": sql}}
    res = api("POST", "/api/dataset", body)
    data = res.get("data", {})
    cols = [c.get("display_name") or c.get("name") for c in data.get("cols", [])]
    return {"cols": cols, "rows": data.get("rows", []), "raw": res}


def show(sql, db=DB_ID, limit=50):
    r = q(sql, db)
    print(" | ".join(str(c) for c in r["cols"]))
    print("-" * 60)
    for row in r["rows"][:limit]:
        print(" | ".join("" if v is None else str(v) for v in row))
    print(f"({len(r['rows'])} rows)")
    return r


if __name__ == "__main__":
    me = api("GET", "/api/user/current")
    print("auth:", me.get("email"), "superuser:", me.get("is_superuser"))
