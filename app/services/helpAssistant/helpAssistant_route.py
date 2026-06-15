"""
helpAssistant_router.py
-----------------------
FastAPI routes for the knowledge-base help assistant.

Endpoints:
  POST /help/chat        — text question → grounded AI answer
  POST /help/voice       — audio input → Whisper → answer → TTS audio
  POST /help/ingest      — manually re-trigger knowledge base ingestion
  GET  /help/health      — health + ChromaDB status

Knowledge ingestion:
  Runs automatically at app startup via the lifespan hook in main.py.
  Add to main.py:

    from contextlib import asynccontextmanager
    from app.services.helpAssistant.helpAssistant import ingest_knowledge_base

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ingest_knowledge_base()   # runs once on startup
        yield

    app = FastAPI(lifespan=lifespan, ...)
"""

import logging
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.services.helpAssistant.helpAssistant_schema import HelpMessage, HelpResponse
from app.services.helpAssistant.helpAssistant import (
    ingest_knowledge_base,
    process_help_chat,
    process_help_voice,
)
from app.utils.upload_to_bucket import delete_file_from_s3

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/help", tags=["Help Assistant"])


# ============================================================
#  POST /help/chat
# ============================================================

@router.post("/chat", response_model=HelpResponse, summary="Ask a text question")
async def help_chat(request: HelpMessage):
    """
    Text-based help assistant.

    - Semantically searches the knowledge base (.md files) for relevant context
    - Answers strictly from that context — will not hallucinate facts
    - Pass `history` to maintain multi-turn conversation context

    **Request example:**
    ```json
    {
      "message": "What are your internet plans?",
      "history": [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello! How can I help you today?"}
      ]
    }
    ```

    **Response includes `sources`** — list of .md files used (e.g. `["services.md"]`).
    Frontend can show these as "Source" badges if desired.
    """
    try:
        return await process_help_chat(request)
    except Exception as e:
        logger.error(f"help_chat error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
#  POST /help/voice
# ============================================================

@router.post("/help/voice", response_model=HelpResponse, summary="Ask a voice question")
async def help_voice(
    audio: UploadFile = File(..., description="Audio file (webm, mp4, wav, m4a, mp3)"),
):
    """
    Voice-based help assistant.

    1. Transcribes audio via OpenAI Whisper
    2. Retrieves relevant knowledge chunks
    3. Generates a grounded text answer
    4. Converts answer to speech (TTS) and uploads to S3
    5. Returns `{ message, sources, audio_url }`

    Frontend should play `audio_url` when present (presigned URL, valid 5 minutes).

    Note: voice turns are single-turn (no history). For multi-turn voice,
    frontend can send prior turns as text history on the /chat endpoint
    after transcribing locally.
    """
    try:
        audio_bytes = await audio.read()
        return await process_help_voice(audio_bytes)
    except Exception as e:
        logger.error(f"help_voice error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
#  DELETE /help/audio/{s3_key:path}
# ============================================================

@router.delete("/audio/{s3_key:path}", summary="Delete a TTS audio file from S3")
async def delete_help_audio(s3_key: str):
    """
    Called by frontend after the help session ends to clean up TTS audio from S3.

    Since helpAssistant is stateless (no Redis), the frontend collects the
    audio_url S3 key from each voice response and calls this endpoint
    for each one when the chat widget is closed.

    The s3_key is the object path under the bucket, e.g.:
      audio/help-tts/abc123.mp3

    Returns: { success, message }
    """
    if not s3_key or not s3_key.startswith("audio/help-tts/"):
        raise HTTPException(
            status_code=400,
            detail="Invalid S3 key. Must be under audio/help-tts/ prefix."
        )
    try:
        result = delete_file_from_s3(object_name=s3_key)
        if result["success"]:
            logger.info(f"Deleted help TTS audio: {s3_key}")
            return {"success": True, "message": f"Deleted {s3_key}"}
        else:
            raise HTTPException(status_code=500, detail=result["message"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"delete_help_audio error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete audio file")


# ============================================================
#  POST /help/ingest
# ============================================================

@router.post("/ingest", summary="Re-ingest knowledge base from .md files")
async def help_ingest(force: bool = False):
    """
    Manually trigger knowledge base ingestion.

    - Runs automatically at startup, so you only need this when .md files change.
    - `force=true` re-ingests all files even if already present in ChromaDB.
    - `force=false` (default) skips already-ingested files.

    Returns which files were ingested vs skipped and total chunk count.
    """
    try:
        result = ingest_knowledge_base(force=force)
        return {
            "status": "ok",
            "ingested": result["ingested"],
            "skipped": result["skipped"],
            "total_chunks": result["total_chunks"],
        }
    except Exception as e:
        logger.error(f"Ingestion error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(e)}")


# ============================================================
#  GET /help/health
# ============================================================

@router.get("/health", summary="Help assistant health check")
async def help_health():
    """Returns ChromaDB connection status and chunk count."""
    try:
        from app.services.helpAssistant.helpAssistant import _get_collection
        collection = _get_collection()
        count = collection.count()
        return {
            "status": "ok",
            "chromadb": "connected",
            "total_chunks": count,
        }
    except Exception as e:
        return {
            "status": "degraded",
            "chromadb": "error",
            "error": str(e),
        }