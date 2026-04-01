"""
Campaigns router — real data from Meta Ads API.
Uses the per-user access tokens stored in Supabase.
"""
import base64
import json

import httpx
from fastapi import APIRouter, HTTPException, Header, Query
from pydantic import BaseModel

from app.config import settings
from app.supabase.client import get_supabase

router = APIRouter()

GRAPH_BASE = "https://graph.facebook.com/v21.0"

CAMPAIGN_FIELDS = "id,name,status,objective,daily_budget,lifetime_budget,start_time,stop_time"


# ── Auth helper (same pattern as meta router) ──────────────────────────────────

def _extract_user_id(authorization: str | None) -> str:
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


def _get_user_accounts(user_id: str, account_id: str | None = None) -> list[dict]:
    """Return meta_accounts rows for the current user from Supabase."""
    db = get_supabase()
    query = (
        db.table("meta_accounts")
        .select("id,meta_ad_account_id,access_token,name")
        .eq("user_id", user_id)
        .eq("status", "active")
    )
    if account_id:
        query = query.eq("id", account_id)
    return query.execute().data or []


def _fetch_campaigns_for_account(account: dict, status_filter: str | None) -> list[dict]:
    """Call Meta API and return campaigns for one ad account."""
    params: dict = {
        "access_token": account["access_token"],
        "fields": CAMPAIGN_FIELDS,
        "limit": 200,
    }
    if status_filter:
        params["effective_status"] = f'["{status_filter}"]'
    else:
        params["effective_status"] = '["ACTIVE","PAUSED","ARCHIVED"]'

    resp = httpx.get(
        f"{GRAPH_BASE}/{account['meta_ad_account_id']}/campaigns",
        params=params,
        timeout=15,
    )
    if resp.status_code != 200:
        return []

    campaigns = []
    for c in resp.json().get("data", []):
        campaigns.append({
            "id": c["id"],
            "meta_account_id": account["id"],          # Supabase UUID
            "meta_campaign_id": c["id"],
            "name": c.get("name", ""),
            "status": c.get("status", "PAUSED"),
            "objective": c.get("objective"),
            "daily_budget": int(c["daily_budget"]) / 100 if c.get("daily_budget") else None,
            "lifetime_budget": int(c["lifetime_budget"]) / 100 if c.get("lifetime_budget") else None,
            "start_time": c.get("start_time"),
            "stop_time": c.get("stop_time"),
        })
    return campaigns


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_campaigns(
    account_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """
    Return campaigns for the authenticated user.
    Optionally filter by account_id (Supabase UUID) and/or status (ACTIVE | PAUSED).
    """
    user_id = _extract_user_id(authorization)
    accounts = _get_user_accounts(user_id, account_id)

    if not accounts:
        return []

    all_campaigns: list[dict] = []
    for account in accounts:
        all_campaigns.extend(_fetch_campaigns_for_account(account, status_filter))

    return all_campaigns


class CreateCampaign(BaseModel):
    name: str
    objective: str        # CONVERSIONS | REACH | LEAD_GENERATION | BRAND_AWARENESS | TRAFFIC
    daily_budget: float   # en USD
    status: str = "PAUSED"  # ACTIVE | PAUSED
    account_id: str | None = None  # Supabase UUID de meta_accounts (opcional)


class StatusUpdate(BaseModel):
    status: str  # ACTIVE | PAUSED


@router.post("/")
def create_campaign(
    body: CreateCampaign,
    authorization: str | None = Header(default=None),
):
    """
    Create a new campaign in Meta Ads via the API.
    Uses the first active account of the user, or the specified account_id.
    """
    user_id = _extract_user_id(authorization)
    accounts = _get_user_accounts(user_id, body.account_id)

    if not accounts:
        raise HTTPException(status_code=404, detail="No hay cuentas de Meta conectadas")

    account = accounts[0]
    daily_budget_cents = int(body.daily_budget * 100)

    resp = httpx.post(
        f"{GRAPH_BASE}/{account['meta_ad_account_id']}/campaigns",
        params={"access_token": account["access_token"]},
        json={
            "name": body.name,
            "objective": body.objective,
            "status": body.status,
            "special_ad_categories": [],
            "daily_budget": daily_budget_cents,
        },
        timeout=15,
    )

    if resp.status_code not in (200, 201):
        detail = resp.json().get("error", {}).get("message", "Error al crear la campaña en Meta")
        raise HTTPException(status_code=400, detail=detail)

    campaign_id = resp.json().get("id")

    # Fetch created campaign details
    detail_resp = httpx.get(
        f"{GRAPH_BASE}/{campaign_id}",
        params={"access_token": account["access_token"], "fields": CAMPAIGN_FIELDS},
        timeout=10,
    )
    c = detail_resp.json() if detail_resp.status_code == 200 else {}

    return {
        "id": campaign_id,
        "meta_account_id": account["id"],
        "meta_campaign_id": campaign_id,
        "name": c.get("name", body.name),
        "status": c.get("status", body.status),
        "objective": c.get("objective", body.objective),
        "daily_budget": int(c["daily_budget"]) / 100 if c.get("daily_budget") else body.daily_budget,
        "lifetime_budget": None,
        "start_time": c.get("start_time"),
        "stop_time": c.get("stop_time"),
    }


@router.patch("/{campaign_id}/status")
def toggle_campaign_status(
    campaign_id: str,
    body: StatusUpdate,
    authorization: str | None = Header(default=None),
):
    """
    Activate or pause a campaign.
    Tries each of the user's connected accounts until one succeeds.
    """
    if body.status not in ("ACTIVE", "PAUSED"):
        raise HTTPException(status_code=400, detail="Estado debe ser ACTIVE o PAUSED")

    user_id = _extract_user_id(authorization)
    accounts = _get_user_accounts(user_id)

    if not accounts:
        raise HTTPException(status_code=404, detail="No hay cuentas de Meta conectadas")

    last_error = "No se pudo actualizar la campaña"
    for account in accounts:
        resp = httpx.post(
            f"{GRAPH_BASE}/{campaign_id}",
            params={"access_token": account["access_token"]},
            json={"status": body.status},
            timeout=10,
        )
        if resp.status_code == 200:
            # Return updated campaign data
            detail_resp = httpx.get(
                f"{GRAPH_BASE}/{campaign_id}",
                params={
                    "access_token": account["access_token"],
                    "fields": CAMPAIGN_FIELDS,
                },
                timeout=10,
            )
            c = detail_resp.json() if detail_resp.status_code == 200 else {}
            return {
                "id": campaign_id,
                "meta_account_id": account["id"],
                "meta_campaign_id": campaign_id,
                "name": c.get("name", ""),
                "status": c.get("status", body.status),
                "objective": c.get("objective"),
                "daily_budget": int(c["daily_budget"]) / 100 if c.get("daily_budget") else None,
                "lifetime_budget": int(c["lifetime_budget"]) / 100 if c.get("lifetime_budget") else None,
                "start_time": c.get("start_time"),
                "stop_time": c.get("stop_time"),
            }
        last_error = resp.json().get("error", {}).get("message", last_error)

    raise HTTPException(status_code=400, detail=last_error)
