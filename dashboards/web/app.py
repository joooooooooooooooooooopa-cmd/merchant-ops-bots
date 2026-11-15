"""OpsDashboard — FastAPI. Слушает 127.0.0.1 (доступ через SSH-туннель), вход по passkey.
Core — read-only (наглядность). Конструктор интеграции — локальная БД."""
import os

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel

import store
import replica
import auth
from config import HOST, PORT, SESSION_MAX_AGE

app = FastAPI(title="OpsDashboard", docs_url=None, redoc_url=None, openapi_url=None)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "kdash_session"


@app.on_event("startup")
def _startup():
    store.init_db()


# ── session helpers ──────────────────────────────────────────────
def _set_session(resp: Response):
    resp.set_cookie(COOKIE, auth.make_session(), max_age=SESSION_MAX_AGE,
                    httponly=True, samesite="strict", path="/")


def require_auth(request: Request):
    if not auth.valid_session(request.cookies.get(COOKIE, "")):
        raise HTTPException(status_code=401, detail="not authenticated")
    return True


# ── статика ──────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ── auth ─────────────────────────────────────────────────────────
def _rp(request: Request):
    """RP_ID/origin по домену запроса: localhost (SSH-туннель) ИЛИ ts.net (Tailscale, телефон).
    Каждый домен — свои passkey. Так и desktop, и телефон работают."""
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "localhost:8077")
    hostname = host.split(":")[0]
    if hostname in ("localhost", "127.0.0.1"):
        return "localhost", f"http://{host}"
    proto = request.headers.get("x-forwarded-proto", "https")
    return hostname, f"{proto}://{host}"


@app.get("/api/auth/state")
def auth_state(request: Request):
    rp_id, _o = _rp(request)
    return {"has_credentials": store.has_credentials(rp_id),
            "authenticated": auth.valid_session(request.cookies.get(COOKIE, ""))}


class RegVerify(BaseModel):
    flow: str
    credential: dict
    label: str | None = "passkey"


@app.post("/api/auth/register/options")
def reg_options(request: Request):
    # регистрация на этом домене: если для него нет passkey (первый) ИЛИ юзер залогинен
    rp_id, origin = _rp(request)
    if store.has_credentials(rp_id) and not auth.valid_session(request.cookies.get(COOKIE, "")):
        raise HTTPException(status_code=403, detail="registration closed")
    options, flow = auth.registration_options(rp_id, origin)
    return JSONResponse({"options": options, "flow": flow})


@app.post("/api/auth/register/verify")
def reg_verify(body: RegVerify, request: Request, response: Response):
    rp_id, origin = _rp(request)
    if store.has_credentials(rp_id) and not auth.valid_session(request.cookies.get(COOKIE, "")):
        raise HTTPException(status_code=403, detail="registration closed")
    try:
        auth.registration_verify(body.flow, _json(body.credential), rp_id, origin, body.label)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    _set_session(response)  # после регистрации passkey — сразу логиним
    return {"ok": True}


@app.post("/api/auth/login/options")
def login_options(request: Request):
    rp_id, _o = _rp(request)
    try:
        options, flow = auth.authentication_options(rp_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"options": options, "flow": flow})


class LoginVerify(BaseModel):
    flow: str
    credential: dict


@app.post("/api/auth/login/verify")
def login_verify(body: LoginVerify, request: Request, response: Response):
    rp_id, origin = _rp(request)
    try:
        auth.authentication_verify(body.flow, body.credential, rp_id, origin)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
    _set_session(response)
    return {"ok": True}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


def _json(d):
    import json
    return json.dumps(d)


# ── данные (read-only Core) ─────────────────────────────────────
@app.get("/api/merchants")
def api_merchants(days: int = 1, _=Depends(require_auth)):
    return {"days": days, "ok": replica.healthy(), "merchants": replica.merchants(days)}


@app.get("/api/merchant/{mid}")
def api_merchant(mid: str, days: int = 7, _=Depends(require_auth)):
    return {"endpoints": replica.merchant_endpoints(mid),
            "gates": replica.merchant_gates(mid, days),
            "declines": replica.merchant_declines(mid, days),
            "offer": replica.merchant_offer(mid),
            "method_conv": replica.merchant_method_conv(mid, days),
            "conv_trend": replica.merchant_conv_trend(mid, 14)}


def _today_msk():
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")


@app.get("/api/monday")
def api_monday(days: int = 1, _=Depends(require_auth)):
    ai = replica.action_items(days)
    tracked = [t for t in store.list_tracked() if t["stage"] in ("lead", "integration")]
    ai["integrations"] = [{"id": t["id"], "name": t["name"], "stage": t["stage"],
                           "sold": t["sold"]} for t in tracked]
    day = _today_msk()
    ai["day"] = day
    ai["done"] = store.list_done(day)
    ai["checklist"] = store.list_checklist(day)
    ai["events"] = replica.events(days)
    return ai


@app.get("/api/withdrawals")
def api_withdrawals(days: int = 7, _=Depends(require_auth)):
    return {"days": days, "withdrawals": replica.withdrawals(days)}


class DoneBody(BaseModel):
    key: str
    done: bool = True


@app.post("/api/done")
def api_done(body: DoneBody, _=Depends(require_auth)):
    store.set_done(body.key, _today_msk(), body.done)
    return {"ok": True}


class CheckBody(BaseModel):
    text: str
    body: str | None = None


@app.post("/api/checklist")
def api_check_add(body: CheckBody, _=Depends(require_auth)):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text required")
    cid = store.add_check(_today_msk(), body.text.strip(), body.body)
    return {"id": cid}


class CheckToggle(BaseModel):
    done: bool | None = None
    body: str | None = None


@app.patch("/api/checklist/{cid}")
def api_check_toggle(cid: int, body: CheckToggle, _=Depends(require_auth)):
    if body.done is not None:
        store.toggle_check(cid, body.done)
    if body.body is not None:
        store.update_check_body(cid, body.body)
    return {"ok": True}


@app.delete("/api/checklist/{cid}")
def api_check_del(cid: int, _=Depends(require_auth)):
    store.delete_check(cid)
    return {"ok": True}


@app.get("/api/notes/{mid}")
def api_notes(mid: str, _=Depends(require_auth)):
    return {"notes": store.list_notes(mid)}


class NoteBody(BaseModel):
    text: str


@app.post("/api/notes/{mid}")
def api_note_add(mid: str, body: NoteBody, _=Depends(require_auth)):
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="text required")
    nid = store.add_note(mid, body.text.strip())
    return {"id": nid}


@app.delete("/api/note/{nid}")
def api_note_del(nid: int, _=Depends(require_auth)):
    store.delete_note(nid)
    return {"ok": True}


@app.get("/api/conv-trend")
def api_conv_trend(days: int = 14, _=Depends(require_auth)):
    return {"trend": replica.conv_trend(days)}


@app.get("/api/boss-report")
def api_boss_report(days: int = 7, _=Depends(require_auth)):
    rep = replica.boss_report(days)
    tracked = store.list_tracked()
    by_stage = {}
    sold_total = 0
    for t in tracked:
        by_stage[t["stage"]] = by_stage.get(t["stage"], 0) + 1
        sold_total += len(t["sold"])
    rep["constructor"] = {"total": len(tracked), "by_stage": by_stage, "sold_methods": sold_total}
    return rep


@app.get("/api/overview")
def api_overview(days: int = 1, _=Depends(require_auth)):
    return replica.overview(days)


@app.get("/api/gate-scorecard")
def api_gate_scorecard(days: int = 1, _=Depends(require_auth)):
    return {"gates": replica.gate_scorecard(days)}


@app.get("/api/method-matrix")
def api_method_matrix(days: int = 1, _=Depends(require_auth)):
    return replica.method_matrix(days)


@app.get("/api/method-catalog")
def api_method_catalog(days: int = 1, _=Depends(require_auth)):
    return replica.method_catalog(days)


@app.get("/api/merchant-list")
def api_merchant_list(_=Depends(require_auth)):
    return {"merchants": replica.merchant_list()}


@app.get("/api/providers")
def api_providers(_=Depends(require_auth)):
    return {"providers": replica.providers()}


@app.get("/api/provider")
def api_provider(gate: str, _=Depends(require_auth)):
    return replica.provider_detail(gate)


# ── конструктор интеграции (локально) ────────────────────────────
@app.get("/api/tracked")
def api_tracked(_=Depends(require_auth)):
    return {"tracked": store.list_tracked(), "stages": store.STAGES}


class TrackedNew(BaseModel):
    name: str
    stage: str | None = "lead"
    note: str | None = ""


@app.post("/api/tracked")
def api_tracked_add(body: TrackedNew, _=Depends(require_auth)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name required")
    tid = store.add_tracked(body.name.strip(), body.stage or "lead", body.note or "")
    return {"id": tid}


class TrackedPatch(BaseModel):
    name: str | None = None
    stage: str | None = None
    note: str | None = None
    merchant_id: str | None = None


@app.patch("/api/tracked/{tid}")
def api_tracked_patch(tid: int, body: TrackedPatch, _=Depends(require_auth)):
    store.update_tracked(tid, **body.model_dump(exclude_none=True))
    return {"ok": True}


class SoldBody(BaseModel):
    sold: list


@app.post("/api/tracked/{tid}/sold")
def api_tracked_sold(tid: int, body: SoldBody, _=Depends(require_auth)):
    store.set_sold(tid, body.sold)
    return {"ok": True}


class JourneyBody(BaseModel):
    text: str


@app.post("/api/tracked/{tid}/journey")
def api_tracked_journey(tid: int, body: JourneyBody, _=Depends(require_auth)):
    if body.text.strip():
        store.add_journey(tid, body.text.strip())
    return {"ok": True}


@app.delete("/api/tracked/{tid}")
def api_tracked_delete(tid: int, _=Depends(require_auth)):
    store.delete_tracked(tid)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
