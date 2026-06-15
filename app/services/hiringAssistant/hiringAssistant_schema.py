from pydantic import BaseModel, EmailStr, field_validator
from typing import Optional, List, Literal
from enum import Enum


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ExperienceLevel(str, Enum):
    fresher = "fresher"        # 0-1 years
    junior = "junior"          # 1-3 years
    mid = "mid"                # 3-7 years
    senior = "senior"          # 8-11 years
    lead = "lead"              # 12+ years


class ScreeningDuration(int, Enum):
    five = 5
    ten = 10
    fifteen = 15


class SessionStep(str, Enum):
    role_info = "role_info"
    basic_info = "basic_info"
    otp_pending = "otp_pending"
    otp_verified = "otp_verified"
    screening_choice = "screening_choice"
    interview = "interview"
    resume_upload = "resume_upload"
    complete = "complete"


# ---------------------------------------------------------------------------
# Candidate data (built up progressively in Redis)
# ---------------------------------------------------------------------------

class CandidateData(BaseModel):
    # Step 1 — role info
    role: Optional[str] = None
    experience_level: Optional[ExperienceLevel] = None
    primary_skills: Optional[List[str]] = None
    secondary_skills: Optional[List[str]] = None
    years_of_experience: Optional[float] = None
    location_city: Optional[str] = None
    location_country: Optional[str] = None

    # Step 2 — basic info
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None

    # Step 4 — interview
    screening_duration: Optional[int] = None   # 5 / 10 / 15 or None (skipped)
    interview_score: Optional[float] = None    # stored internally, never sent to candidate
    interview_summary: Optional[str] = None

    # Step 5 — resume
    resume_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Redis session blob
# ---------------------------------------------------------------------------

class MessageItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str                               # always stored as text (voice transcribed)


class HiringSession(BaseModel):
    session_id: str
    step: SessionStep = SessionStep.role_info
    job_details: Optional[dict] = None        # sent by frontend on first message
    candidate: CandidateData = CandidateData()
    messages: List[MessageItem] = []          # full conversation history
    questions: List[str] = []                 # generated interview questions queue
    answers: List[str] = []                   # candidate answers, index-aligned with questions
    scores: List[float] = []                  # per-question scores, index-aligned
    current_question_index: int = 0
    otp: Optional[str] = None
    otp_expires: Optional[float] = None       # unix timestamp
    tts_turns: List[str] = []                 # S3 keys of generated TTS audio, for cleanup


# ---------------------------------------------------------------------------
# API request models
# ---------------------------------------------------------------------------

class ChatTextRequest(BaseModel):
    session_id: str
    message: str
    job_details: Optional[dict] = None        # only required on very first message


class OTPVerifyRequest(BaseModel):
    session_id: str
    code: str


# ---------------------------------------------------------------------------
# API response models
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    none = "none"
    send_otp = "send_otp"                  # frontend should call /hiring/otp/send
    verify_otp = "verify_otp"              # frontend should call /hiring/otp/verify with the code
    show_file_input = "show_file_input"    # frontend renders clickable upload block
    show_card = "show_card"               # frontend renders the screening options card
    close_session = "close_session"        # session done, payload attached


class FinalCandidatePayload(BaseModel):
    """Sent to frontend on close_session so it can forward to your other backend."""
    session_id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    role: str
    experience_level: str
    years_of_experience: float
    primary_skills: List[str]
    secondary_skills: List[str]
    location_city: str
    location_country: str
    screening_duration: Optional[int]      # None = skipped
    resume_url: str
    interview_summary: Optional[str]
    # NOTE: interview_score is intentionally excluded — never sent to candidate/frontend


class ChatResponse(BaseModel):
    message: str                           # AI text reply (always present)
    audio_url: Optional[str] = None        # presigned S3 URL for TTS mp3 (voice input only)
    action: ActionType = ActionType.none
    payload: Optional[FinalCandidatePayload] = None


class OTPSendResponse(BaseModel):
    success: bool
    message: str


class ResumeUploadResponse(BaseModel):
    success: bool
    message: str
    resume_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Debug response (Swagger / Postman inspection)
# ---------------------------------------------------------------------------

class DebugSessionResponse(BaseModel):
    session_id: str
    session: Optional[dict] = None
    exists: bool