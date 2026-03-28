import base64
import json

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent import core

router = APIRouter()


def _extract_user(authorization: str | None) -> tuple[str, str]:
    """Extrae user_id y tenant_id del JWT de Supabase sin dependencias extra."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.removeprefix("Bearer ")
    try:
        payload_b64 = token.split(".")[1]
        # Ajustar padding base64url
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        user_id: str = payload["sub"]
        return user_id, user_id  # tenant_id = user_id
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido")


# ─── Schemas ──────────────────────────────────────────────────────────────────

class CreateConversationBody(BaseModel):
    context_type: str = "general"


class SendMessageBody(BaseModel):
    content: str


# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/conversations")
def create_conversation(
    body: CreateConversationBody,
    authorization: str | None = Header(default=None),
):
    user_id, tenant_id = _extract_user(authorization)
    conv_id = core.create_conversation(tenant_id, user_id)
    return {"id": conv_id, "title": None, "context_type": body.context_type}


@router.post("/conversations/{conversation_id}/messages")
def send_message(
    conversation_id: str,
    body: SendMessageBody,
    authorization: str | None = Header(default=None),
):
    user_id, tenant_id = _extract_user(authorization)

    def generate():
        try:
            result = core.chat(
                tenant_id=tenant_id,
                user_id=user_id,
                message=body.content,
                conversation_id=conversation_id,
            )
            text: str = result["message"]

            # Streamear en chunks de ~5 palabras para efecto de tipeo
            words = text.split(" ")
            chunk_size = 5
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i : i + chunk_size])
                if i + chunk_size < len(words):
                    chunk += " "
                yield f"data: {chunk}\n\n"

            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: ⚠️ {str(e)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
