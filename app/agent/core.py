import requests
from app.config import settings
from app.supabase.client import get_supabase
from app.agent.prompts import build_system_prompt

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-2.0-flash"


def _call_gemini(system_prompt: str, history: list[dict], message: str, max_tokens: int = 4096) -> tuple[str, int]:
    """Llama a la API de Gemini directamente via REST."""
    contents = []
    for msg in history:
        role = "model" if msg["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    contents.append({"role": "user", "parts": [{"text": message}]})

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens},
    }

    url = GEMINI_API_URL.format(model=GEMINI_MODEL)
    resp = requests.post(url, params={"key": settings.gemini_api_key}, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    text = data["candidates"][0]["content"]["parts"][0]["text"]
    tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
    return text, tokens


def get_training_context(tenant_id: str) -> str:
    """Carga las instrucciones personalizadas del tenant desde agent_training."""
    try:
        db = get_supabase()
        result = (
            db.table("agent_training")
            .select("category, title, instruction")
            .eq("tenant_id", tenant_id)
            .eq("is_active", True)
            .execute()
        )
        if not result.data:
            return ""
        lines = []
        for item in result.data:
            lines.append(f"[{item['category'].upper()}] {item['title']}: {item['instruction']}")
        return "\n".join(lines)
    except Exception:
        return ""


def get_assistant_config(tenant_id: str) -> dict:
    """Carga la configuración del asistente. Usa config global si no hay una por tenant."""
    try:
        db = get_supabase()
        result = db.table("assistant_config").select("*").limit(1).execute()
        if result.data:
            return result.data[0]
    except Exception:
        pass
    return {"max_tokens": 4096}


def get_conversation_history(conversation_id: str) -> list[dict]:
    """Carga el historial de mensajes de una conversación."""
    try:
        db = get_supabase()
        result = (
            db.table("ai_messages")
            .select("role, content")
            .eq("conversation_id", conversation_id)
            .order("created_at")
            .execute()
        )
        return result.data or []
    except Exception:
        return []


def create_conversation(tenant_id: str, user_id: str, title: str = "Nueva conversación") -> str:
    """Crea una nueva conversación y retorna su ID."""
    db = get_supabase()
    result = (
        db.table("ai_conversations")
        .insert({
            "tenant_id": tenant_id,
            "user_id": user_id,
            "title": title,
            "context_type": "general",
        })
        .execute()
    )
    return result.data[0]["id"]


def save_message(conversation_id: str, role: str, content: str, tokens: int = 0):
    """Guarda un mensaje en la base de datos."""
    db = get_supabase()
    db.table("ai_messages").insert({
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "tokens_used": tokens,
    }).execute()


def chat(
    tenant_id: str,
    user_id: str,
    message: str,
    conversation_id: str | None = None,
    campaign_context: dict | None = None,
) -> dict:
    """Envía un mensaje al agente Trafiker y retorna la respuesta."""
    # 1. Obtener o crear conversación
    if not conversation_id:
        title = message[:60] + "..." if len(message) > 60 else message
        conversation_id = create_conversation(tenant_id, user_id, title)

    # 2. Cargar configuración y contexto
    config = get_assistant_config(tenant_id)
    training_context = get_training_context(tenant_id)

    client_context = ""
    if campaign_context:
        client_context = f"Campaña activa: {campaign_context}"

    system_prompt = build_system_prompt(client_context, training_context)

    # 3. Cargar historial
    history = get_conversation_history(conversation_id)

    # 4. Guardar mensaje del usuario
    save_message(conversation_id, "user", message)

    # 5. Llamar a Gemini
    assistant_message, tokens_used = _call_gemini(
        system_prompt=system_prompt,
        history=history,
        message=message,
        max_tokens=config.get("max_tokens", 4096),
    )

    # 6. Guardar respuesta
    save_message(conversation_id, "assistant", assistant_message, tokens_used)

    return {
        "conversation_id": conversation_id,
        "message": assistant_message,
        "tokens_used": tokens_used,
    }
