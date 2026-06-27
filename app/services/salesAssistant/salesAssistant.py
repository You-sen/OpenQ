# """
# salesAssistant.py
# -----------------
# Core AI logic for the sales assistant.

# No Redis. No state machine. Stateless per-turn — frontend owns the state.

# Two prompt modes:
#   CATALOG   → AI acts as a consultant: "Tell me what you need, here's what we have"
#   SELECTION → AI acts as an affirming salesman: "Great choice! Here's why it's perfect for you"

# After generating the reply, a second lightweight GPT call extracts which
# sub_service IDs the AI recommended, so the frontend can highlight them.
# """

# import json
# import logging
# import tempfile
# import uuid
# from typing import Optional

# from openai import AsyncOpenAI

# from app.core.config import settings
# from app.services.salesAssistant.salesAssistant_schema import (
#     SalesMessage,
#     SalesResponse,
#     Service,
#     SubService,
#     Discount,
# )

# logger = logging.getLogger(__name__)

# client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
# MODEL = settings.OPENAI_MODEL
# CONTEXT_WINDOW = 6  # last N history turns fed to GPT


# # ============================================================
# #  Catalog formatter — turns structured JSON into readable text
# # ============================================================

# def _format_catalog(catalog: list[Service]) -> str:
#     """Convert full catalog to a clean text block for the system prompt."""
#     if not catalog:
#         return "No catalog provided."
#     lines = []
#     for svc in catalog:
#         lines.append(f"\n## {svc.name}")
#         for sub in svc.sub_services:
#             price = sub.price_label or (f"${sub.price}/mo" if sub.price else "pricing on request")
#             tag = f" [{sub.tag}]" if sub.tag else ""
#             lines.append(f"  • {sub.name}{tag} — {price}")
#             if sub.description:
#                 lines.append(f"    {sub.description}")
#             if sub.features:
#                 for f in sub.features:
#                     lines.append(f"    - {f}")
#         if svc.applicable_discounts:
#             lines.append(f"  Discounts available:")
#             for d in svc.applicable_discounts:
#                 val = f" ({d.value})" if d.value else ""
#                 cond = f" | Condition: {d.condition}" if d.condition else ""
#                 lines.append(f"    🏷 {d.label}{val}{cond}")
#     return "\n".join(lines)


# def _format_selection(selected: list[SubService], discounts: list[Discount]) -> str:
#     """Format what the user selected into a clean text block."""
#     if not selected:
#         return "No items selected yet."
#     lines = ["User has selected:"]
#     for sub in selected:
#         price = sub.price_label or (f"${sub.price}/mo" if sub.price else "")
#         lines.append(f"  ✓ {sub.name} — {price}")
#         if sub.description:
#             lines.append(f"    {sub.description}")
#         if sub.features:
#             for f in sub.features:
#                 lines.append(f"    - {f}")
#     if discounts:
#         lines.append("\nApplicable offers/discounts:")
#         for d in discounts:
#             val = f" ({d.value})" if d.value else ""
#             lines.append(f"  🏷 {d.label}{val}")
#             if d.description:
#                 lines.append(f"    {d.description}")
#     return "\n".join(lines)


# def _format_context(context: Optional[dict]) -> str:
#     if not context:
#         return ""
#     parts = []
#     if context.get("user_age"):
#         parts.append(f"User age: {context['user_age']}")
#     if context.get("is_new_customer") is not None:
#         parts.append(f"New customer: {context['is_new_customer']}")
#     for k, v in context.items():
#         if k not in ("user_age", "is_new_customer"):
#             parts.append(f"{k}: {v}")
#     return "Customer context: " + ", ".join(parts) if parts else ""


# # ============================================================
# #  Prompt builders
# # ============================================================

# SALESMAN_PERSONA = """You are Alex, a friendly and knowledgeable sales consultant for a telecom company.
# Your personality:
# - Warm, enthusiastic, and genuinely helpful — never pushy or robotic
# - You use natural conversational language, not bullet-point lists
# - You highlight real benefits, not just specs (e.g. "stream 4K on 5 devices at once" not just "1Gbps")
# - You acknowledge the user's situation and tailor your suggestion to them
# - You mention discounts and promos naturally when relevant
# - You keep responses concise — 2 to 4 sentences max unless the user asks for detail
# - Never make up prices or features not in the provided catalog
# - Never say "I cannot" or "As an AI" — you are Alex, a sales consultant"""


# def _build_catalog_prompt(request: SalesMessage) -> str:
#     catalog_text = _format_catalog(request.catalog or [])
#     context_text = _format_context(request.context)
#     return f"""{SALESMAN_PERSONA}

# CURRENT MODE: The customer is browsing and hasn't selected anything yet.
# Your goal: understand their needs through friendly questions, then guide them toward the best option.
# If they seem undecided, briefly highlight what makes each service stand out.
# If they share a preference or use case, recommend specifically.

# {context_text}

# AVAILABLE SERVICES AND PRICING:
# {catalog_text}

# IMPORTANT: When you recommend something, mention the sub-service name exactly as listed above.
# At the end of your response, on a NEW LINE, output ONLY this JSON (no explanation):
# SUGGESTIONS:{{"ids": ["id1", "id2"]}}
# If you have no specific recommendation yet, output SUGGESTIONS:{{"ids": []}}"""


# def _build_selection_prompt(request: SalesMessage) -> str:
#     selection_text = _format_selection(
#         request.selected_services or [],
#         request.selected_discounts or [],
#     )
#     context_text = _format_context(request.context)
#     return f"""{SALESMAN_PERSONA}

# CURRENT MODE: The customer has selected specific services.
# Your goal: affirm their choice enthusiastically, highlight why it's a great fit,
# mention any applicable discounts naturally, and optionally suggest a complementary add-on
# if it genuinely makes sense (don't force it).

# {context_text}

# {selection_text}

# IMPORTANT: Sound like a great salesperson — make them feel confident about their choice.
# At the end of your response, on a NEW LINE, output ONLY this JSON (no explanation):
# SUGGESTIONS:{{"ids": ["id1", "id2"]}}
# Where ids are the sub_service IDs you're actively recommending or affirming."""


# # ============================================================
# #  Reply parser — extract message and suggested IDs
# # ============================================================

# def _parse_reply(raw: str) -> tuple[str, list[str]]:
#     """
#     Split the raw GPT reply into (message, suggested_ids).
#     GPT is instructed to append SUGGESTIONS:{...} on its last line.
#     """
#     suggested_ids = []
#     message = raw.strip()

#     if "SUGGESTIONS:" in raw:
#         parts = raw.rsplit("SUGGESTIONS:", 1)
#         message = parts[0].strip()
#         try:
#             suggestions_raw = parts[1].strip()
#             parsed = json.loads(suggestions_raw)
#             suggested_ids = parsed.get("ids", [])
#         except Exception:
#             pass  # IDs are nice-to-have, not critical

#     return message, suggested_ids


# # ============================================================
# #  Main entry point
# # ============================================================

# async def process_sales_chat(request: SalesMessage) -> SalesResponse:
#     """
#     Single-turn sales assistant.

#     Args:
#         request: SalesMessage with mode, catalog or selection, history, user message

#     Returns:
#         SalesResponse with AI reply and optionally highlighted sub_service IDs
#     """
#     # Build system prompt based on mode
#     if request.mode == "selection" and request.selected_services:
#         system_prompt = _build_selection_prompt(request)
#     else:
#         system_prompt = _build_catalog_prompt(request)

#     # Build message list: history (capped) + current user message
#     history = (request.history or [])[-CONTEXT_WINDOW:]
#     messages = [
#         {"role": m["role"], "content": m["content"]}
#         for m in history
#         if m.get("role") in ("user", "assistant") and m.get("content")
#     ]
#     messages.append({"role": "user", "content": request.message})

#     try:
#         response = await client.chat.completions.create(
#             model=MODEL,
#             messages=[{"role": "system", "content": system_prompt}] + messages,
#             max_tokens=400,
#             temperature=0.75,   # slightly creative — sounds more natural
#         )
#         raw_reply = response.choices[0].message.content.strip()
#         message, suggested_ids = _parse_reply(raw_reply)

#         return SalesResponse(
#             message=message,
#             suggested_ids=suggested_ids,
#         )

#     except Exception as e:
#         logger.error(f"SalesAssistant GPT call failed: {e}", exc_info=True)
#         return SalesResponse(
#             message="I'm having a little trouble right now — could you give me a moment and try again?",
#             suggested_ids=[],
#         )


# # ============================================================
# #  TTS helper
# # ============================================================

# async def _text_to_speech(text: str) -> Optional[str]:
#     """Convert reply text to MP3 via OpenAI TTS, upload to S3, return presigned URL."""
#     from app.utils.upload_to_bucket import upload_bytes_to_s3, generate_presigned_url
#     try:
#         response = await client.audio.speech.create(
#             model=settings.OPENAI_TTS_MODEL,
#             voice=settings.OPENAI_TTS_VOICE,
#             input=text,
#             response_format="mp3",
#         )
#         s3_key = f"audio/sales-tts/{uuid.uuid4()}.mp3"
#         result = upload_bytes_to_s3(
#             data=response.content,
#             object_name=s3_key,
#             content_type="audio/mpeg",
#             public=False,
#         )
#         if result["success"]:
#             return generate_presigned_url(s3_key, expires_in=300)
#         return None
#     except Exception as e:
#         logger.error(f"Sales TTS failed: {e}", exc_info=True)
#         return None


# # ============================================================
# #  Voice entry point
# # ============================================================

# async def process_sales_voice(audio_bytes: bytes, request: SalesMessage) -> SalesResponse:
#     """
#     Voice handler:
#     1. Whisper transcription of audio
#     2. Run through process_sales_chat with transcribed text
#     3. TTS the reply → S3 → presigned URL

#     The request object carries catalog/selection/history/context as usual —
#     only the message field is overwritten with the Whisper transcript.
#     """
#     # 1. Transcribe
#     try:
#         with tempfile.NamedTemporaryFile(suffix=".webm", delete=True) as tmp:
#             tmp.write(audio_bytes)
#             tmp.flush()
#             with open(tmp.name, "rb") as f:
#                 transcription = await client.audio.transcriptions.create(
#                     model=settings.OPENAI_WHISPER_MODEL,
#                     file=f,
#                     response_format="text",
#                 )
#         transcript = transcription.strip() if isinstance(transcription, str) else str(transcription)
#         logger.info(f"Sales Whisper transcript: {transcript[:120]}")
#     except Exception as e:
#         logger.error(f"Sales Whisper failed: {e}", exc_info=True)
#         return SalesResponse(
#             message="I couldn't catch that — could you try again or type your question?",
#             suggested_ids=[],
#         )

#     # 2. Override message with transcript and run text pipeline
#     request.message = transcript
#     text_response = await process_sales_chat(request)

#     # 3. TTS
#     audio_url = await _text_to_speech(text_response.message)

#     return SalesResponse(
#         message=text_response.message,
#         suggested_ids=text_response.suggested_ids,
#         audio_url=audio_url,
#     )

"""
salesAssistant.py
-----------------
Core AI logic for the 6-screen sales assistant.

Redis stores per-session:
  - screen history (messages)
  - collected data per screen (merged on every request)
  - current screen

Session is wiped on confirmation screen close.
"""

import json
import logging
import re
import tempfile
import uuid
from typing import Optional

from openai import AsyncOpenAI

from app.core.config import settings
from app.utils.cache_manager import HiringSessionCache
from app.services.salesAssistant.salesAssistant_schema import (
    SalesRequest, SalesResponse, Screen,
)

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
MODEL = settings.OPENAI_MODEL
CONTEXT_WINDOW = 10
SESSION_TTL = 2 * 3600  # 2 hours

# Reuse HiringSessionCache Redis pattern under a different key prefix
from app.utils.cache_manager import hiring_cache as _base_cache
import redis as _redis_lib
import os as _os

_redis_client = _redis_lib.from_url(
    _os.getenv("REDIS_URL", "redis://redis:6379"),   # uses Docker service name — never localhost
    db=int(_os.getenv("REDIS_DB", 0)),
    decode_responses=False,
)


# ============================================================
#  Simple Redis helpers for sales sessions
# ============================================================

SALES_KEY_PREFIX = "sales_session"


def _key(session_id: str) -> str:
    return f"{SALES_KEY_PREFIX}:{session_id}"


def get_session(session_id: str) -> Optional[dict]:
    try:
        raw = _redis_client.get(_key(session_id))
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.error(f"sales get_session error: {e}")
        return None


def save_session(session_id: str, session: dict) -> None:
    try:
        _redis_client.setex(_key(session_id), SESSION_TTL, json.dumps(session))
    except Exception as e:
        logger.error(f"sales save_session error: {e}")


def delete_session(session_id: str) -> None:
    try:
        _redis_client.delete(_key(session_id))
    except Exception as e:
        logger.error(f"sales delete_session error: {e}")


def create_session(session_id: str) -> dict:
    session = {
        "session_id": session_id,
        "screen": Screen.personal_info,
        "first_name": None,
        "messages": [],
        "tts_keys": [],
        # collected data per screen
        "personal_info": {},
        "service_selection": {},
        "package_options": {},
        "discount": {},
        "additional_instructions": {},
        "confirmation": {},
    }
    save_session(session_id, session)
    return session


def append_message(session_id: str, role: str, content: str) -> None:
    session = get_session(session_id)
    if not session:
        return
    session.setdefault("messages", []).append({"role": role, "content": content})
    save_session(session_id, session)


def register_tts_key(session_id: str, s3_key: str) -> None:
    session = get_session(session_id)
    if not session:
        return
    session.setdefault("tts_keys", []).append(s3_key)
    save_session(session_id, session)


def get_tts_keys(session_id: str) -> list:
    session = get_session(session_id)
    return session.get("tts_keys", []) if session else []


# ============================================================
#  Merge screen_data into session
# ============================================================

def _merge_screen_data(session: dict, request: SalesRequest) -> dict:
    """Merge whatever screen_data the frontend sent into the session."""
    screen = request.screen.value

    if request.personal_info:
        data = request.personal_info.model_dump(exclude_none=True)
        session.setdefault("personal_info", {}).update(data)
        if data.get("first_name"):
            session["first_name"] = data["first_name"]

    elif request.service_selection:
        session["service_selection"] = request.service_selection.model_dump(exclude_none=True)

    elif request.package_options:
        session["package_options"] = request.package_options.model_dump(exclude_none=True)

    elif request.discount:
        existing = session.get("discount", {})
        existing.update(request.discount.model_dump(exclude_none=True))
        session["discount"] = existing

    elif request.additional_instructions:
        session["additional_instructions"] = request.additional_instructions.model_dump(exclude_none=True)

    elif request.confirmation:
        session["confirmation"] = request.confirmation.model_dump(exclude_none=True)

    session["screen"] = screen
    return session


# ============================================================
#  Prompt builders per screen
# ============================================================

PERSONA = (
    "You are a warm, professional AI sales assistant for Connect Pro, a telecom company. "
    "Be concise — 2 to 4 sentences max. Never make up prices or packages not in the provided data. "
    "Never say 'As an AI' — you are a Connect Pro assistant."
)


def _build_prompt(session: dict, request: SalesRequest) -> str:
    screen = request.screen
    first_name = session.get("first_name") or "there"

    if screen == Screen.personal_info:
        return (
            f"{PERSONA}\n\n"
            f"The customer's first name is {first_name}. "
            "When the conversation starts (empty message), greet them with exactly:\n"
            f"'Hello, {first_name}! I am your AI Assistant and I will be happy to assist you "
            "in your application process. If you have any questions, you can talk to me directly "
            "or chat with me. During the process you can always say \"Stop Talking\" so that I "
            "will stop and wait for your chat input. You can switch back to voice mode from the UI anytime.'\n"
            "For follow-up questions, answer helpfully about the signup process."
        )

    elif screen == Screen.service_selection:
        services = session.get("service_selection", {}).get("selected_services") or []
        selected_text = f"Currently selected: {', '.join(services)}" if services else "Nothing selected yet."
        return (
            f"{PERSONA}\n\n"
            "Available services: Phone, Internet, TV.\n"
            f"{selected_text}\n\n"
            "When the screen loads (empty message), say:\n"
            "'You can choose a single service or multiple services. If you pick multiple services, "
            "you get a better discount on your plan. Take a look at the available options and let "
            "me know if you have any questions!'\n"
            "For follow-up questions, help the user decide which services fit their needs. "
            "If they select multiple services, proactively mention bundle discounts."
        )

    elif screen == Screen.package_options:
        pkg_data = session.get("package_options", {})
        std = pkg_data.get("standard_packages") or []
        custom = pkg_data.get("custom_packages") or []
        selected = pkg_data.get("selected_package")

        std_text = "\n".join(
            f"  - {p.get('name')} ({p.get('price_label') or ''}) {p.get('tag') or ''}: "
            f"{p.get('description') or ''}"
            for p in std
        ) or "No standard packages provided yet."

        custom_text = "\n".join(
            f"  - {p.get('name')} ({p.get('price_label') or ''}) : {p.get('description') or ''}"
            for p in custom
        ) if custom else ""

        selected_text = f"User selected: {selected.get('name') if selected else 'nothing yet'}"

        custom_section = f"Custom packages:\n{custom_text}\n" if custom_text else ""

        return (
            f"{PERSONA}\n\n"
            f"Standard packages:\n{std_text}\n"
            f"{custom_section}"
            f"{selected_text}\n\n"
            "When the screen loads (empty message), say:\n"
            "'Please let me know if you do not see the option you want — I can help you customize "
            "your plan! Click the Customize button and choose your best combination from the "
            "extended options list.'\n"
            "If the user selects a package, affirm their choice enthusiastically and explain "
            "why it is a great fit for them based on the services they selected earlier. "
            "Help them compare options if they ask."
        )

    elif screen == Screen.discount:
        disc = session.get("discount", {})
        options = disc.get("eligibility_options") or []
        selected = disc.get("eligibility_selected")
        employer_map = disc.get("employer_discounts") or {}
        employer_list = ", ".join(employer_map.keys()) if employer_map else "none provided"

        return (
            f"{PERSONA}\n\n"
            f"Eligibility options: {', '.join(options) if options else 'Senior, Military, Student, Low Income, None of the above applies to me'}\n"
            f"User selected eligibility: {selected or 'not yet selected'}\n"
            f"Employer discount map (confidential — do not list all to user): {json.dumps(employer_map)}\n\n"
            "RULES:\n"
            "- If user selected a standard eligibility option: confirm the discount warmly and "
            "encourage them to proceed.\n"
            "- If user selected 'None of the above applies to me' or 'none_of_above': say "
            "'Do not worry — let me see what other discounts we have for you. I will look for a "
            "referral code right now so that you can still apply. If you are working at a company, "
            "just say the employer name and I will quickly check if we have a partnership with them.'\n"
            "- If user mentions an employer name: check the employer discount map.\n"
            "  - If found: say 'Great news — we work with [Employer]! [description]' and output "
            "on a NEW LINE: PROMO_CODE:[code]\n"
            "  - If not found: say 'We don't have a relationship with [Employer] — but here's "
            "1 month free on a 12-month signup.' and output on a NEW LINE: PROMO_CODE:FREE1M\n"
            "- When screen loads (empty message): say 'Great news — we provide very good discounts "
            "for various customer segments. From the list, pick the option that best describes you "
            "and we will apply it automatically.'"
        )

    elif screen == Screen.additional_instructions:
        return (
            f"{PERSONA}\n\n"
            "When screen loads (empty message), say:\n"
            "'If you have any additional instructions for us, please provide them here. For example, "
            "if your home address has a gate code, please share it so our team can reach you "
            "without delays.'\n"
            "For follow-up, acknowledge their instructions warmly and confirm they have been noted."
        )

    elif screen == Screen.confirmation:
        conf = session.get("confirmation", {})
        summary = json.dumps(conf, indent=2) if conf else "No summary provided."
        return (
            f"{PERSONA}\n\n"
            f"Order summary: {summary}\n\n"
            "When screen loads (empty message), say:\n"
            "'You are all set! We have received your application and we will take care of you "
            "from here. Welcome to Connect Pro!'\n"
            "For any follow-up questions, reassure the customer and answer warmly."
        )

    return PERSONA


# ============================================================
#  PROMO_CODE extractor
# ============================================================

def _extract_promo_code(reply: str) -> tuple[str, Optional[str]]:
    """Extract PROMO_CODE:[code] from reply if present."""
    promo = None
    match = re.search(r"PROMO_CODE:(\S+)", reply)
    if match:
        promo = match.group(1)
        reply = re.sub(r"\s*PROMO_CODE:\S+", "", reply).strip()
    return reply, promo


# ============================================================
#  TTS helper
# ============================================================

async def _text_to_speech(text: str, session_id: str) -> Optional[str]:
    from app.utils.upload_to_bucket import upload_bytes_to_s3, generate_presigned_url
    try:
        response = await client.audio.speech.create(
            model=settings.OPENAI_TTS_MODEL,
            voice=settings.OPENAI_TTS_VOICE,
            input=text,
            response_format="mp3",
        )
        s3_key = f"audio/sales-tts/{session_id}/{uuid.uuid4()}.mp3"
        result = upload_bytes_to_s3(
            data=response.content,
            object_name=s3_key,
            content_type="audio/mpeg",
            public=False,
        )
        if result["success"]:
            register_tts_key(session_id, s3_key)
            from app.utils.upload_to_bucket import generate_presigned_url
            return generate_presigned_url(s3_key, expires_in=300)
        return None
    except Exception as e:
        logger.error(f"Sales TTS failed: {e}", exc_info=True)
        return None


# ============================================================
#  Core processor
# ============================================================

async def process_sales(request: SalesRequest, generate_audio: bool = False) -> SalesResponse:
    """
    Single entry point for both text and voice.
    generate_audio=True when called from voice endpoint.
    """
    # Load or create session
    session = get_session(request.session_id)
    if session is None:
        session = create_session(request.session_id)

    # Merge screen data
    session = _merge_screen_data(session, request)
    save_session(request.session_id, session)

    # Build prompt
    system_prompt = _build_prompt(session, request)

    # Build messages: history + current user message
    messages = session.get("messages", [])[-CONTEXT_WINDOW:]
    openai_messages = [{"role": m["role"], "content": m["content"]} for m in messages]

    if request.message:
        openai_messages.append({"role": "user", "content": request.message})

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}] + openai_messages,
            max_tokens=300,
            temperature=0.7,
        )
        raw_reply = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Sales GPT failed: {e}", exc_info=True)
        return SalesResponse(message="I'm having trouble right now — please try again in a moment.")

    # Extract promo code if present
    reply, promo_code = _extract_promo_code(raw_reply)

    # Persist messages
    if request.message:
        append_message(request.session_id, "user", request.message)
    append_message(request.session_id, "assistant", reply)

    # TTS
    audio_url = None
    if generate_audio:
        audio_url = await _text_to_speech(reply, request.session_id)

    return SalesResponse(
        message=reply,
        audio_url=audio_url,
        promo_code=promo_code,
    )


async def process_sales_voice(audio_bytes: bytes, request: SalesRequest) -> SalesResponse:
    """Whisper → process_sales with audio=True."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            with open(tmp.name, "rb") as f:
                transcription = await client.audio.transcriptions.create(
                    model=settings.OPENAI_WHISPER_MODEL,
                    file=f,
                    response_format="text",
                )
        request.message = transcription.strip() if isinstance(transcription, str) else str(transcription)
        logger.info(f"Sales Whisper [{request.session_id}]: {request.message[:100]}")
    except Exception as e:
        logger.error(f"Sales Whisper failed: {e}", exc_info=True)
        return SalesResponse(message="I couldn't catch that — could you try again?")

    return await process_sales(request, generate_audio=True)