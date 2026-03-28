"""
Alerts router — rule management + event history + AI analysis.

Endpoints (proxied from Next.js via /api/trafiker/alerts):
  GET    /alerts/{user_id}              → { rules, events }
  POST   /alerts/                       → create rule
  PATCH  /alerts/{rule_id}              → toggle status
  DELETE /alerts/{rule_id}              → delete rule
  POST   /alerts/events/{id}/analyze    → AI analysis of an alert event
"""
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.supabase.client import get_supabase

router = APIRouter()

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


# ── Schemas ────────────────────────────────────────────────────────────────────

class CreateAlertBody(BaseModel):
    tenant_id: str        # = user_id (sent by Next.js proxy)
    name: str
    metric: str           # spend | ctr | cpc | roas | impressions
    operator: str         # gt | lt | gte | lte
    threshold: float
    campaign_id: str | None = None


class ToggleAlertBody(BaseModel):
    status: str           # active | paused


# ── Helpers ────────────────────────────────────────────────────────────────────

def _metric_label(metric: str, value: float) -> str:
    if metric == "spend":       return f"${value:.2f}"
    if metric == "ctr":         return f"{value:.2%}"
    if metric == "cpc":         return f"${value:.2f}"
    if metric == "roas":        return f"{value:.2f}x"
    if metric == "impressions": return f"{int(value):,}"
    return str(value)


def _severity(metric: str, operator: str, value: float, threshold: float) -> str:
    """Classify severity based on how far the value exceeds the threshold."""
    if operator in ("gt", "gte"):
        excess = (value - threshold) / (threshold or 1)
    else:
        excess = (threshold - value) / (threshold or 1)

    if metric in ("spend", "cpc", "cpa") and operator in ("gt", "gte"):
        return "critical" if excess > 0.5 else "warning"
    if metric == "roas" and operator in ("lt", "lte"):
        return "critical" if excess > 0.5 else "warning"
    return "critical" if excess > 1 else "warning"


def _gemini_analyze(prompt: str) -> str:
    """Call Gemini for a brief marketing analysis."""
    payload = {
        "system_instruction": {
            "parts": [{"text": (
                "Eres Trafiker, un experto en marketing digital y Meta Ads. "
                "Analiza alertas de campañas publicitarias de forma concisa (2-4 oraciones). "
                "Siempre da 1-2 acciones concretas. Sin markdown, texto plano."
            )}]
        },
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 300},
    }
    resp = requests.post(
        GEMINI_API_URL,
        params={"key": settings.gemini_api_key},
        json=payload,
        timeout=30,
    )
    if resp.status_code != 200:
        return "No se pudo completar el análisis en este momento."
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"]


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/{user_id}")
def list_alerts(user_id: str):
    """Return all rules and recent events for a user."""
    db = get_supabase()

    rules = (
        db.table("alert_rules")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )

    events = (
        db.table("alert_events")
        .select("*")
        .eq("user_id", user_id)
        .order("fired_at", desc=True)
        .limit(50)
        .execute()
        .data
        or []
    )

    return {"rules": rules, "events": events}


@router.post("/")
def create_alert(body: CreateAlertBody):
    """Create a new alert rule."""
    db = get_supabase()
    result = (
        db.table("alert_rules")
        .insert({
            "user_id": body.tenant_id,
            "name": body.name,
            "metric": body.metric,
            "operator": body.operator,
            "threshold": body.threshold,
            "campaign_id": body.campaign_id,
            "status": "active",
        })
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=500, detail="Error creando la alerta")
    return result.data[0]


@router.patch("/{rule_id}")
def toggle_alert(rule_id: str, body: ToggleAlertBody):
    """Activate or pause an alert rule."""
    if body.status not in ("active", "paused"):
        raise HTTPException(status_code=400, detail="Estado inválido")
    db = get_supabase()
    result = (
        db.table("alert_rules")
        .update({"status": body.status})
        .eq("id", rule_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Regla no encontrada")
    return result.data[0]


@router.delete("/{rule_id}")
def delete_alert(rule_id: str):
    """Delete an alert rule and its events."""
    db = get_supabase()
    db.table("alert_rules").delete().eq("id", rule_id).execute()
    return {"deleted": rule_id}


@router.post("/events/{event_id}/analyze")
def analyze_event(event_id: str):
    """Use Gemini to analyze why an alert fired and suggest actions."""
    db = get_supabase()

    event_result = (
        db.table("alert_events")
        .select("*, alert_rules(name, metric, operator, threshold)")
        .eq("id", event_id)
        .single()
        .execute()
    )
    if not event_result.data:
        raise HTTPException(status_code=404, detail="Evento no encontrado")

    event = event_result.data
    rule = event.get("alert_rules") or {}

    prompt = (
        f"Alerta disparada para la campaña '{event['campaign_name']}'.\n"
        f"Regla: {rule.get('name', 'Sin nombre')}\n"
        f"Métrica: {event['metric'].upper()} = {_metric_label(event['metric'], event['value'])} "
        f"(umbral: {_metric_label(event['metric'], event['threshold'])})\n"
        f"Severidad: {event['severity']}\n\n"
        f"¿Por qué puede estar pasando esto y qué acciones concretas recomiendas tomar?"
    )

    analysis = _gemini_analyze(prompt)

    # Save analysis back to the event
    db.table("alert_events").update({"ai_analysis": analysis}).eq("id", event_id).execute()

    return {"analysis": analysis}
