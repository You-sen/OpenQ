"""
salesAssistant.py
-----------------
Core AI logic for the sales assistant.

No Redis. No state machine. Stateless per-turn — frontend owns the state.

Two prompt modes:
  CATALOG   → AI acts as a consultant: "Tell me what you need, here's what we have"
  SELECTION → AI acts as an affirming salesman: "Great choice! Here's why it's perfect for you"

After generating the reply, a second lightweight GPT call extracts which
sub_service IDs the AI recommended, so the frontend can highlight them.
"""

import json
import logging
import tempfile
import uuid
from typing import Optional

from openai import AsyncOpenAI

from app.core.config import settings
from app.services.salesAssistant.salesAssistant_schema import (
    SalesMessage,
    SalesResponse,
    Service,
    SubService,
    Discount,
)

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
MODEL = settings.OPENAI_MODEL
CONTEXT_WINDOW = 6  # last N history turns fed to GPT


# ============================================================
#  Catalog formatter — turns structured JSON into readable text
# ============================================================

def _format_catalog(catalog: list[Service]) -> str:
    """Convert full catalog to a clean text block for the system prompt."""
    if not catalog:
        return "No catalog provided."
    lines = []
    for svc in catalog:
        lines.append(f"\n## {svc.name}")
        for sub in svc.sub_services:
            price = sub.price_label or (f"${sub.price}/mo" if sub.price else "pricing on request")
            tag = f" [{sub.tag}]" if sub.tag else ""
            lines.append(f"  • {sub.name}{tag} — {price}")
            if sub.description:
                lines.append(f"    {sub.description}")
            if sub.features:
                for f in sub.features:
                    lines.append(f"    - {f}")
        if svc.applicable_discounts:
            lines.append(f"  Discounts available:")
            for d in svc.applicable_discounts:
                val = f" ({d.value})" if d.value else ""
                cond = f" | Condition: {d.condition}" if d.condition else ""
                lines.append(f"    🏷 {d.label}{val}{cond}")
    return "\n".join(lines)


def _format_selection(selected: list[SubService], discounts: list[Discount]) -> str:
    """Format what the user selected into a clean text block."""
    if not selected:
        return "No items selected yet."
    lines = ["User has selected:"]
    for sub in selected:
        price = sub.price_label or (f"${sub.price}/mo" if sub.price else "")
        lines.append(f"  ✓ {sub.name} — {price}")
        if sub.description:
            lines.append(f"    {sub.description}")
        if sub.features:
            for f in sub.features:
                lines.append(f"    - {f}")
    if discounts:
        lines.append("\nApplicable offers/discounts:")
        for d in discounts:
            val = f" ({d.value})" if d.value else ""
            lines.append(f"  🏷 {d.label}{val}")
            if d.description:
                lines.append(f"    {d.description}")
    return "\n".join(lines)


def _format_context(context: Optional[dict]) -> str:
    if not context:
        return ""
    parts = []
    if context.get("user_age"):
        parts.append(f"User age: {context['user_age']}")
    if context.get("is_new_customer") is not None:
        parts.append(f"New customer: {context['is_new_customer']}")
    for k, v in context.items():
        if k not in ("user_age", "is_new_customer"):
            parts.append(f"{k}: {v}")
    return "Customer context: " + ", ".join(parts) if parts else ""


# ============================================================
#  Prompt builders
# ============================================================

SALESMAN_PERSONA = """You are Alex, a friendly and knowledgeable sales consultant for a telecom company.
Your personality:
- Warm, enthusiastic, and genuinely helpful — never pushy or robotic
- You use natural conversational language, not bullet-point lists
- You highlight real benefits, not just specs (e.g. "stream 4K on 5 devices at once" not just "1Gbps")
- You acknowledge the user's situation and tailor your suggestion to them
- You mention discounts and promos naturally when relevant
- You keep responses concise — 2 to 4 sentences max unless the user asks for detail
- Never make up prices or features not in the provided catalog
- Never say "I cannot" or "As an AI" — you are Alex, a sales consultant"""


def _build_catalog_prompt(request: SalesMessage) -> str:
    catalog_text = _format_catalog(request.catalog or [])
    context_text = _format_context(request.context)
    return f"""{SALESMAN_PERSONA}

CURRENT MODE: The customer is browsing and hasn't selected anything yet.
Your goal: understand their needs through friendly questions, then guide them toward the best option.
If they seem undecided, briefly highlight what makes each service stand out.
If they share a preference or use case, recommend specifically.

{context_text}

AVAILABLE SERVICES AND PRICING:
{catalog_text}

IMPORTANT: When you recommend something, mention the sub-service name exactly as listed above.
At the end of your response, on a NEW LINE, output ONLY this JSON (no explanation):
SUGGESTIONS:{{"ids": ["id1", "id2"]}}
If you have no specific recommendation yet, output SUGGESTIONS:{{"ids": []}}"""


def _build_selection_prompt(request: SalesMessage) -> str:
    selection_text = _format_selection(
        request.selected_services or [],
        request.selected_discounts or [],
    )
    context_text = _format_context(request.context)
    return f"""{SALESMAN_PERSONA}

CURRENT MODE: The customer has selected specific services.
Your goal: affirm their choice enthusiastically, highlight why it's a great fit,
mention any applicable discounts naturally, and optionally suggest a complementary add-on
if it genuinely makes sense (don't force it).

{context_text}

{selection_text}

IMPORTANT: Sound like a great salesperson — make them feel confident about their choice.
At the end of your response, on a NEW LINE, output ONLY this JSON (no explanation):
SUGGESTIONS:{{"ids": ["id1", "id2"]}}
Where ids are the sub_service IDs you're actively recommending or affirming."""


# ============================================================
#  Reply parser — extract message and suggested IDs
# ============================================================

def _parse_reply(raw: str) -> tuple[str, list[str]]:
    """
    Split the raw GPT reply into (message, suggested_ids).
    GPT is instructed to append SUGGESTIONS:{...} on its last line.
    """
    suggested_ids = []
    message = raw.strip()

    if "SUGGESTIONS:" in raw:
        parts = raw.rsplit("SUGGESTIONS:", 1)
        message = parts[0].strip()
        try:
            suggestions_raw = parts[1].strip()
            parsed = json.loads(suggestions_raw)
            suggested_ids = parsed.get("ids", [])
        except Exception:
            pass  # IDs are nice-to-have, not critical

    return message, suggested_ids


# ============================================================
#  Main entry point
# ============================================================

async def process_sales_chat(request: SalesMessage) -> SalesResponse:
    """
    Single-turn sales assistant.

    Args:
        request: SalesMessage with mode, catalog or selection, history, user message

    Returns:
        SalesResponse with AI reply and optionally highlighted sub_service IDs
    """
    # Build system prompt based on mode
    if request.mode == "selection" and request.selected_services:
        system_prompt = _build_selection_prompt(request)
    else:
        system_prompt = _build_catalog_prompt(request)

    # Build message list: history (capped) + current user message
    history = (request.history or [])[-CONTEXT_WINDOW:]
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    messages.append({"role": "user", "content": request.message})

    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}] + messages,
            max_tokens=400,
            temperature=0.75,   # slightly creative — sounds more natural
        )
        raw_reply = response.choices[0].message.content.strip()
        message, suggested_ids = _parse_reply(raw_reply)

        return SalesResponse(
            message=message,
            suggested_ids=suggested_ids,
        )

    except Exception as e:
        logger.error(f"SalesAssistant GPT call failed: {e}", exc_info=True)
        return SalesResponse(
            message="I'm having a little trouble right now — could you give me a moment and try again?",
            suggested_ids=[],
        )


# ============================================================
#  TTS helper
# ============================================================

async def _text_to_speech(text: str) -> Optional[str]:
    """Convert reply text to MP3 via OpenAI TTS, upload to S3, return presigned URL."""
    from app.utils.upload_to_bucket import upload_bytes_to_s3, generate_presigned_url
    try:
        response = await client.audio.speech.create(
            model=settings.OPENAI_TTS_MODEL,
            voice=settings.OPENAI_TTS_VOICE,
            input=text,
            response_format="mp3",
        )
        s3_key = f"audio/sales-tts/{uuid.uuid4()}.mp3"
        result = upload_bytes_to_s3(
            data=response.content,
            object_name=s3_key,
            content_type="audio/mpeg",
            public=False,
        )
        if result["success"]:
            return generate_presigned_url(s3_key, expires_in=300)
        return None
    except Exception as e:
        logger.error(f"Sales TTS failed: {e}", exc_info=True)
        return None


# ============================================================
#  Voice entry point
# ============================================================

async def process_sales_voice(audio_bytes: bytes, request: SalesMessage) -> SalesResponse:
    """
    Voice handler:
    1. Whisper transcription of audio
    2. Run through process_sales_chat with transcribed text
    3. TTS the reply → S3 → presigned URL

    The request object carries catalog/selection/history/context as usual —
    only the message field is overwritten with the Whisper transcript.
    """
    # 1. Transcribe
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
        transcript = transcription.strip() if isinstance(transcription, str) else str(transcription)
        logger.info(f"Sales Whisper transcript: {transcript[:120]}")
    except Exception as e:
        logger.error(f"Sales Whisper failed: {e}", exc_info=True)
        return SalesResponse(
            message="I couldn't catch that — could you try again or type your question?",
            suggested_ids=[],
        )

    # 2. Override message with transcript and run text pipeline
    request.message = transcript
    text_response = await process_sales_chat(request)

    # 3. TTS
    audio_url = await _text_to_speech(text_response.message)

    return SalesResponse(
        message=text_response.message,
        suggested_ids=text_response.suggested_ids,
        audio_url=audio_url,
    )