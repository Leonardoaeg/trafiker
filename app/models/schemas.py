from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# --- Agent ---

class ChatRequest(BaseModel):
    tenant_id: str
    user_id: str
    message: str
    conversation_id: Optional[str] = None
    campaign_context: Optional[dict] = None


class ChatResponse(BaseModel):
    conversation_id: str
    message: str
    tokens_used: int


# --- Campaigns ---

class CampaignAction(BaseModel):
    tenant_id: str
    campaign_id: str
    action: str  # pause | activate | budget_change
    new_budget: Optional[float] = None
    notes: Optional[str] = None


# --- Alerts ---

class AlertRuleCreate(BaseModel):
    tenant_id: str
    meta_account_id: Optional[str] = None
    campaign_id: Optional[str] = None
    name: str
    metric: str        # ctr | cpc | cpm | roas | conversions | spend
    operator: str      # gt | lt | gte | lte
    threshold: float
    window_hours: int = 24
    notify_email: bool = True
    notify_in_app: bool = True


# --- Training ---

class TrainingCreate(BaseModel):
    tenant_id: str
    category: str      # brand_voice | strategy | kpis | audience | restrictions | custom
    title: str
    instruction: str
    created_by: Optional[str] = None


class TrainingUpdate(BaseModel):
    title: Optional[str] = None
    instruction: Optional[str] = None
    is_active: Optional[bool] = None


# --- Creatives ---

class CreativeCreate(BaseModel):
    tenant_id: str
    campaign_id: Optional[str] = None
    name: str
    type: str          # image | video | copy | carousel
    url: Optional[str] = None
    copy_text: Optional[str] = None
    headline: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[list[str]] = None
