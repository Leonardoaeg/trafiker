"""
Metrics router — real data from Meta Ads Insights API.
Endpoints:
  GET /v1/metrics/overview   — aggregated totals for the user's accounts
  GET /v1/metrics/daily      — day-by-day breakdown
  GET /v1/metrics/timeseries — per-campaign daily breakdown
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


# ── Auth helper ────────────────────────────────────────────────────────────────

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


# ── Meta helpers ───────────────────────────────────────────────────────────────

def _action_value(actions: list[dict], *types: str) -> float:
    """Sum values from the actions array for the given action_type(s)."""
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
    """Call Meta Insights API and return raw day rows."""
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

    resp = httpx.get(
        f"{GRAPH_BASE}/{ad_account_id}/insights",
        params=params,
        timeout=20,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("data", [])


def _parse_row(row: dict) -> dict:
    """Convert one Meta Insights row into a clean dict."""
    actions = row.get("actions") or []
    action_values = row.get("action_values") or []

    conversions = _action_value(
        actions,
        "offsite_conversion.fb_pixel_purchase",
        "purchase",
        "complete_registration",
        "lead",
    )
    initiate_checkout = _action_value(actions, "initiate_checkout")
    revenue = _action_value(
        action_values,
        "offsite_conversion.fb_pixel_purchase",
        "purchase",
    )

    spend = float(row.get("spend", 0))
    impressions = int(row.get("impressions", 0))
    clicks = int(row.get("clicks", 0))
    reach = int(row.get("reach", 0))
    ctr = float(row.get("ctr", 0)) / 100  # Meta returns percentage
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
    """Resolve query params to ISO date strings."""
    end = date.today()
    start = end - timedelta(days=default_days - 1)
    if to_date:
        end = date.fromisoformat(to_date)
    if from_date:
        start = date.fromisoformat(from_date)
    return start.isoformat(), end.isoformat()


def _get_user_accounts(user_id: str) -> list[dict]:
    db = get_supabase()
    return (
        db.table("meta_accounts")
        .select("id,meta_ad_account_id,access_token")
        .eq("user_id", user_id)
        .eq("status", "active")
        .execute()
        .data
        or []
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/overview")
def metrics_overview(
    from_date: str | None = Query(default=None),
    to_date: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """Aggregated totals across all connected Meta accounts."""
    user_id = _extract_user_id(authorization)
    since, until = _date_range(from_date, to_date, default_days=30)
    accounts = _get_user_accounts(user_id)

    totals: dict = {
        "total_spend": 0.0,
        "total_impressions": 0,
        "total_clicks": 0,
        "total_conversions": 0,
        "total_revenue": 0.0,
        "avg_ctr": None,
        "avg_cpc": None,
        "avg_roas": None,
        "active_campaigns": 0,
    }

    if not accounts:
        return totals

    all_rows: list[dict] = []
    for account in accounts:
        rows = _fetch_insights(
            account["access_token"],
            account["meta_ad_account_id"],
            since,
            until,
            time_increment=0,   # aggregate (no daily breakdown)
        )
        all_rows.extend([_parse_row(r) for r in rows])

        # Active campaign count
        camp_resp = httpx.get(
            f"{GRAPH_BASE}/{account['meta_ad_account_id']}/campaigns",
            params={
                "access_token": account["access_token"],
                "effective_status": '["ACTIVE"]',
                "fields": "id",
                "limit": 200,
            },
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
    authorization: str | None = Header(default=None),
):
    """Day-by-day metrics across all connected accounts, merged by date."""
    user_id = _extract_user_id(authorization)
    since, until = _date_range(from_date, to_date, default_days=30)
    accounts = _get_user_accounts(user_id)

    if not accounts:
        return []

    # Merge rows from all accounts by date
    by_date: dict[str, dict] = defaultdict(lambda: {
        "date": "",
        "spend": 0.0,
        "impressions": 0,
        "clicks": 0,
        "reach": 0,
        "conversions": 0,
        "initiate_checkout": 0,
        "revenue": 0.0,
    })

    for account in accounts:
        rows = _fetch_insights(
            account["access_token"],
            account["meta_ad_account_id"],
            since,
            until,
            time_increment=1,
        )
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

    # Add computed rates per day
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
    """Per-campaign daily breakdown."""
    user_id = _extract_user_id(authorization)
    since, until = _date_range(from_date, to_date, default_days=30)
    accounts = _get_user_accounts(user_id)

    if not accounts:
        return []

    for account in accounts:
        rows = _fetch_insights(
            account["access_token"],
            account["meta_ad_account_id"],
            since,
            until,
            time_increment=1,
            campaign_id=campaign_id,
        )
        if rows:
            return [
                {**_parse_row(r), "campaign_id": campaign_id}
                for r in sorted(rows, key=lambda x: x.get("date_start", ""))
            ]

    return []
