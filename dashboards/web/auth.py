"""Passkey (WebAuthn/FIDO2) аутентификация + подписанные сессионные куки.
Регистрация passkey разрешена только если кредов ещё нет (первый вход) ИЛИ юзер уже залогинен.
Доступ к самому серверу — через SSH-туннель (слушаем 127.0.0.1), passkey — второй замок."""
import os
import time
import secrets

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from webauthn import (
    generate_registration_options, verify_registration_response,
    generate_authentication_options, verify_authentication_response, options_to_json,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    PublicKeyCredentialDescriptor, AuthenticatorSelectionCriteria,
    ResidentKeyRequirement, UserVerificationRequirement,
)

import store
from config import RP_ID, RP_NAME, ORIGIN, SESSION_SECRET, SESSION_MAX_AGE

# pending challenge: flow_token -> {"challenge": bytes, "ts": float}
_PENDING: dict[str, dict] = {}
_PENDING_TTL = 300

_serializer = URLSafeTimedSerializer(SESSION_SECRET or secrets.token_hex(32), salt="ops-dash-session")


def _gc():
    now = time.time()
    for k in [k for k, v in _PENDING.items() if now - v["ts"] > _PENDING_TTL]:
        _PENDING.pop(k, None)


def _stash(challenge: bytes) -> str:
    _gc()
    tok = secrets.token_urlsafe(24)
    _PENDING[tok] = {"challenge": challenge, "ts": time.time()}
    return tok


def _pop(tok: str):
    v = _PENDING.pop(tok or "", None)
    if not v or time.time() - v["ts"] > _PENDING_TTL:
        return None
    return v["challenge"]


# ── сессии ───────────────────────────────────────────────────────
def make_session() -> str:
    return _serializer.dumps({"t": int(time.time())})


def valid_session(token: str) -> bool:
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


# ── регистрация passkey (rp_id/origin — по домену запроса) ───────
def registration_options(rp_id=RP_ID, origin=ORIGIN):
    opts = generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_name="owner",
        user_id=b"ops-dashboard-owner",
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["cred_id"]))
            for c in store.list_credentials(rp_id)
        ],
    )
    tok = _stash(opts.challenge)
    return options_to_json(opts), tok


def registration_verify(tok: str, credential: str, rp_id=RP_ID, origin=ORIGIN, label: str = "passkey"):
    challenge = _pop(tok)
    if challenge is None:
        raise ValueError("challenge истёк, повторите")
    res = verify_registration_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        require_user_verification=False,
    )
    store.add_credential(
        cred_id=bytes_to_base64url(res.credential_id),
        public_key=bytes_to_base64url(res.credential_public_key),
        sign_count=res.sign_count,
        label=label,
        rp_id=rp_id,
    )
    return True


# ── вход по passkey ──────────────────────────────────────────────
def authentication_options(rp_id=RP_ID):
    creds = store.list_credentials(rp_id)
    if not creds:
        raise ValueError("нет зарегистрированных passkey")
    opts = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(c["cred_id"])) for c in creds
        ],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    tok = _stash(opts.challenge)
    return options_to_json(opts), tok


def authentication_verify(tok: str, credential: dict, rp_id=RP_ID, origin=ORIGIN) -> bool:
    challenge = _pop(tok)
    if challenge is None:
        raise ValueError("challenge истёк, повторите")
    cred_id = credential.get("id") or credential.get("rawId")
    rec = store.get_credential(cred_id)
    if not rec:
        raise ValueError("неизвестный passkey")
    res = verify_authentication_response(
        credential=credential,
        expected_challenge=challenge,
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=base64url_to_bytes(rec["public_key"]),
        credential_current_sign_count=rec["sign_count"],
        require_user_verification=False,
    )
    store.update_sign_count(cred_id, res.new_sign_count)
    return True
