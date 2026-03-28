from fastapi import APIRouter, HTTPException
from app.models.schemas import ChatRequest, ChatResponse
from app.agent import core
from app.supabase.client import get_supabase

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    """
    Envía un mensaje al agente Trafiker.
    Si no se pasa conversation_id, crea una nueva conversación automáticamente.
    """
    try:
        result = core.chat(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            message=request.message,
            conversation_id=request.conversation_id,
            campaign_context=request.campaign_context,
        )
        return ChatResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/conversations/{tenant_id}")
def list_conversations(tenant_id: str):
    """Lista todas las conversaciones de un tenant."""
    try:
        db = get_supabase()
        result = (
            db.table("ai_conversations")
            .select("id, title, created_at")
            .eq("tenant_id", tenant_id)
            .eq("context_type", "trafiker")
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        return {"conversations": result.data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/conversations/{tenant_id}/{conversation_id}/messages")
def get_messages(tenant_id: str, conversation_id: str):
    """Obtiene los mensajes de una conversación."""
    try:
        history = core.get_conversation_history(conversation_id)
        return {"messages": history}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
def agent_status():
    return {"status": "Trafiker activo", "model": "gemini-2.5-flash"}
