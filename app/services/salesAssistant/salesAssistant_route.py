# 1st version 
# """
# salesAssistant_route.py
# -----------------------
# FastAPI routes for the sales assistant.

# Endpoints:
#   POST   /sales/chat          — text chat (catalog or selection mode)
#   POST   /sales/voice         — voice input → Whisper → GPT → TTS audio out
#   DELETE /sales/audio/{key}   — frontend calls this after session ends to delete TTS audio from S3
#   GET    /sales/health        — health check
# """

# import logging
# from fastapi import APIRouter, File, Form, HTTPException, UploadFile
# from typing import Optional

# from app.services.salesAssistant.salesAssistant_schema import SalesMessage, SalesResponse
# from app.services.salesAssistant.salesAssistant import process_sales_chat, process_sales_voice
# from app.utils.upload_to_bucket import delete_file_from_s3

# logger = logging.getLogger(__name__)

# router = APIRouter(prefix="/sales", tags=["Sales Assistant"])


# # ============================================================
# #  POST /sales/chat
# # ============================================================

# @router.post("/chat", response_model=SalesResponse, summary="Sales assistant chat")
# async def sales_chat(request: SalesMessage):
#     """
#     Stateless sales assistant. Frontend sends everything needed per turn.

#     **Catalog mode** (`mode: "catalog"`): AI explores and suggests.
#     **Selection mode** (`mode: "selection"`): AI affirms and highlights benefits.
#     """
#     try:
#         return await process_sales_chat(request)
#     except Exception as e:
#         logger.error(f"sales_chat error: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail="Internal server error")


# # ============================================================
# #  POST /sales/voice
# # ============================================================

# @router.post("/voice", response_model=SalesResponse, summary="Sales assistant voice input")
# async def sales_voice(
#     audio: UploadFile = File(..., description="Audio file (webm, mp4, wav, m4a, mp3)"),
#     mode: str = Form("catalog"),
#     catalog: Optional[str] = Form(None, description="Full catalog JSON string (catalog mode)"),
#     selected_services: Optional[str] = Form(None, description="Selected items JSON string (selection mode)"),
#     selected_discounts: Optional[str] = Form(None, description="Applicable discounts JSON string"),
#     history: Optional[str] = Form(None, description="Conversation history JSON string"),
#     context: Optional[str] = Form(None, description="User context JSON string e.g. {user_age: 38}"),
# ):
#     """
#     Voice input for the sales assistant.

#     1. Transcribes audio via Whisper
#     2. Runs the same pipeline as /sales/chat
#     3. Returns TTS audio URL alongside the text reply

#     Frontend sends catalog/selection data as JSON strings in the multipart form
#     (same data as /sales/chat, just serialized since it's multipart).

#     Response includes `audio_url` — presigned S3 URL valid for 5 minutes.
#     Frontend should play it when present.
#     """
#     import json

#     try:
#         audio_bytes = await audio.read()

#         # Parse JSON form fields
#         def _parse(val):
#             if not val:
#                 return None
#             try:
#                 return json.loads(val)
#             except Exception:
#                 return None

#         request = SalesMessage(
#             message="",           # overwritten by Whisper transcript inside process_sales_voice
#             mode=mode,
#             catalog=_parse(catalog),
#             selected_services=_parse(selected_services) or [],
#             selected_discounts=_parse(selected_discounts) or [],
#             history=_parse(history) or [],
#             context=_parse(context),
#         )

#         return await process_sales_voice(audio_bytes, request)

#     except Exception as e:
#         logger.error(f"sales_voice error: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail="Internal server error")


# # ============================================================
# #  DELETE /sales/audio/{s3_key:path}
# # ============================================================

# @router.delete("/audio/{s3_key:path}", summary="Delete a TTS audio file from S3")
# async def delete_sales_audio(s3_key: str):
#     """
#     Called by frontend after the sales session ends to clean up TTS audio from S3.

#     Since salesAssistant is stateless (no Redis, no session tracking), the frontend
#     is responsible for collecting the S3 keys returned in each voice response
#     and deleting them when the chat window is closed.

#     The s3_key is the path returned inside audio_url responses, e.g.:
#       audio/sales-tts/abc123.mp3

#     Frontend should maintain a list of keys per session and call this
#     endpoint for each one on session end, or call it in batch by iterating.

#     Returns: { success, message }
#     """
#     if not s3_key or not s3_key.startswith("audio/sales-tts/"):
#         raise HTTPException(
#             status_code=400,
#             detail="Invalid S3 key. Must be under audio/sales-tts/ prefix."
#         )
#     try:
#         result = delete_file_from_s3(object_name=s3_key)
#         if result["success"]:
#             logger.info(f"Deleted sales TTS audio: {s3_key}")
#             return {"success": True, "message": f"Deleted {s3_key}"}
#         else:
#             raise HTTPException(status_code=500, detail=result["message"])
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"delete_sales_audio error: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail="Failed to delete audio file")


# # ============================================================
# #  GET /sales/health
# # ============================================================

# @router.get("/health", summary="Sales assistant health check")
# async def health():
#     return {"status": "ok", "service": "sales-assistant"}

# 2nd version
# """
# salesAssistant_route.py
# -----------------------
# FastAPI routes for the 6-screen sales assistant.

# Endpoints:
#   POST   /sales/chat/text        — text input, any screen
#   POST   /sales/chat/voice       — voice input, any screen
#   DELETE /sales/session/{id}     — wipe Redis session + delete S3 TTS audio on confirmation close
#   GET    /sales/debug/{id}       — inspect Redis session (dev only)
#   GET    /sales/health
# """

# import json
# import logging
# import os
# from typing import Optional

# from fastapi import APIRouter, File, Form, HTTPException, UploadFile

# from app.services.salesAssistant.salesAssistant_schema import (
#     SalesRequest, SalesResponse, Screen,
#     PersonalInfoData, ServiceSelectionData, PackageOptionsData,
#     DiscountData, AdditionalInstructionsData, ConfirmationData,
# )
# from app.services.salesAssistant.salesAssistant import (
#     process_sales,
#     process_sales_voice,
#     get_session,
#     delete_session,
#     get_tts_keys,
# )
# from app.utils.upload_to_bucket import delete_s3_keys_batch, delete_s3_prefix

# logger = logging.getLogger(__name__)

# router = APIRouter(prefix="/sales", tags=["Sales Assistant"])


# # ============================================================
# #  POST /sales/chat/text
# # ============================================================

# @router.post("/chat/text", response_model=SalesResponse, summary="Sales assistant — text input")
# async def sales_chat_text(request: SalesRequest):
#     """
#     Text chat for any of the 6 screens.

#     Send `message: ""` on screen load to trigger the AI's opening line for that screen.
#     Send `message: "user text here"` for follow-up conversation.

#     Always include `session_id` and `screen`.
#     Include the relevant screen data object when data changes on that screen.
#     """
#     try:
#         return await process_sales(request, generate_audio=False)
#     except Exception as e:
#         logger.error(f"sales_chat_text error: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail="Internal server error")


# # ============================================================
# #  POST /sales/chat/voice
# # ============================================================

# @router.post("/chat/voice", response_model=SalesResponse, summary="Sales assistant — voice input")
# async def sales_chat_voice(
#     audio: UploadFile = File(..., description="Audio file (webm, mp4, wav, m4a, mp3)"),
#     session_id: str = Form(...),
#     screen: str = Form(...),
#     message: str = Form(""),
#     personal_info: Optional[str] = Form(None),
#     service_selection: Optional[str] = Form(None),
#     package_options: Optional[str] = Form(None),
#     discount: Optional[str] = Form(None),
#     additional_instructions: Optional[str] = Form(None),
#     confirmation: Optional[str] = Form(None),
# ):
#     """
#     Voice input for any of the 6 screens.
#     Transcribes via Whisper, runs same pipeline as text, returns TTS audio_url.

#     Send screen data fields as JSON strings in the multipart form.
#     The `message` field is ignored — replaced by Whisper transcript.
#     """
#     def _parse(val, model_cls):
#         if not val:
#             return None
#         try:
#             return model_cls(**json.loads(val))
#         except Exception:
#             return None

#     try:
#         request = SalesRequest(
#             session_id=session_id,
#             screen=Screen(screen),
#             message=message,
#             personal_info=_parse(personal_info, PersonalInfoData),
#             service_selection=_parse(service_selection, ServiceSelectionData),
#             package_options=_parse(package_options, PackageOptionsData),
#             discount=_parse(discount, DiscountData),
#             additional_instructions=_parse(additional_instructions, AdditionalInstructionsData),
#             confirmation=_parse(confirmation, ConfirmationData),
#         )
#         audio_bytes = await audio.read()
#         return await process_sales_voice(audio_bytes, request)
#     except Exception as e:
#         logger.error(f"sales_chat_voice error: {e}", exc_info=True)
#         raise HTTPException(status_code=500, detail="Internal server error")


# # ============================================================
# #  DELETE /sales/session/{session_id}
# # ============================================================

# @router.delete("/session/{session_id}", summary="Close sales session — wipe Redis and S3 audio")
# async def delete_sales_session(session_id: str):
#     """
#     Called by frontend on confirmation screen close.
#     Deletes all TTS audio from S3 and wipes the Redis session.
#     Frontend calls this once the user sees the confirmation screen.
#     """
#     # Delete TTS audio from S3
#     tts_keys = get_tts_keys(session_id)
#     deleted_count = 0
#     if tts_keys:
#         result = delete_s3_keys_batch(tts_keys)
#         deleted_count = result.get("deleted_count", 0)
#     else:
#         # Fallback prefix scan
#         result = delete_s3_prefix(f"audio/sales-tts/{session_id}/")
#         deleted_count = result.get("deleted_count", 0)

#     # Wipe Redis
#     delete_session(session_id)

#     logger.info(f"Sales session {session_id} closed. {deleted_count} audio files deleted.")
#     return {"success": True, "session_id": session_id, "audio_deleted": deleted_count}


# # ============================================================
# #  GET /sales/debug/{session_id}
# # ============================================================

# @router.get("/debug/{session_id}", summary="[DEBUG] Inspect Redis session — disable in production")
# async def debug_sales_session(session_id: str):
#     if os.getenv("ENVIRONMENT", "development") == "production":
#         raise HTTPException(status_code=403, detail="Debug endpoint disabled in production")
#     session = get_session(session_id)
#     return {"exists": session is not None, "session": session}


# # ============================================================
# #  GET /sales/health
# # ============================================================

# @router.get("/health", summary="Sales assistant health check")
# async def health():
#     return {"status": "ok", "service": "sales-assistant"}

# 3rd version
"""
salesAssistant_route.py
-----------------------
FastAPI routes for the 6-screen sales assistant.

Endpoints:
  POST   /sales/chat/text        — text input, any screen
  POST   /sales/chat/voice       — voice input, any screen
  DELETE /sales/session/{id}     — wipe Redis session + delete S3 TTS audio on confirmation close
  GET    /sales/debug/{id}       — inspect Redis session (dev only)
  GET    /sales/health
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.services.salesAssistant.salesAssistant_schema import (
    SalesRequest, SalesResponse, Screen,
    PersonalInfoData, ServiceSelectionData, PackageOptionsData,
    DiscountData, AdditionalInstructionsData, ConfirmationData,
)
from app.services.salesAssistant.salesAssistant import (
    process_sales,
    process_sales_voice,
    get_session,
    delete_session,
    get_tts_keys,
)
from app.utils.upload_to_bucket import delete_s3_keys_batch, delete_s3_prefix

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sales", tags=["Sales Assistant"])


# ============================================================
#  POST /sales/chat/text
# ============================================================

@router.post("/chat/text", response_model=SalesResponse, summary="Sales assistant — text input")
async def sales_chat_text(request: SalesRequest):
    """
    Text chat for any of the 6 screens.

    Send `message: ""` on screen load to trigger the AI's opening line for that screen.
    Send `message: "user text here"` for follow-up conversation.

    Always include `session_id` and `screen`.
    Include the relevant screen data object when data changes on that screen.
    """
    try:
        return await process_sales(request, generate_audio=False)
    except Exception as e:
        logger.error(f"sales_chat_text error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
#  POST /sales/chat/voice
# ============================================================

@router.post("/chat/voice", response_model=SalesResponse, summary="Sales assistant — voice input or TTS trigger")
async def sales_chat_voice(
    session_id: str = Form(...),
    screen: str = Form(...),
    message: str = Form(""),
    audio: Optional[UploadFile] = File(None, description="Audio file — optional. Omit for TTS-only triggers (screen loads, service selections)"),
    personal_info: Optional[str] = Form(None),
    service_selection: Optional[str] = Form(None),
    package_options: Optional[str] = Form(None),
    discount: Optional[str] = Form(None),
    additional_instructions: Optional[str] = Form(None),
    confirmation: Optional[str] = Form(None),
):
    """
    Voice endpoint — two modes depending on whether audio is provided:

    **Mode 1 — User spoke (audio provided):**
    Whisper transcribes the audio → GPT replies → TTS audio back.
    The `message` field is ignored — replaced by Whisper transcript.

    **Mode 2 — Frontend trigger, no user audio (audio omitted):**
    Use this for screen loads, first name entered, service selected etc.
    Frontend already knows the text (e.g. "Sarah selected Internet and TV").
    Pass it in `message` — GPT replies → TTS audio back. No Whisper needed.

    Both modes return `audio_url` in the response.

    Examples of Mode 2 triggers:
    - Screen loads → message: ""
    - First name entered → message: "" + personal_info.first_name
    - Service selected → message: "I selected Internet and TV" + service_selection data
    - Package picked → message: "I selected Internet + TV Bundle" + package_options.selected_package
    """
    def _parse(val, model_cls):
        if not val:
            return None
        try:
            return model_cls(**json.loads(val))
        except Exception:
            return None

    try:
        request = SalesRequest(
            session_id=session_id,
            screen=Screen(screen),
            message=message,
            personal_info=_parse(personal_info, PersonalInfoData),
            service_selection=_parse(service_selection, ServiceSelectionData),
            package_options=_parse(package_options, PackageOptionsData),
            discount=_parse(discount, DiscountData),
            additional_instructions=_parse(additional_instructions, AdditionalInstructionsData),
            confirmation=_parse(confirmation, ConfirmationData),
        )

        if audio is not None:
            # Mode 1: user spoke — run Whisper then TTS
            audio_bytes = await audio.read()
            return await process_sales_voice(audio_bytes, request)
        else:
            # Mode 2: frontend trigger — skip Whisper, just run GPT + TTS
            return await process_sales(request, generate_audio=True)

    except Exception as e:
        logger.error(f"sales_chat_voice error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


# ============================================================
#  DELETE /sales/session/{session_id}
# ============================================================

@router.delete("/session/{session_id}", summary="Close sales session — wipe Redis and S3 audio")
async def delete_sales_session(session_id: str):
    """
    Called by frontend on confirmation screen close.
    Deletes all TTS audio from S3 and wipes the Redis session.
    Frontend calls this once the user sees the confirmation screen.
    """
    # Delete TTS audio from S3
    tts_keys = get_tts_keys(session_id)
    deleted_count = 0
    if tts_keys:
        result = delete_s3_keys_batch(tts_keys)
        deleted_count = result.get("deleted_count", 0)
    else:
        # Fallback prefix scan
        result = delete_s3_prefix(f"audio/sales-tts/{session_id}/")
        deleted_count = result.get("deleted_count", 0)

    # Wipe Redis
    delete_session(session_id)

    logger.info(f"Sales session {session_id} closed. {deleted_count} audio files deleted.")
    return {"success": True, "session_id": session_id, "audio_deleted": deleted_count}


# ============================================================
#  GET /sales/debug/{session_id}
# ============================================================

@router.get("/debug/{session_id}", summary="[DEBUG] Inspect Redis session — disable in production")
async def debug_sales_session(session_id: str):
    if os.getenv("ENVIRONMENT", "development") == "production":
        raise HTTPException(status_code=403, detail="Debug endpoint disabled in production")
    session = get_session(session_id)
    return {"exists": session is not None, "session": session}


# ============================================================
#  GET /sales/health
# ============================================================

@router.get("/health", summary="Sales assistant health check")
async def health():
    return {"status": "ok", "service": "sales-assistant"}