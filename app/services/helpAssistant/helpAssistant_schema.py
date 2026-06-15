"""
helpAssistant_schema.py
-----------------------
Pydantic models for the help assistant.

Knowledge is stored in ChromaDB (chunked .md files).
Per-turn: semantic search retrieves only relevant chunks → injected into system prompt.
Conversation memory: frontend sends last N turns (same pattern as salesAssistant).
Voice: same dual-endpoint pattern as hiringAssistant (Whisper in → TTS out).
"""

from pydantic import BaseModel
from typing import Optional, List


# ============================================================
#  Request models
# ============================================================

class HelpMessage(BaseModel):
    """
    Sent on every chat turn (text endpoint).

    message:  The user's question.
    history:  Frontend maintains and sends last N turns for context.
              Format: [{"role": "user"|"assistant", "content": "..."}]
    language: Optional hint — AI will respond in the same language as the user
              by default, but frontend can force a language if needed.
    """
    message: str
    history: Optional[List[dict]] = []     # last 6 turns max recommended
    language: Optional[str] = None         # e.g. "en", "bn" — optional override


# ============================================================
#  Response models
# ============================================================

class HelpResponse(BaseModel):
    """
    Returned on every text turn.

    message:        AI reply grounded in knowledge base.
    sources:        Which .md files were used (e.g. ["faq.md", "services.md"]).
                    Useful for frontend to show "Source: FAQ" badges if needed.
    audio_url:      Presigned S3 URL for TTS mp3 — only present on voice endpoint.
    """
    message: str
    sources: Optional[List[str]] = []
    audio_url: Optional[str] = None


# ============================================================
#  Internal: chunk stored in ChromaDB
# ============================================================

class KnowledgeChunk(BaseModel):
    """
    Represents one chunk as retrieved from ChromaDB.
    Not exposed in API — used internally in the retrieval pipeline.
    """
    chunk_id: str       # e.g. "faq.md::chunk_3"
    source: str         # e.g. "faq.md"
    content: str        # raw text of the chunk
    score: float        # cosine similarity score (lower = more similar in ChromaDB)