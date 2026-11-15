"""Тест-запросы /init в API Acme: любая сумма × выбранный эндпоинт(метод/провайдер).
Подпись HS256 (PATH=/init), обход Cloudflare (браузерный UA). Только тест-мерч."""
import os
import json
import hmac
import hashlib
import base64
import random
import urllib.request
import urllib.error

BASE = os.getenv("TEST_API_BASE", "https://api.psp.example.com")
MERCHANT = os.getenv("TEST_MERCHANT_ID", "")
SECRET = os.getenv("TEST_API_SECRET", "")
ENDPOINTS_FILE = os.getenv("TEST_ENDPOINTS_FILE", "test_endpoints.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
DEFAULT_AMOUNTS = [1000, 3000, 5000, 50000, 150000]


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign(body):
    params = {k: v for k, v in body.items() if k not in ("signature", "api_secret")}
    sp = {k: params[k] for k in sorted(params)}
    payload = {"PATH": "/init", "POST": sp}
    eh = _b64(json.dumps({"alg": "HS256"}, separators=(",", ":"), ensure_ascii=False).encode())
    ep = _b64(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())
    return _b64(hmac.new(SECRET.encode(), f"{eh}.{ep}".encode(), hashlib.sha256).digest())


def endpoints():
    """Список тест-эндпоинтов (метод/провайдер → endpoint_id). Расширяется правкой json."""
    try:
        with open(ENDPOINTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def init(endpoint_id, amount, currency="RUB"):
    """Один /init. Возвращает {ok, http, status, code, message, url}."""
    order = "".join(random.choice("0123456789abcdef") for _ in range(16))
    body = {
        "amount": str(amount), "order": order, "merchant_id": MERCHANT,
        "endpoint_id": endpoint_id, "currency": currency, "customer": "ops-tester",
        "finish_url": "https://google.com/?invoice=pending",
        "notification_url": "https://webhook.site",
    }
    body["signature"] = _sign(body)
    req = urllib.request.Request(
        f"{BASE}/init", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": UA,
                 "Accept": "application/json, text/plain, */*"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            code = r.status
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            data = json.loads(e.read().decode())
        except Exception:
            data = {"status": "error", "message": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "http": 0, "status": "error", "code": "NETWORK",
                "message": str(e)[:120], "url": None}
    return {
        "ok": data.get("status") in ("redirect", "success"),
        "http": code, "status": data.get("status"),
        "code": data.get("code"), "message": data.get("message"),
        "url": data.get("url"),
    }


def run(endpoint_id, amounts, currency="RUB"):
    return [{"amount": a, **init(endpoint_id, a, currency)} for a in amounts]
