"""RAG по докам API Acme: грузит OpenAPI-спеку (api_spec.json) и отдаёт точные детали
операций (метод, путь, тело запроса, ответы) для ответов мерчам по интеграции."""
import os
import json

SPEC_FILE = os.getenv("API_SPEC_FILE", "api_spec.json")
API_BASE = os.getenv("API_BASE", "https://api.psp.example.com/api")
DOCS_URL = "https://api.psp.example.com/api/redoc/"
_spec = None


def _load():
    global _spec
    if _spec is None:
        try:
            with open(SPEC_FILE, encoding="utf-8") as f:
                _spec = json.load(f)
        except Exception:
            _spec = {}
    return _spec


def _resolve(ref, spec):
    cur = spec
    for part in ref.lstrip("#/").split("/"):
        cur = cur.get(part, {}) if isinstance(cur, dict) else {}
    return cur


def _fields(schema, spec, seen=None, depth=0):
    if not isinstance(schema, dict) or depth > 3:
        return []
    seen = seen or set()
    if "$ref" in schema:
        r = schema["$ref"]
        if r in seen:
            return []
        seen.add(r)
        schema = _resolve(r, spec)
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            out = []
            for sub in schema[key]:
                out += _fields(sub, spec, seen, depth + 1)
            return out
    props = schema.get("properties", {})
    req = set(schema.get("required", []))
    out = []
    for name, pr in props.items():
        if "$ref" in pr:
            pr = _resolve(pr["$ref"], spec)
        typ = pr.get("type") or "object"
        if pr.get("enum"):
            typ = "enum " + str(pr["enum"])
        desc = pr.get("description") or pr.get("title") or ""
        out.append(f"{name} ({typ}{', обязательное' if name in req else ''})"
                   + (f" — {desc}" if desc else ""))
    return out


def _all_ops():
    spec = _load()
    out = []
    for path, methods in spec.get("paths", {}).items():
        for m, op in methods.items():
            if m in ("get", "post", "put", "patch", "delete"):
                out.append((m, path, op))
    return out


def _detail(m, path, op, spec):
    lines = [f"{m.upper()} {API_BASE}{path}"]
    if op.get("summary"):
        lines.append("Назначение: " + op["summary"])
    if op.get("tags"):
        lines.append("Раздел: " + ", ".join(op["tags"]))
    if op.get("description"):
        lines.append(op["description"].strip()[:600])
    params = op.get("parameters", [])
    if params:
        ps = [f"{p.get('name')} ({p.get('in')}{', обяз.' if p.get('required') else ''})" for p in params]
        lines.append("Параметры: " + "; ".join(ps))
    js = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
    if js:
        fl = _fields(js, spec)
        if fl:
            lines.append("Тело запроса:\n  - " + "\n  - ".join(fl[:40]))
    resp = op.get("responses", {})
    if resp:
        lines.append("Ответы: " + "; ".join(f"{c} {r.get('description', '')[:40]}" for c, r in resp.items()))
    return "\n".join(lines)


def overview():
    return [f"{m.upper()} {path} — {op.get('summary') or op.get('operationId')}"
            for m, path, op in _all_ops()]


def search(query, limit=4):
    spec = _load()
    if not spec:
        return {"error": "спека API не загружена"}
    q = (query or "").lower().strip()
    scored = []
    for m, path, op in _all_ops():
        hay = " ".join([path, op.get("summary", ""), op.get("description", ""),
                        op.get("operationId", ""), " ".join(op.get("tags", []))]).lower()
        score = sum(1 for w in q.split() if w and w in hay)
        if q and q in hay:
            score += 3
        if score:
            scored.append((score, m, path, op))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return {"docs_url": DOCS_URL, "all_endpoints": overview()}
    return {"docs_url": DOCS_URL,
            "operations": [_detail(m, path, op, spec) for _, m, path, op in scored[:limit]]}
