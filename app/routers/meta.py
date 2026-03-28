"""
Meta OAuth router — per-user account connection.

Flow:
  1. GET  /v1/meta/auth-url              → returns Meta OAuth dialog URL
  2. POST /v1/meta/available-accounts    → exchanges code for short-lived token, returns ad accounts
  3. POST /v1/meta/connect               → exchanges to long-lived token, saves to Supabase
  4. GET  /v1/meta/accounts              → lists user's connected accounts
  5. DELETE /v1/meta/accounts/{id}       → disconnects an account
  6. POST /v1/meta/accounts/{id}/sync    → refreshes last_synced_at
  7. POST /v1/meta/data-deletion         → Meta Data Deletion Callback (required by Meta policy)
"""
import base64
import hashlib
import hmac
import json
from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import APIRouter, Form, HTTPException, Header, Request
from pydantic import BaseModel

from app.config import settings
from app.supabase.client import get_supabase

router = APIRouter()

GRAPH_BASE = "https://graph.facebook.com/v21.0"
OAUTH_SCOPES = "ads_read,ads_management,business_management,pages_read_engagement"


# ── Auth helpers ───────────────────────────────────────────────────────────────

def _extract_user_id(authorization: str | None) -> str:
    """Decode the Supabase JWT and return the user_id (sub claim)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.removeprefix("Bearer ")
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload["sub"]
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")


# ── Schemas ────────────────────────────────────────────────────────────────────

class AvailableAccountsBody(BaseModel):
    code: str


class ConnectBody(BaseModel):
    access_token: str
    ad_account_id: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/auth-url")
def get_auth_url():
    """Return the Meta OAuth dialog URL for the frontend to redirect to."""
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": settings.meta_redirect_uri,
        "scope": OAUTH_SCOPES,
        "response_type": "code",
    }
    url = f"https://www.facebook.com/v21.0/dialog/oauth?{urlencode(params)}"
    return {"url": url}


@router.post("/available-accounts")
def get_available_accounts(body: AvailableAccountsBody):
    """
    Exchange the OAuth code for a short-lived user token, then list
    the ad accounts accessible to that user.
    Returns the accounts list and the short-lived access_token so the
    frontend can pass it back to /connect without reusing the code.
    """
    # Exchange code → short-lived token
    token_params = {
        "client_id": settings.meta_app_id,
        "client_secret": settings.meta_app_secret,
        "redirect_uri": settings.meta_redirect_uri,
        "code": body.code,
    }
    token_resp = httpx.get(f"{GRAPH_BASE}/oauth/access_token", params=token_params)
    if token_resp.status_code != 200:
        detail = token_resp.json().get("error", {}).get("message", "Error al intercambiar el código de Meta")
        raise HTTPException(status_code=400, detail=detail)

    short_token: str = token_resp.json()["access_token"]

    # List accessible ad accounts
    accounts_resp = httpx.get(
        f"{GRAPH_BASE}/me/adaccounts",
        params={
            "access_token": short_token,
            "fields": "id,name,currency,timezone_name",
        },
    )
    if accounts_resp.status_code != 200:
        detail = accounts_resp.json().get("error", {}).get("message", "Error al obtener cuentas de Meta")
        raise HTTPException(status_code=400, detail=detail)

    raw_accounts = accounts_resp.json().get("data", [])
    accounts = [
        {
            "id": acc["id"],          # already in act_XXXXX format
            "name": acc.get("name", acc["id"]),
            "currency": acc.get("currency", ""),
            "timezone": acc.get("timezone_name", ""),
        }
        for acc in raw_accounts
    ]

    return {"accounts": accounts, "access_token": short_token}


@router.post("/connect")
def connect_meta_account(
    body: ConnectBody,
    authorization: str | None = Header(default=None),
):
    """
    Exchange short-lived token → long-lived token (60-day).
    Fetch the chosen ad account's details and store everything in Supabase.
    """
    user_id = _extract_user_id(authorization)

    # Exchange short-lived → long-lived token
    ll_resp = httpx.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.meta_app_id,
            "client_secret": settings.meta_app_secret,
            "fb_exchange_token": body.access_token,
        },
    )
    if ll_resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=ll_resp.json().get("error", {}).get("message", "Error al obtener token de larga duración"),
        )

    ll_data = ll_resp.json()
    long_token: str = ll_data["access_token"]
    # token_type=bearer, expires_in in seconds (usually 5184000 = 60 days)
    expires_in: int = ll_data.get("expires_in", 5184000)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Ensure ad_account_id has act_ prefix
    ad_account_id = body.ad_account_id
    if not ad_account_id.startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"

    # Fetch Meta user ID + account name/currency with the long-lived token
    me_resp = httpx.get(
        f"{GRAPH_BASE}/me",
        params={"access_token": long_token, "fields": "id"},
    )
    meta_user_id: str = me_resp.json().get("id", "") if me_resp.status_code == 200 else ""

    acc_resp = httpx.get(
        f"{GRAPH_BASE}/{ad_account_id}",
        params={
            "access_token": long_token,
            "fields": "id,name,currency,timezone_name",
        },
    )
    acc_data = acc_resp.json() if acc_resp.status_code == 200 else {}

    account_name = acc_data.get("name", ad_account_id)
    currency = acc_data.get("currency", "")
    timezone_name = acc_data.get("timezone_name", "")

    # Upsert into Supabase (one row per user+ad_account)
    db = get_supabase()
    result = (
        db.table("meta_accounts")
        .upsert(
            {
                "user_id": user_id,
                "meta_user_id": meta_user_id,
                "meta_ad_account_id": ad_account_id,
                "name": account_name,
                "currency": currency,
                "timezone": timezone_name,
                "access_token": long_token,
                "token_expires_at": expires_at.isoformat(),
                "status": "active",
                "connected_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="user_id,meta_ad_account_id",
        )
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=500, detail="Error guardando la cuenta en la base de datos")

    row = result.data[0]
    return {
        "id": row["id"],
        "meta_ad_account_id": row["meta_ad_account_id"],
        "name": row["name"],
        "currency": row["currency"],
        "timezone": row.get("timezone", ""),
        "status": row["status"],
        "last_synced_at": row.get("last_synced_at"),
    }


@router.get("/accounts")
def list_accounts(authorization: str | None = Header(default=None)):
    """List all Meta accounts connected by the current user."""
    user_id = _extract_user_id(authorization)
    db = get_supabase()
    result = (
        db.table("meta_accounts")
        .select("id,meta_ad_account_id,name,currency,timezone,status,last_synced_at,connected_at")
        .eq("user_id", user_id)
        .order("connected_at", desc=True)
        .execute()
    )
    return result.data


@router.delete("/accounts/{account_id}", status_code=204)
def disconnect_account(account_id: str, authorization: str | None = Header(default=None)):
    """Remove a connected Meta account (only the owner can do this)."""
    user_id = _extract_user_id(authorization)
    db = get_supabase()
    db.table("meta_accounts").delete().eq("id", account_id).eq("user_id", user_id).execute()


@router.post("/accounts/{account_id}/sync")
def sync_account(account_id: str, authorization: str | None = Header(default=None)):
    """Update last_synced_at timestamp for the account."""
    user_id = _extract_user_id(authorization)
    db = get_supabase()
    result = (
        db.table("meta_accounts")
        .update({"last_synced_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", account_id)
        .eq("user_id", user_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Cuenta no encontrada")
    return {"status": "synced", "last_synced_at": result.data[0]["last_synced_at"]}


# ── Meta Data Deletion Callback ────────────────────────────────────────────────

def _parse_signed_request(signed_request: str, app_secret: str) -> dict:
    """
    Verify and decode Meta's signed_request parameter.
    Format: base64url(signature).base64url(payload)
    Signature = HMAC-SHA256(payload_b64, app_secret)
    Raises HTTPException if the signature is invalid.
    """
    try:
        encoded_sig, payload_b64 = signed_request.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="signed_request malformado")

    # Decode signature
    padding = "=" * (4 - len(encoded_sig) % 4)
    sig = base64.urlsafe_b64decode(encoded_sig + padding)

    # Verify HMAC-SHA256
    expected = hmac.new(
        app_secret.encode("utf-8"),
        payload_b64.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=400, detail="Firma inválida en signed_request")

    # Decode payload
    padding = "=" * (4 - len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    return payload


@router.post("/data-deletion")
async def data_deletion_callback(request: Request, signed_request: str = Form(...)):
    """
    Meta Data Deletion Callback — required by Meta Platform Policy.

    Meta POSTs here (application/x-www-form-urlencoded) with a signed_request
    when a user removes AgenteFlow from their Facebook account settings.

    We delete all rows in meta_accounts that have a matching Meta user ID,
    then return the required confirmation JSON.

    Configure this URL in Meta App Dashboard →
    Settings → Basic → Data Deletion Instructions URL:
      https://agenteflow.online/v1/meta/data-deletion
    (or proxy through Next.js — see below)
    """
    payload = _parse_signed_request(signed_request, settings.meta_app_secret)
    meta_user_id: str = payload.get("user_id", "")

    if meta_user_id:
        db = get_supabase()
        # meta_user_id is Meta's internal user ID, stored in the token payload.
        # We store it in meta_accounts.meta_user_id for this lookup.
        db.table("meta_accounts").delete().eq("meta_user_id", meta_user_id).execute()

    # Meta requires this exact response shape
    confirmation_code = f"deletion_{meta_user_id}_{int(datetime.now(timezone.utc).timestamp())}"
    return {
        "url": f"https://agenteflow.online/deletion-status?id={confirmation_code}",
        "confirmation_code": confirmation_code,
    }
