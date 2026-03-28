"""
Background scheduler jobs.
- Token refresh: renews Meta long-lived tokens before they expire (runs every 12h).
"""
import logging
from datetime import datetime, timezone, timedelta

import httpx
from apscheduler.schedulers.background import BackgroundScheduler

from app.supabase.client import get_supabase

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

GRAPH_BASE = "https://graph.facebook.com/v21.0"


def refresh_expiring_meta_tokens() -> None:
    """
    Find meta_accounts tokens expiring within 10 days and exchange them
    for a new long-lived token (60 days).
    """
    from app.config import settings  # late import to avoid circular

    cutoff = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()

    db = get_supabase()
    result = (
        db.table("meta_accounts")
        .select("id,access_token,user_id")
        .eq("status", "active")
        .lt("token_expires_at", cutoff)
        .execute()
    )
    rows = result.data or []

    if not rows:
        logger.info("Token refresh: no tokens expiring soon.")
        return

    logger.info("Token refresh: %d token(s) to renew.", len(rows))

    for row in rows:
        try:
            resp = httpx.get(
                f"{GRAPH_BASE}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.meta_app_id,
                    "client_secret": settings.meta_app_secret,
                    "fb_exchange_token": row["access_token"],
                },
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Token refresh failed for account %s: %s",
                    row["id"],
                    resp.json().get("error", {}).get("message", "unknown"),
                )
                continue

            data = resp.json()
            new_token: str = data["access_token"]
            expires_in: int = data.get("expires_in", 5184000)
            new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            db.table("meta_accounts").update(
                {
                    "access_token": new_token,
                    "token_expires_at": new_expires_at.isoformat(),
                }
            ).eq("id", row["id"]).execute()

            logger.info("Token refresh: account %s renewed until %s.", row["id"], new_expires_at.date())

        except Exception as exc:
            logger.error("Token refresh error for account %s: %s", row["id"], exc)


def evaluate_alert_rules() -> None:
    """
    Check every active alert rule against the latest Meta Ads metrics.
    Fires alert_events when a rule condition is met.
    """
    import json, base64
    from collections import defaultdict

    db = get_supabase()

    # Get all active rules
    rules = (
        db.table("alert_rules")
        .select("*")
        .eq("status", "active")
        .execute()
        .data
        or []
    )

    if not rules:
        return

    # Group rules by user to minimize Meta API calls
    rules_by_user: dict[str, list[dict]] = defaultdict(list)
    for rule in rules:
        rules_by_user[rule["user_id"]].append(rule)

    today = datetime.now(timezone.utc).date().isoformat()

    for user_id, user_rules in rules_by_user.items():
        # Get user's connected accounts
        accounts = (
            db.table("meta_accounts")
            .select("meta_ad_account_id,access_token,name")
            .eq("user_id", user_id)
            .eq("status", "active")
            .execute()
            .data
            or []
        )
        if not accounts:
            continue

        # Fetch today's aggregate metrics for all accounts
        totals: dict[str, float] = {
            "spend": 0, "impressions": 0, "clicks": 0, "roas": 0,
        }
        roas_samples = 0

        for account in accounts:
            resp = httpx.get(
                "https://graph.facebook.com/v21.0/" + account["meta_ad_account_id"] + "/insights",
                params={
                    "access_token": account["access_token"],
                    "fields": "spend,impressions,clicks,ctr,cpc,actions,action_values",
                    "time_range": json.dumps({"since": today, "until": today}),
                    "time_increment": 0,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                continue

            for row in resp.json().get("data", []):
                spend = float(row.get("spend", 0))
                impressions = int(row.get("impressions", 0))
                clicks = int(row.get("clicks", 0))
                actions = row.get("action_values") or []
                revenue = sum(
                    float(a["value"])
                    for a in actions
                    if a.get("action_type") in ("offsite_conversion.fb_pixel_purchase", "purchase")
                )
                totals["spend"] += spend
                totals["impressions"] += impressions
                totals["clicks"] += clicks
                if spend > 0 and revenue > 0:
                    totals["roas"] += revenue / spend
                    roas_samples += 1

        if roas_samples:
            totals["roas"] = totals["roas"] / roas_samples

        totals["ctr"] = totals["clicks"] / (totals["impressions"] or 1)
        totals["cpc"] = totals["spend"] / (totals["clicks"] or 1)

        # Evaluate each rule
        for rule in user_rules:
            metric_value = totals.get(rule["metric"], 0)
            threshold = rule["threshold"]
            op = rule["operator"]

            triggered = (
                (op == "gt"  and metric_value >  threshold) or
                (op == "gte" and metric_value >= threshold) or
                (op == "lt"  and metric_value <  threshold) or
                (op == "lte" and metric_value <= threshold)
            )

            if not triggered:
                continue

            # Determine severity
            if op in ("gt", "gte"):
                excess = (metric_value - threshold) / (threshold or 1)
            else:
                excess = (threshold - metric_value) / (threshold or 1)
            severity = "critical" if excess > 0.5 else "warning"

            # Create event
            db.table("alert_events").insert({
                "rule_id": rule["id"],
                "user_id": user_id,
                "metric": rule["metric"],
                "value": round(metric_value, 4),
                "threshold": threshold,
                "operator": op,
                "severity": severity,
                "campaign_name": rule.get("campaign_id") or "Todas las cuentas",
            }).execute()

            # Update rule counters
            db.table("alert_rules").update({
                "trigger_count": rule["trigger_count"] + 1,
                "last_triggered_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", rule["id"]).execute()

            logger.info(
                "Alert fired: rule=%s user=%s metric=%s value=%.4f threshold=%.4f severity=%s",
                rule["id"], user_id, rule["metric"], metric_value, threshold, severity,
            )


def start_scheduler() -> None:
    if scheduler.running:
        return

    scheduler.add_job(
        refresh_expiring_meta_tokens,
        trigger="interval",
        hours=12,
        id="meta_token_refresh",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
    )

    scheduler.add_job(
        evaluate_alert_rules,
        trigger="interval",
        hours=1,
        id="alert_evaluation",
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=10),
    )

    scheduler.start()
    logger.info("Scheduler started. Token refresh every 12h, alert evaluation every 1h.")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown()
