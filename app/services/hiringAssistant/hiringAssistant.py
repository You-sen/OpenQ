"""
hiringAssistant.py
------------------
Core AI pipeline for the hiring assistant.

Responsibilities:
  - State machine: decides what step we're in and what to do next
  - Prompt builder: constructs the right system prompt per step
  - GPT-4o calls: one model, different prompts per state
  - Scoring: evaluates interview answers internally (never exposed to candidate)
  - TTS: converts AI text response to audio when requested
  - Session orchestration: reads/writes Redis via HiringSessionCache
"""

import os
import json
import uuid
import time
import logging
import tempfile
import re
from typing import Optional, Tuple

from openai import AsyncOpenAI

from app.core.config import settings
from app.utils.cache_manager import hiring_cache
from app.utils.upload_to_bucket import (
    upload_bytes_to_s3,
    upload_file_object_to_s3,
    generate_presigned_url,
    delete_s3_keys_batch,
    delete_s3_prefix,
)
from app.services.hiringAssistant.hiringAssistant_schema import (
    ActionType,
    ChatResponse,
    FinalCandidatePayload,
    HiringSession,
    SessionStep,
)

logger = logging.getLogger(__name__)

client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
MODEL = settings.OPENAI_MODEL
TTS_MODEL = settings.OPENAI_TTS_MODEL
TTS_VOICE = settings.OPENAI_TTS_VOICE
WHISPER_MODEL = settings.OPENAI_WHISPER_MODEL

# How many messages from history we feed back to GPT for context
CONTEXT_WINDOW = 12

# Question counts per duration
QUESTION_COUNT = {5: 4, 10: 7, 15: 10}

# Scoring rubric sent to GPT internally
SCORING_RUBRIC = """
Score the candidate's answer on a scale of 0–10 based on:
- Technical accuracy (0–4 pts): Is the answer factually correct and complete?
- Clarity (0–2 pts): Is the answer well-structured and easy to understand?
- Depth (0–2 pts): Does the candidate demonstrate deeper understanding or give examples?
- Relevance (0–2 pts): Does the answer directly address the question?

Return ONLY a JSON object: {"score": <number 0-10>, "rationale": "<one sentence>"}
Do not include any other text.
"""


# ============================================================
#  Session factory
# ============================================================

def create_new_session(session_id: str, job_details: Optional[dict]) -> dict:
    """Create a fresh HiringSession dict and persist it to Redis."""
    session = HiringSession(
        session_id=session_id,
        job_details=job_details or {},
    ).model_dump()
    hiring_cache.save_session(session_id, session)
    return session


# ============================================================
#  Prompt builders
# ============================================================

def _job_context(session: dict) -> str:
    jd = session.get("job_details") or {}
    if not jd:
        return ""
    return f"\nJob context provided by employer: {json.dumps(jd, indent=2)}\n"


def _candidate_summary(session: dict) -> str:
    c = session.get("candidate", {})
    lines = []
    expected_keys = [
        "role", "experience_level", "primary_skills", "secondary_skills",
        "years_of_experience", "location_city", "location_country",
        "first_name", "last_name", "phone", "email"
    ]
    for k in expected_keys:
        v = c.get(k)
        lines.append(f"  {k}: {v if v is not None else 'not yet collected'}")
    return "\n".join(lines) if lines else "  (not yet collected)"


def _extract_candidate_fields_local(session: dict, conversation_so_far: list) -> dict:
    """
    Deterministically extract obvious candidate fields from recent user messages.
    This keeps the flow moving when GPT misses short answers like names, email, or phone.
    """
    candidate = dict(session.get("candidate", {}))

    for item in conversation_so_far[-10:]:
        if item.get("role") != "user":
            continue

        message = (item.get("content") or "").strip()
        if not message:
            continue

        # Experience level
        if not candidate.get("experience_level"):
            level_map = {
                "fresher": "fresher",
                "junior": "junior",
                "mid": "mid",
                "senior": "senior",
                "lead": "lead",
            }
            lowered = message.lower()
            for key, value in level_map.items():
                if re.search(rf"\b{key}\b", lowered):
                    candidate["experience_level"] = value
                    break

        # Years of experience
        if candidate.get("years_of_experience") is None:
            years_match = re.fullmatch(r"\d+(?:\.\d+)?", message)
            if years_match:
                candidate["years_of_experience"] = float(message)

        # Email
        if not candidate.get("email"):
            email_match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", message)
            if email_match:
                candidate["email"] = email_match.group(0)

        # Phone number
        if not candidate.get("phone"):
            phone_digits = re.sub(r"\D", "", message)
            if 7 <= len(phone_digits) <= 15 and any(char.isdigit() for char in message):
                candidate["phone"] = message

        # Name extraction — only during basic_info step
        if session.get("step") == "basic_info":
            words = message.split()
            if (
                not candidate.get("first_name")
                and len(words) >= 1
                and len(words) <= 3
                and all(w.isalpha() for w in words)
            ):
                candidate["first_name"] = words[0].title()
                if len(words) >= 2:
                    candidate["last_name"] = words[-1].title()
            elif (
                candidate.get("first_name")
                and not candidate.get("last_name")
                and len(words) == 1
                and words[0].isalpha()
            ):
                candidate["last_name"] = words[0].title()

        # Skills - removed aggressive local extraction to let GPT handle it reliably
        pass

    return candidate


def backfill_candidate_fields_from_history(session: dict) -> dict:
    """
    Re-run the local extractor against the stored message history and merge any missing fields.
    Useful for recovery paths like /otp/send when the session still has stale candidate data.
    """
    messages = session.get("messages", [])
    updated_candidate = _extract_candidate_fields_local(session, messages)
    session["candidate"] = updated_candidate
    return session


def _build_system_prompt(session: dict) -> str:
    step = session.get("step")
    jd_ctx = _job_context(session)
    candidate_ctx = _candidate_summary(session)

    base = (
        "You are an AI hiring assistant for a professional recruitment platform. "
        "Your tone is warm, professional, and encouraging. "
        "You guide candidates step by step through a structured onboarding and interview process. "
        "Never skip a step. Never reveal internal scores or evaluation criteria to the candidate. "
        "Never ask for more than one piece of information per message unless explicitly grouped.\n"
        f"{jd_ctx}"
        f"\nCandidate data collected so far:\n{candidate_ctx}\n"
    )

    step_instructions = {
        SessionStep.role_info: (
            "CURRENT STEP: Collect role and skills information.\n"
            "You must collect in order: role applying for, experience level "
            "(0-1 yrs = Fresher, 1-3 = Junior, 3-7 = Mid, 8-11 = Senior, 12+ = Lead), "
            "primary skills (comma-separated list), secondary skills, "
            "total years of experience (number), current city, current country.\n"
            "Ask one or two fields at a time. Once all are collected, "
            "smoothly transition to collecting basic contact information."
        ),
        SessionStep.basic_info: (
            "CURRENT STEP: Collect basic contact details.\n"
            "You must collect in this exact order: first name, last name, phone number, email address.\n"
            "Do not ask for email before both first name and last name are collected.\n"
            "After collecting the email, tell the candidate you will send a verification code "
            "to their email and that they should enter it when ready. "
            "Do NOT send the OTP yourself — the backend handles that."
        ),
        SessionStep.otp_pending: (
            "CURRENT STEP: Waiting for OTP verification.\n"
            "You have already told the candidate a 4-digit code was sent to their email. "
            "If the candidate provides a 4-digit number, treat it as their OTP attempt. "
            "If they ask to resend, acknowledge that and say the backend will resend it."
        ),
        SessionStep.otp_verified: (
            "CURRENT STEP: OTP verified. Transition to screening offer.\n"
            "Say something like: 'Excellent! Now for an optional technical screening. "
            "Candidates who complete screening are prioritised — the longer the screening, "
            "the higher your profile ranking. To proceed, type yes.'"
        ),
        SessionStep.screening_choice: (
            "CURRENT STEP: Candidate said yes to screening. Present the 4 options:\n"
            "1) 15-Minute Screening — In-depth technical assessment. Highest priority ranking.\n"
            "2) 10-Minute Screening — Balanced technical questions. Good priority ranking.\n"
            "3) 5-Minute Screening — Quick technical check-in. Standard priority ranking.\n"
            "4) Skip Screening — Submit profile without screening. Manual review time.\n"
            "After they choose, acknowledge their choice and say you are preparing their questions."
        ),
        SessionStep.interview: (
            "CURRENT STEP: Technical interview in progress.\n"
            "Ask interview questions ONE AT A TIME. Wait for the candidate's full answer before "
            "proceeding to the next question. Be encouraging but neutral — do not hint at "
            "whether an answer was correct or incorrect. Never reveal scores."
        ),
        SessionStep.resume_upload: (
            "CURRENT STEP: Interview complete. Ask the candidate to upload their resume.\n"
            "Say exactly: 'Screening complete! Excellent work. Now please upload your resume "
            "to strengthen your profile.' Nothing else — the frontend will show the upload widget."
        ),
        SessionStep.complete: (
            "CURRENT STEP: Session complete.\n"
            "Thank the candidate warmly. Tell them their profile has been submitted and the team "
            "will be in touch. Do not mention scores or internal evaluation."
        ),
    }

    instruction = step_instructions.get(step, "Guide the candidate through the hiring process.")
    return base + "\n" + instruction


# ============================================================
#  GPT call helpers
# ============================================================

async def _call_gpt(system_prompt: str, messages: list, max_tokens: int = 512) -> str:
    """Make a standard GPT-4o chat completion call."""
    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "system", "content": system_prompt}] + messages[-CONTEXT_WINDOW:],
        max_tokens=max_tokens,
        temperature=0.7,
    )
    return response.choices[0].message.content.strip()


async def _score_answer(question: str, answer: str, role: str, skills: list) -> Tuple[float, str]:
    """
    Internally score a candidate answer. Result is stored in Redis, never sent to frontend.
    Returns (score: float, rationale: str)
    """
    prompt = (
        f"You are evaluating a candidate interview answer.\n"
        f"Role: {role}\nKey skills: {', '.join(skills or [])}\n\n"
        f"Question: {question}\n"
        f"Candidate answer: {answer}\n\n"
        f"{SCORING_RUBRIC}"
    )
    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.2,
        )
        raw = response.choices[0].message.content.strip()
        parsed = json.loads(raw)
        return float(parsed.get("score", 5.0)), parsed.get("rationale", "")
    except Exception as e:
        logger.warning(f"Score parsing failed: {e}. Defaulting to 5.0")
        return 5.0, "Scoring unavailable"


async def _generate_questions(session: dict) -> list:
    """
    Generate all interview questions upfront based on role, skills, and duration.
    Stored in Redis as a queue — asked one by one during the interview step.
    """
    c = session.get("candidate", {})
    duration = c.get("screening_duration", 10)
    count = QUESTION_COUNT.get(duration, 7)
    role = c.get("role", "Software Engineer")
    level = c.get("experience_level", "mid")
    primary = c.get("primary_skills") or []
    secondary = c.get("secondary_skills") or []
    jd = session.get("job_details") or {}

    prompt = (
        f"Generate exactly {count} technical interview questions for a {level}-level {role}.\n"
        f"Primary skills to test: {', '.join(primary)}\n"
        f"Secondary skills to consider: {', '.join(secondary)}\n"
        f"Job details: {json.dumps(jd)}\n\n"
        "Requirements:\n"
        "- Questions should progress from foundational to advanced\n"
        "- Mix conceptual understanding, practical scenarios, and problem-solving\n"
        "- Each question should be answerable in 1-3 minutes verbally\n"
        "- Return ONLY a JSON array of question strings, no numbering, no extra text\n"
        'Example: ["What is ...?", "Explain how ...?", "How would you ...?"]'
    )
    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.6,
        )
        raw = response.choices[0].message.content.strip()
        questions = json.loads(raw)
        if isinstance(questions, list) and questions:
            return questions[:count]
    except Exception as e:
        logger.error(f"Question generation failed: {e}")
    # Fallback generic questions
    return [
        f"Tell me about your experience with {primary[0] if primary else 'your primary skill'}.",
        "Describe a challenging technical problem you solved recently.",
        "How do you approach debugging a production issue?",
        "What's your experience with code reviews and best practices?",
        "How do you stay up to date with new technologies?",
    ][:count]


# ============================================================
#  TTS helper
# ============================================================

async def _text_to_speech(text: str, session_id: str, turn: int) -> Optional[str]:
    """
    Convert AI text to audio using OpenAI TTS, upload to S3, return presigned URL.
    Also registers the S3 key in Redis so it can be cleaned up at session end.
    Returns presigned URL or None on failure.
    """
    try:
        response = await client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,       # warm, professional voice
            input=text,
            response_format="mp3",
        )
        audio_bytes = response.content
        s3_key = f"audio/tts/{session_id}_{turn}.mp3"
        result = upload_bytes_to_s3(
            data=audio_bytes,
            object_name=s3_key,
            content_type="audio/mpeg",
            public=False,           # private — accessed via presigned URL
        )
        if result["success"]:
            hiring_cache.register_tts_key(session_id, s3_key)
            presigned = generate_presigned_url(s3_key, expires_in=300)  # 5 min
            return presigned
        return None
    except Exception as e:
        logger.error(f"TTS failed for session {session_id} turn {turn}: {e}")
        return None


# ============================================================
#  State transition logic
# ============================================================

def _detect_candidate_fields(user_message: str, session: dict) -> dict:
    """
    Very lightweight field extractor used to detect when a step is complete.
    GPT handles the actual collection; this just checks if the session dict
    has been populated enough to advance the state machine.
    """
    return session.get("candidate", {})


async def _advance_state_if_needed(session: dict, user_message: str, ai_reply: str) -> dict:
    """
    After GPT replies, check if we should advance to the next step.
    The AI is instructed to transition naturally; this function enforces it in Redis.
    This is called AFTER saving the message exchange.
    """
    step = session.get("step")
    c = session.get("candidate", {})

    if step == SessionStep.role_info:
        # Advance when all role fields are present
        if all([
            c.get("role"), c.get("experience_level"), c.get("primary_skills"),
            c.get("years_of_experience") is not None, c.get("location_city"),
            c.get("location_country"),
        ]):
            session["step"] = SessionStep.basic_info

    elif step == SessionStep.basic_info:
        # Advance only when full basic info is collected.
        if c.get("email") and c.get("first_name") and c.get("last_name") and c.get("phone"):
            session["step"] = SessionStep.otp_pending

    elif step == SessionStep.otp_verified:
        # Advance to screening choice after "yes"
        if "yes" in user_message.lower():
            session["step"] = SessionStep.screening_choice

    elif step == SessionStep.interview:
        # Interview advancement is handled explicitly in process_interview_turn()
        pass

    return session


# ============================================================
#  Candidate data extractor (called after each GPT reply)
# ============================================================

async def extract_candidate_fields(session: dict, conversation_so_far: list) -> dict:
    """
    Ask GPT to extract structured candidate data from the conversation so far.
    Returns updated candidate dict. Called after role_info and basic_info steps.
    """
    c = session.get("candidate", {})
    local_candidate = _extract_candidate_fields_local(session, conversation_so_far)
    prompt = (
        "Extract candidate information from this conversation and return ONLY a JSON object "
        "with these exact fields (use null for fields not yet mentioned or confirmed):\n"
        '{"role": null, "experience_level": null, "primary_skills": [], "secondary_skills": [], '
        '"years_of_experience": null, "location_city": null, "location_country": null, '
        '"first_name": null, "last_name": null, "phone": null, "email": null}\n\n'
        "Rules:\n"
        "1. experience_level must be one of: fresher, junior, mid, senior, lead\n"
        "2. primary_skills and secondary_skills must be arrays of strings.\n"
        "3. Do NOT extract skills from the user stating their role (e.g. if they say 'backend' when asked for role, set role to 'backend' and leave skills as empty/null).\n"
        "4. Return ONLY the JSON object. No explanation.\n\n"
        f"Conversation:\n{json.dumps(conversation_so_far[-10:], indent=2)}"
    )
    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```json"):
            raw = raw[7:]
        elif raw.startswith("```"):
            raw = raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        extracted = json.loads(raw)
        # Merge: only update fields that are newly non-null
        for k, v in extracted.items():
            if v is not None and v != [] and k in c:
                c[k] = v
            elif k not in c:
                c[k] = v
        # Prefer deterministic local values for email, phone — but let GPT win for names
        for k, v in local_candidate.items():
            if k in ("first_name", "last_name"):
                # GPT is more reliable for names — only backfill if GPT missed it
                if v is not None and v != [] and not c.get(k):
                    c[k] = v
            elif v is not None and v != [] and not c.get(k):
                c[k] = v
        return c
    except Exception as e:
        logger.warning(f"Field extraction failed: {e}")
        return local_candidate


# ============================================================
#  Interview turn processor
# ============================================================

async def process_interview_turn(
    session: dict,
    user_answer: str,
) -> Tuple[str, bool]:
    """
    Handle one interview Q&A turn.
    Returns (ai_reply, interview_complete).
    """
    questions = session.get("questions", [])
    idx = session.get("current_question_index", 0)
    c = session.get("candidate", {})

    # Score the answer that just came in (for the question at idx)
    if idx < len(questions):
        question = questions[idx]
        score, rationale = await _score_answer(
            question=question,
            answer=user_answer,
            role=c.get("role", ""),
            skills=(c.get("primary_skills") or []) + (c.get("secondary_skills") or []),
        )
        more_questions = hiring_cache.record_answer_and_score(
            session["session_id"], user_answer, score
        )
        # Reload session after mutation
        session = hiring_cache.get_session(session["session_id"])
        idx = session.get("current_question_index", 0)

        if more_questions and idx < len(questions):
            next_q = questions[idx]
            reply = f"Thank you for your answer. Here's your next question:\n\n{next_q}"
            return reply, False
        else:
            # All questions answered
            return (
                "Screening complete! Excellent work. Now please upload your resume to strengthen your profile.",
                True,
            )
    else:
        return (
            "Screening complete! Excellent work. Now please upload your resume to strengthen your profile.",
            True,
        )


# ============================================================
#  Final payload builder
# ============================================================

def build_final_payload(session: dict) -> Optional[FinalCandidatePayload]:
    """Build the final candidate JSON payload to send to frontend (and onward to other backend)."""
    c = session.get("candidate", {})
    scores = session.get("scores", [])
    avg_score = round(sum(scores) / len(scores), 2) if scores else None

    # Store avg score back in session (internal only)
    if avg_score is not None:
        c["interview_score"] = avg_score
        session["candidate"] = c
        hiring_cache.save_session(session["session_id"], session)

    try:
        return FinalCandidatePayload(
            session_id=session["session_id"],
            first_name=c.get("first_name", ""),
            last_name=c.get("last_name", ""),
            email=c.get("email", ""),
            phone=c.get("phone", ""),
            role=c.get("role", ""),
            experience_level=c.get("experience_level", ""),
            years_of_experience=float(c.get("years_of_experience") or 0),
            primary_skills=c.get("primary_skills") or [],
            secondary_skills=c.get("secondary_skills") or [],
            location_city=c.get("location_city", ""),
            location_country=c.get("location_country", ""),
            screening_duration=c.get("screening_duration"),
            resume_url=c.get("resume_url", ""),
            interview_summary=c.get("interview_summary"),
            # interview_score intentionally omitted from payload
        )
    except Exception as e:
        logger.error(f"Failed to build final payload: {e}")
        return None


# ============================================================
#  Session cleanup
# ============================================================

async def cleanup_session(session_id: str):
    """
    Delete TTS audio from S3, then wipe the Redis session.
    Called after final payload is sent to frontend.
    """
    tts_keys = hiring_cache.get_tts_keys(session_id)
    if tts_keys:
        logger.info(f"Cleaning up {len(tts_keys)} TTS audio files for session {session_id}")
        delete_s3_keys_batch(tts_keys)
    else:
        # Fallback: delete by prefix in case Redis was cleared prematurely
        delete_s3_prefix(f"audio/tts/{session_id}_")

    hiring_cache.delete_session(session_id)
    logger.info(f"Session {session_id} fully wiped from Redis and S3 audio cleaned.")


# ============================================================
#  Main entry point
# ============================================================

async def process_chat(
    session_id: str,
    user_message: str,
    job_details: Optional[dict] = None,
    generate_audio: bool = False,
) -> ChatResponse:
    """
    Main entry point for both /chat/text and /chat/voice (after Whisper transcription).

    Args:
        session_id:     UUID identifying this candidate session
        user_message:   Text input (either typed or transcribed from voice)
        job_details:    Optional job context sent by frontend on first message
        generate_audio: If True, run TTS on the reply and return audio_url

    Returns:
        ChatResponse with message, optional audio_url, action, and optional payload
    """
    # ------------------------------------------------------------------
    # 1. Load or create session
    # ------------------------------------------------------------------
    session = hiring_cache.get_session(session_id)
    if session is None:
        session = create_new_session(session_id, job_details)

    # Update job_details if provided and not yet set
    if job_details and not session.get("job_details"):
        session["job_details"] = job_details
        hiring_cache.save_session(session_id, session)

    step = session.get("step")
    turn = len(session.get("messages", []))

    # ------------------------------------------------------------------
    # 2. Append user message to history
    # ------------------------------------------------------------------
    hiring_cache.append_message(session_id, "user", user_message)
    session = hiring_cache.get_session(session_id)

    # ------------------------------------------------------------------
    # 3. Step-specific routing
    # ------------------------------------------------------------------
    action = ActionType.none
    payload = None
    ai_reply = ""

    if step == SessionStep.interview:
        # Interview turns have dedicated logic
        ai_reply, interview_done = await process_interview_turn(session, user_message)
        if interview_done:
            session = hiring_cache.get_session(session_id)
            session["step"] = SessionStep.resume_upload
            hiring_cache.save_session(session_id, session)
            action = ActionType.show_file_input

    elif step == SessionStep.resume_upload:
        # Should not receive a chat message here normally — just remind
        ai_reply = "Please use the upload button above to submit your resume."

    elif step == SessionStep.complete:
        ai_reply = "Your profile has already been submitted. Our team will be in touch soon!"

    else:
        # Standard conversational steps — build system prompt and call GPT
        system_prompt = _build_system_prompt(session)
        messages = hiring_cache.get_messages(session_id)
        # Convert to OpenAI format
        openai_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
        ai_reply = await _call_gpt(system_prompt, openai_messages)

        # Extract candidate fields from conversation (runs on role_info and basic_info)
        if step in (SessionStep.role_info, SessionStep.basic_info):
            updated_candidate = await extract_candidate_fields(session, openai_messages)
            session = hiring_cache.get_session(session_id)
            session["candidate"] = updated_candidate
            hiring_cache.save_session(session_id, session)

        # Advance state if conditions are met
        session = hiring_cache.get_session(session_id)
        session = await _advance_state_if_needed(session, user_message, ai_reply)
        hiring_cache.save_session(session_id, session)

        # Post-advance: handle screening choice selection
        step = session.get("step")
        if step == SessionStep.otp_pending:
            action = ActionType.send_otp
            # If user typed a 4-digit code, signal frontend to call /otp/verify
            if re.fullmatch(r"\d{4}", user_message.strip()):
                action = ActionType.verify_otp
        if step == SessionStep.screening_choice:
            duration = _detect_screening_choice(user_message)
            if duration == 0:
                # Skip screening — go straight to resume upload
                session["step"] = SessionStep.resume_upload
                session["candidate"]["screening_duration"] = None
                hiring_cache.save_session(session_id, session)
                ai_reply += "\n\nGreat! Please go ahead and upload your resume."
                action = ActionType.show_file_input
            elif duration:
                # Generate questions and start interview
                session["candidate"]["screening_duration"] = duration
                questions = await _generate_questions(session)
                session["questions"] = questions
                session["current_question_index"] = 0
                session["step"] = SessionStep.interview
                hiring_cache.save_session(session_id, session)
                first_q = questions[0] if questions else "Tell me about yourself."
                ai_reply = (
                    f"Perfect! Let's begin your {duration}-minute screening. "
                    f"I'll ask you {QUESTION_COUNT.get(duration, 7)} questions. "
                    f"Take your time with each answer.\n\nQuestion 1: {first_q}"
                )
            else:
                # User hasn't chosen a valid option yet; show the choice card
                action = ActionType.show_card

    # ------------------------------------------------------------------
    # 4. Save AI reply to history
    # ------------------------------------------------------------------
    hiring_cache.append_message(session_id, "assistant", ai_reply)

    # ------------------------------------------------------------------
    # 5. Generate TTS audio if this was a voice request
    # ------------------------------------------------------------------
    audio_url = None
    if generate_audio:
        audio_url = await _text_to_speech(ai_reply, session_id, turn)

    # ------------------------------------------------------------------
    # 6. Return response
    # ------------------------------------------------------------------
    return ChatResponse(
        message=ai_reply,
        audio_url=audio_url,
        action=action,
        payload=payload,
    )


async def process_voice(
    session_id: str,
    audio_bytes: bytes,
    job_details: Optional[dict] = None,
) -> ChatResponse:
    """
    Voice endpoint handler.
    1. Transcribe audio via Whisper
    2. Run through process_chat with generate_audio=True
    """
    # Transcribe
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=True) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        with open(tmp.name, "rb") as f:
            transcription = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="text",
            )

    transcript_text = transcription.strip() if isinstance(transcription, str) else str(transcription)
    logger.info(f"Whisper transcript [{session_id}]: {transcript_text[:100]}")

    return await process_chat(
        session_id=session_id,
        user_message=transcript_text,
        job_details=job_details,
        generate_audio=True,
    )


async def finalize_resume(session_id: str, resume_url: str) -> ChatResponse:
    """
    Called after resume is successfully uploaded to S3.
    Builds final payload, advances to complete, triggers session cleanup.
    """
    session = hiring_cache.get_session(session_id)
    if not session:
        return ChatResponse(message="Session not found.", action=ActionType.none)

    session["candidate"]["resume_url"] = resume_url
    session["step"] = SessionStep.complete
    hiring_cache.save_session(session_id, session)

    # Generate a brief interview summary (stored internally)
    if session.get("questions") and session.get("answers"):
        try:
            qa_pairs = "\n".join([
                f"Q: {q}\nA: {a}"
                for q, a in zip(session["questions"], session["answers"])
            ])
            summary_prompt = (
                f"Summarize this candidate's interview performance in 2-3 sentences. "
                f"Be factual and professional. Do not include a score.\n\n{qa_pairs}"
            )
            summary_resp = await client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": summary_prompt}],
                max_tokens=200,
                temperature=0.3,
            )
            session["candidate"]["interview_summary"] = summary_resp.choices[0].message.content.strip()
            hiring_cache.save_session(session_id, session)
        except Exception as e:
            logger.warning(f"Summary generation failed: {e}")

    payload = build_final_payload(session)

    farewell = (
        "Thank you! Your profile has been successfully submitted. "
        "Our team will review your application and get back to you soon. "
        "Best of luck! 🎉"
    )

    # Cleanup: delete TTS audio from S3 and wipe Redis session
    await cleanup_session(session_id)

    return ChatResponse(
        message=farewell,
        audio_url=None,
        action=ActionType.close_session,
        payload=payload,
    )


# ============================================================
#  Helpers
# ============================================================

def _detect_screening_choice(message: str) -> Optional[int]:
    """
    Parse the candidate's screening duration choice from their message.
    Returns 5, 10, 15 for screening, 0 for skip, None if not yet a choice.
    """
    msg = message.lower().strip()
    if any(x in msg for x in ["15", "fifteen", "1)"]):
        return 15
    if any(x in msg for x in ["10", "ten", "2)"]):
        return 10
    if any(x in msg for x in ["5", "five", "3)"]):
        return 5
    if any(x in msg for x in ["skip", "4)", "without", "no screening"]):
        return 0
    return None