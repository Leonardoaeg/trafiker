"""
Metrics router — real data from Meta Ads Insights API.
Endpoints:
  GET  /v1/metrics/overview                        — aggregated totals for the user's accounts
  GET  /v1/metrics/daily                           — day-by-day breakdown
  GET  /v1/metrics/timeseries                      — per-campaign daily breakdown
  POST /v1/metrics/sync-portfolio/{portfolio_id}   — sync campaigns + metrics for a portfolio
"""
import base64
import json
from collections import defaultdict
from datetime import date, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Header, Query

from app.supabase.client import get_supabase

router = APIRouter()

GRAPH_BASE = "https://graph.facebook.com/v21.0"

INSIGHT_FIELDS = (
    "spend,impressions,clicks,reach,ctr,cpc,cpm,"
    "actions,action_values"
)

CAMPAIGN_FIELDS = "id,name,status,objective,daily_budget,lifetime_budget,start_time,stop_time"


# ── Auth helpers ───────────────────────────────────────────────────────────────

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


def _get_tenant_id(user_id: str) -> str:
    db = get_supabase()
    result = (
        db.table("tenant_members")
        .select("tenant_id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=403, detail="Usuario sin tenant asignado")
    return result.data[0]["tenant_id"]


def _get_user_accounts(tenant_id: str, meta_account_ids: list[str] | None = None) -> list[dict]:
    """Return meta_accounts rows for the tenant. Optionally filter by Supabase UUIDs."""
    db = get_supabase()
    query = (
        db.table("meta_accounts")
        .select("id,meta_ad_account_id,access_token_encrypted")
        .eq("tenant_id", tenant_id)
        .eq("status", "active")
    )
    if meta_account_ids:
        query = query.in_("id", meta_account_ids)
    rows = query.execute().data or []
    # Normalize token field name
    for row in rows:
        row["access_token"] = row.pop("access_token_encrypted", "")
    return rows


def _get_portfolio_account_ids(portfolio_id: str) -> list[str]:
    """Return meta_account UUIDs linked to a portfolio."""
    db = get_supabase()
    result = (
        db.table("portfolio_accounts")
        .select("meta_account_id")
        .eq("portfolio_id", portfolio_id)
        .execute()
    )
    return [r["meta_account_id"] for r in (result.data or [])]


# ── Meta helpers ───────────────────────────────────────────────────────────────

def _action_value(actions: list[dict], *types: str) -> float:
    total = 0.0
    for a in actions:
        if a.get("action_type") in types:
            total += float(a.get("value", 0))
    return total


def _fetch_insights(
    access_token: str,
    ad_account_id: str,
    since: str,
    until: str,
    time_increment: int = 1,
    campaign_id: str | None = None,
) -> list[dict]:
    params: dict = {
        "access_token": access_token,
        "fields": INSIGHT_FIELDS,
        "time_range": json.dumps({"since": since, "until": until}),
        "time_increment": time_increment,
        "limit": 500,
    }
    if campaign_id:
        params["filtering"] = json.dumps(
            [{"field": "campaign.id", "operator": "EQUAL", "value": campaign_id}]
        )
    resp = httpx.get(f"{GRAPH_BASE}/{ad_account_id}/insights", params=params, timeout=20)
    if resp.status_code != 200:
        return []
    return resp.json().get("data", [])


def _parse_row(row: dict) -> dict:
    actions = row.get("actions") or []
    action_values = row.get("action_values") or []
    conversions = _action_value(
        actions, "offsite_conversion.fb_pixel_purchase", "purchase", "complete_registration", "lead"
    )
    initiate_checkout = _action_value(actions, "initiate_checkout")
    revenue = _action_value(action_values, "offsite_conversion.fb_pixel_purchase", "purchase")
    spend = float(row.get("spend", 0))
    impressions = int(row.get("impressions", 0))
    clicks = int(row.get("clicks", 0))
    reach = int(row.get("reach", 0))
    ctr = float(row.get("ctr", 0)) / 100
    cpc = float(row.get("cpc", 0))
    cpm = float(row.get("cpm", 0))
    roas = (revenue / spend) if spend > 0 and revenue > 0 else None
    return {
        "date": row.get("date_start", ""),
        "spend": round(spend, 2),
        "impressions": impressions,
        "clicks": clicks,
        "reach": reach,
        "conversions": int(conversions),
        "initiate_checkout": int(initiate_checkout),
        "revenue": round(revenue, 2),
        "ctr": round(ctr, 6),
        "cpc": round(cpc, 4),
        "cpm": round(cpm, 4),
        "roas": round(roas, 4) if roas is not None else None,
    }


def _date_range(from_date: str | None, to_date: str | None, default_days: int = 30):
    end = date.today()
    start = end - timedelta(days=default_days - 1)
    if to_date:
        end = date.fromisoformat(to_date)
    if from_date:
        start = date.fromisoformat(from_date)
    return start.isoformat(), end.isoformat()


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/overview")
def metrics_overview(
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    portfolio_id: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    user_id = _extract_user_id(authorization)
    tenant_id = _get_tenant_id(user_id)
    since, until = _date_range(from_date, to_date, default_days=30)

    account_ids = _get_portfolio_account_ids(portfolio_id) if portfolio_id else None
    accounts = _get_user_accounts(tenant_id, account_ids)

    totals: dict = {
        "total_spend": 0.0, "total_impressions": 0, "total_clicks": 0,
        "total_conversions": 0, "total_revenue": 0.0,
        "avg_ctr": None, "avg_cpc": None, "avg_roas": None, "active_campaigns": 0,
    }
    if not accounts:
        return totals

    all_rows: list[dict] = []
    for account in accounts:
        rows = _fetch_insights(account["access_token"], account["meta_ad_account_id"], since, until, time_increment=0)
        all_rows.extend([_parse_row(r) for r in rows])
        camp_resp = httpx.get(
            f"{GRAPH_BASE}/{account['meta_ad_account_id']}/campaigns",
            params={"access_token": account["access_token"], "effective_status": '["ACTIVE"]', "fields": "id", "limit": 200},
            timeout=10,
        )
        if camp_resp.status_code == 200:
            totals["active_campaigns"] += len(camp_resp.json().get("data", []))

    for r in all_rows:
        totals["total_spend"] += r["spend"]
        totals["total_impressions"] += r["impressions"]
        totals["total_clicks"] += r["clicks"]
        totals["total_conversions"] += r["conversions"]
        totals["total_revenue"] += r["revenue"]

    totals["total_spend"] = round(totals["total_spend"], 2)
    totals["total_revenue"] = round(totals["total_revenue"], 2)
    clicks = totals["total_clicks"]
    spend = totals["total_spend"]
    impressions = totals["total_impressions"]
    if impressions > 0:
        totals["avg_ctr"] = round(clicks / impressions, 6)
    if clicks > 0:
        totals["avg_cpc"] = round(spend / clicks, 4)
    if spend > 0 and totals["total_revenue"] > 0:
        totals["avg_roas"] = round(totals["total_revenue"] / spend, 4)
    return totals


@router.get("/daily")
def metrics_daily(
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    portfolio_id: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    user_id = _extract_user_id(authorization)
    tenant_id = _get_tenant_id(user_id)
    since, until = _date_range(from_date, to_date, default_days=30)

    account_ids = _get_portfolio_account_ids(portfolio_id) if portfolio_id else None
    accounts = _get_user_accounts(tenant_id, account_ids)

    if not accounts:
        return []

    by_date: dict[str, dict] = defaultdict(lambda: {
        "date": "", "spend": 0.0, "impressions": 0, "clicks": 0,
        "reach": 0, "conversions": 0, "initiate_checkout": 0, "revenue": 0.0,
    })
    for account in accounts:
        rows = _fetch_insights(account["access_token"], account["meta_ad_account_id"], since, until, time_increment=1)
        for row in rows:
            parsed = _parse_row(row)
            d = parsed["date"]
            by_date[d]["date"] = d
            by_date[d]["spend"] = round(by_date[d]["spend"] + parsed["spend"], 2)
            by_date[d]["impressions"] += parsed["impressions"]
            by_date[d]["clicks"] += parsed["clicks"]
            by_date[d]["reach"] += parsed["reach"]
            by_date[d]["conversions"] += parsed["conversions"]
            by_date[d]["initiate_checkout"] += parsed["initiate_checkout"]
            by_date[d]["revenue"] = round(by_date[d]["revenue"] + parsed["revenue"], 2)

    result = []
    for row in sorted(by_date.values(), key=lambda r: r["date"]):
        clicks = row["clicks"]
        spend = row["spend"]
        impressions = row["impressions"]
        row["ctr"] = round(clicks / impressions, 6) if impressions else 0
        row["cpc"] = round(spend / clicks, 4) if clicks else 0
        row["cpm"] = round((spend / impressions) * 1000, 4) if impressions else 0
        row["roas"] = round(row["revenue"] / spend, 4) if spend > 0 and row["revenue"] > 0 else None
        result.append(row)
    return result


@router.get("/timeseries")
def metrics_timeseries(
    campaign_id: str = Query(...),
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    user_id = _extract_user_id(authorization)
    tenant_id = _get_tenant_id(user_id)
    since, until = _date_range(from_date, to_date, default_days=30)
    accounts = _get_user_accounts(tenant_id)

    if not accounts:
        return []

    for account in accounts:
        rows = _fetch_insights(
            account["access_token"], account["meta_ad_account_id"], since, until,
            time_increment=1, campaign_id=campaign_id,
        )
        if rows:
            return [{**_parse_row(r), "campaign_id": campaign_id}
                    for r in sorted(rows, key=lambda x: x.get("date_start", ""))]
    return []


@router.post("/sync-portfolio/{portfolio_id}")
def sync_portfolio_metrics(
    portfolio_id: str,
    from_date: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """
    Sync campaigns and daily metrics for all Meta accounts linked to a portfolio.
    Upserts into `campaigns` and `campaign_metrics` tables.
    """
    import logging
    logger = logging.getLogger(__name__)

    user_id = _extract_user_id(authorization)
    tenant_id = _get_tenant_id(user_id)
    db = get_supabase()

    # Get accounts for this portfolio
    account_ids = _get_portfolio_account_ids(portfolio_id)
    if not account_ids:
        raise HTTPException(status_code=404, detail="Portafolio sin cuentas Meta asociadas")

    accounts = _get_user_accounts(tenant_id, account_ids)
    if not accounts:
        raise HTTPException(status_code=404, detail="No se encontraron cuentas activas")

    # Date range — default last 30 days
    since, until = _date_range(from_date, None, default_days=30)

    synced_campaigns = 0
    synced_metrics = 0

    for account in accounts:
        token = account["access_token"]
        meta_ad_account_id = account["meta_ad_account_id"]
        supabase_account_id = account["id"]

        # ── 1. Fetch campaigns from Meta ──────────────────────────────────────
        try:
            camp_resp = httpx.get(
                f"{GRAPH_BASE}/{meta_ad_account_id}/campaigns",
                params={
                    "access_token": token,
                    "fields": CAMPAIGN_FIELDS,
                    "effective_status": '["ACTIVE","PAUSED","ARCHIVED"]',
                    "limit": 200,
                },
                timeout=15,
            )
        except Exception as exc:
            logger.error("Meta API error fetching campaigns for account %s: %s", meta_ad_account_id, exc)
            continue

        if camp_resp.status_code != 200:
            logger.warning(
                "Meta API returned %s for account %s: %s",
                camp_resp.status_code, meta_ad_account_id,
                camp_resp.json().get("error", {}).get("message", ""),
            )
            continue

        campaigns = camp_resp.json().get("data", [])

        for c in campaigns:
            meta_campaign_id = c["id"]

            # Upsert campaign record
            try:
                db.table("campaigns").upsert(
                    {
                        "tenant_id": tenant_id,
                        "meta_account_id": supabase_account_id,
                        "meta_campaign_id": meta_campaign_id,
                        "name": c.get("name", ""),
                        "status": c.get("status", "PAUSED"),
                        "objective": c.get("objective"),
                        "daily_budget": int(c["daily_budget"]) / 100 if c.get("daily_budget") else None,
                        "lifetime_budget": int(c["lifetime_budget"]) / 100 if c.get("lifetime_budget") else None,
                        "start_time": c.get("start_time"),
                        "stop_time": c.get("stop_time"),
                        "last_synced_at": date.today().isoformat(),
                    },
                    on_conflict="tenant_id,meta_campaign_id",
                ).execute()
            except Exception as exc:
                logger.error("Supabase upsert error for campaign %s: %s", meta_campaign_id, exc)
                raise HTTPException(
                    status_code=500,
                    detail=f"Error al guardar campaña en base de datos: {exc}",
                )
            synced_campaigns += 1

            # Get the Supabase campaign UUID
            try:
                camp_row = (
                    db.table("campaigns")
                    .select("id")
                    .eq("tenant_id", tenant_id)
                    .eq("meta_campaign_id", meta_campaign_id)
                    .limit(1)
                    .execute()
                )
            except Exception as exc:
                logger.error("Supabase select error for campaign %s: %s", meta_campaign_id, exc)
                continue

            if not camp_row.data:
                continue
            campaign_uuid = camp_row.data[0]["id"]

            # ── 2. Fetch daily insights for this campaign ─────────────────────
            try:
                insight_rows = _fetch_insights(
                    token, meta_ad_account_id, since, until,
                    time_increment=1, campaign_id=meta_campaign_id,
                )
            except Exception as exc:
                logger.error("Meta insights error for campaign %s: %s", meta_campaign_id, exc)
                continue

            for row in insight_rows:
                parsed = _parse_row(row)
                if not parsed["date"]:
                    continue

                try:
                    db.table("campaign_metrics").upsert(
                        {
                            "tenant_id": tenant_id,
                            "campaign_id": campaign_uuid,
                            "date": parsed["date"],
                            "impressions": parsed["impressions"],
                            "clicks": parsed["clicks"],
                            "spend": parsed["spend"],
                            "reach": parsed["reach"],
                            "conversions": parsed["conversions"],
                            "ctr": parsed["ctr"],
                            "cpc": parsed["cpc"],
                            "cpm": parsed["cpm"],
                            "roas": parsed["roas"],
                        },
                        on_conflict="tenant_id,campaign_id,date",
                    ).execute()
                except Exception as exc:
                    logger.error("Supabase upsert error for metric %s/%s: %s", campaign_uuid, parsed["date"], exc)
                    raise HTTPException(
                        status_code=500,
                        detail=f"Error al guardar métricas en base de datos: {exc}",
                    )
                synced_metrics += 1

    return {
        "portfolio_id": portfolio_id,
        "synced_campaigns": synced_campaigns,
        "synced_metrics": synced_metrics,
        "date_range": {"since": since, "until": until},
    }
