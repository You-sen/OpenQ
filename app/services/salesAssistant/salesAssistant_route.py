"""
salesAssistant_route.py
-----------------------
FastAPI routes for the sales assistant.

Endpoints:
  POST   /sales/chat          — text chat (catalog or selection mode)
  POST   /sales/voice         — voice input → Whisper → GPT → TTS audio out
  DELETE /sales/audio/{key}   — frontend calls this after session ends to delete TTS audio from S3
  GET    /sales/health        — health check
"""

import logging
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from typing import Optional

from app.services.salesAssistant.salesAssistant_schema import SalesMessage, SalesResponse
from app.services.salesAssistant.salesAssistant import process_sales_chat, process_sales_voice
from app.utils.upload_to_bucket import delete_file_from_s3

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sales", tags=["Sales Assistant"])


# ============================================================
#  POST /sales/chat
# ============================================================

@router.post("/chat", response_model=SalesResponse, summary="Sales assistant chat")
async def sales_chat(request: SalesMessage):
    """
    Stateless sales assistant. Frontend sends everything needed per turn.

    **Catalog mode** (`mode: "catalog"`): AI explores and suggests.
    **Selection mode** (`mode: "selection"`): AI affirms and highlights benefits.
    """
    try:
        return await process_sales_chat(request)
    except Exception as e:
        logger.error(f"sales_chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
#  POST /sales/voice
# ============================================================

@router.post("/voice", response_model=SalesResponse, summary="Sales assistant voice input")
async def sales_voice(
    audio: UploadFile = File(..., description="Audio file (webm, mp4, wav, m4a, mp3)"),
    mode: str = Form("catalog"),
    catalog: Optional[str] = Form(None, description="Full catalog JSON string (catalog mode)"),
    selected_services: Optional[str] = Form(None, description="Selected items JSON string (selection mode)"),
    selected_discounts: Optional[str] = Form(None, description="Applicable discounts JSON string"),
    history: Optional[str] = Form(None, description="Conversation history JSON string"),
    context: Optional[str] = Form(None, description="User context JSON string e.g. {user_age: 38}"),
):
    """
    Voice input for the sales assistant.

    1. Transcribes audio via Whisper
    2. Runs the same pipeline as /sales/chat
    3. Returns TTS audio URL alongside the text reply

    Frontend sends catalog/selection data as JSON strings in the multipart form
    (same data as /sales/chat, just serialized since it's multipart).

    Response includes `audio_url` — presigned S3 URL valid for 5 minutes.
    Frontend should play it when present.
    """
    import json

    try:
        audio_bytes = await audio.read()

        # Parse JSON form fields
        def _parse(val):
            if not val:
                return None
            try:
                return json.loads(val)
            except Exception:
                return None

        request = SalesMessage(
            message="",           # overwritten by Whisper transcript inside process_sales_voice
            mode=mode,
            catalog=_parse(catalog),
            selected_services=_parse(selected_services) or [],
            selected_discounts=_parse(selected_discounts) or [],
            history=_parse(history) or [],
            context=_parse(context),
        )

        return await process_sales_voice(audio_bytes, request)

    except Exception as e:
        logger.error(f"sales_voice error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
#  DELETE /sales/audio/{s3_key:path}
# ============================================================

@router.delete("/audio/{s3_key:path}", summary="Delete a TTS audio file from S3")
async def delete_sales_audio(s3_key: str):
    """
    Called by frontend after the sales session ends to clean up TTS audio from S3.

    Since salesAssistant is stateless (no Redis, no session tracking), the frontend
    is responsible for collecting the S3 keys returned in each voice response
    and deleting them when the chat window is closed.

    The s3_key is the path returned inside audio_url responses, e.g.:
      audio/sales-tts/abc123.mp3

    Frontend should maintain a list of keys per session and call this
    endpoint for each one on session end, or call it in batch by iterating.

    Returns: { success, message }
    """
    if not s3_key or not s3_key.startswith("audio/sales-tts/"):
        raise HTTPException(
            status_code=400,
            detail="Invalid S3 key. Must be under audio/sales-tts/ prefix."
        )
    try:
        result = delete_file_from_s3(object_name=s3_key)
        if result["success"]:
            logger.info(f"Deleted sales TTS audio: {s3_key}")
            return {"success": True, "message": f"Deleted {s3_key}"}
        else:
            raise HTTPException(status_code=500, detail=result["message"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_sales_audio error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete audio file")


# ============================================================
#  GET /sales/health
# ============================================================

@router.get("/health", summary="Sales assistant health check")
async def health():
    return {"status": "ok", "service": "sales-assistant"}