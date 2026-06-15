"""
cache_manager.py  —  merged file
---------------------------------
Keeps the original SessionCacheManager untouched (used by chat / other services).
Adds HiringSessionCache specifically for the hiringAssistant flow.
"""

import os
import redis
import json
from typing import List, Optional, Dict, Any, Callable, Awaitable
from datetime import datetime
from dotenv import load_dotenv

# Only import HistoryItem when the chat service schema is available
try:
    from app.services.chat.chatbot_schema import HistoryItem
except ImportError:
    HistoryItem = None  # graceful fallback during isolated testing

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_DB = int(os.getenv("REDIS_DB", 0))
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", 24))
CHAT_CONTEXT_WINDOW = 15
MAX_REDIS_HISTORY = 30
TEMP_ANON_TTL_SECONDS = 24 * 3600
TEMP_ANON_MAX_MESSAGES = 5
TEMP_ANON_HISTORY_KEEP = 4

# Hiring assistant session TTL — 2 hours (sessions auto-expire if abandoned)
HIRING_SESSION_TTL_SECONDS = 2 * 3600


# ============================================================
#  Original SessionCacheManager — unchanged
# ============================================================

class SessionCacheManager:
    def __init__(self):
        try:
            self.redis_client = redis.from_url(REDIS_URL, db=REDIS_DB)
            self.redis_client.ping()
        except Exception as e:
            print(f"Redis connection failed: {e}. Cache will be disabled.")
            self.redis_client = None

    def _get_history_key(self, chat_id: str) -> str:
        return f"chat_session:{chat_id}:history"

    def _get_archive_summary_key(self, chat_id: str) -> str:
        return f"chat_session:{chat_id}:archive_summary"

    def _get_context_summary_key(self, chat_id: str) -> str:
        return f"chat_session:{chat_id}:summary"

    def _get_temp_history_key(self, user_id: str) -> str:
        return f"temp_chat_session:{user_id}:history"

    def _get_temp_count_key(self, user_id: str) -> str:
        return f"temp_chat_session:{user_id}:count"

    def _set_with_ttl(self, key: str, value: str):
        ttl_seconds = CACHE_TTL_HOURS * 3600
        self.redis_client.setex(key, ttl_seconds, value)

    def get_history(self, chat_id: str):
        if not self.redis_client or not chat_id:
            return None
        try:
            cached_data = self.redis_client.get(self._get_history_key(chat_id))
            if cached_data:
                if HistoryItem:
                    return [HistoryItem(**item) for item in json.loads(cached_data)]
                return json.loads(cached_data)
            return []
        except Exception as e:
            print(f"Error retrieving cache for chat {chat_id}: {e}")
            return []

    def get_recent_history(self, chat_id: str):
        history = self.get_history(chat_id) or []
        return history[-CHAT_CONTEXT_WINDOW:]

    def get_history_page(self, chat_id: str, page: int = 1, page_size: int = 20):
        history = self.get_history(chat_id) or []
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 1
        start = (page - 1) * page_size
        end = start + page_size
        return history[start:end]

    def get_archive_summary(self, chat_id: str) -> str:
        if not self.redis_client or not chat_id:
            return ""
        try:
            value = self.redis_client.get(self._get_archive_summary_key(chat_id))
            return value.decode("utf-8") if isinstance(value, bytes) else (value or "")
        except Exception as e:
            print(f"Error retrieving archive summary for chat {chat_id}: {e}")
            return ""

    def get_context_summary(self, chat_id: str) -> str:
        if not self.redis_client or not chat_id:
            return ""
        try:
            value = self.redis_client.get(self._get_context_summary_key(chat_id))
            return value.decode("utf-8") if isinstance(value, bytes) else (value or "")
        except Exception as e:
            print(f"Error retrieving context summary for chat {chat_id}: {e}")
            return ""

    def get_context_for_ai(self, chat_id: str) -> Dict[str, Any]:
        history = self.get_recent_history(chat_id)
        summary = self.get_context_summary(chat_id)
        return {"history": history, "summary": summary}

    async def update_history_and_summary(
        self,
        chat_id: str,
        new_message: str,
        new_response: str,
        summarize_fn: Callable[[list, str], Awaitable[str]],
    ):
        if not self.redis_client or not chat_id:
            return
        try:
            history = self.get_history(chat_id) or []
            if HistoryItem:
                history.append(
                    HistoryItem(
                        message=new_message,
                        response=new_response,
                        timestamp=datetime.utcnow().isoformat(),
                    )
                )
            archive_summary = self.get_archive_summary(chat_id)
            if len(history) > MAX_REDIS_HISTORY:
                dropped_messages = history[:-MAX_REDIS_HISTORY]
                history = history[-MAX_REDIS_HISTORY:]
                archive_summary = await summarize_fn(dropped_messages, archive_summary)
            older_than_recent = history[:-CHAT_CONTEXT_WINDOW]
            if older_than_recent:
                context_summary = await summarize_fn(older_than_recent, archive_summary)
            else:
                context_summary = archive_summary
            self._set_with_ttl(
                self._get_history_key(chat_id),
                json.dumps([item.dict() for item in history]),
            )
            self._set_with_ttl(self._get_archive_summary_key(chat_id), archive_summary or "")
            self._set_with_ttl(self._get_context_summary_key(chat_id), context_summary or "")
        except Exception as e:
            print(f"Error updating cache for chat {chat_id}: {e}")

    def clear_session(self, chat_id: str):
        if not self.redis_client or not chat_id:
            return
        try:
            self.redis_client.delete(self._get_history_key(chat_id))
            self.redis_client.delete(self._get_archive_summary_key(chat_id))
            self.redis_client.delete(self._get_context_summary_key(chat_id))
        except Exception as e:
            print(f"Error clearing cache for chat {chat_id}: {e}")

    def get_temp_history(self, user_id: str):
        if not self.redis_client or not user_id:
            return []
        try:
            cached_data = self.redis_client.get(self._get_temp_history_key(user_id))
            if cached_data:
                if HistoryItem:
                    return [HistoryItem(**item) for item in json.loads(cached_data)]
                return json.loads(cached_data)
            return []
        except Exception as e:
            print(f"Error retrieving temporary history for user {user_id}: {e}")
            return []

    def get_temp_message_count(self, user_id: str) -> int:
        if not self.redis_client or not user_id:
            return 0
        try:
            value = self.redis_client.get(self._get_temp_count_key(user_id))
            if not value:
                return 0
            return int(value)
        except Exception as e:
            print(f"Error retrieving temporary message count for user {user_id}: {e}")
            return 0

    def can_send_temp_message(self, user_id: str) -> bool:
        return self.get_temp_message_count(user_id) < TEMP_ANON_MAX_MESSAGES

    def update_temp_history(self, user_id: str, new_message: str, new_response: str):
        if not self.redis_client or not user_id:
            return
        try:
            history = self.get_temp_history(user_id)
            if HistoryItem:
                history.append(
                    HistoryItem(
                        message=new_message,
                        response=new_response,
                        timestamp=datetime.utcnow().isoformat(),
                    )
                )
            history = history[-TEMP_ANON_HISTORY_KEEP:]
            count = self.get_temp_message_count(user_id) + 1
            ttl_seconds = TEMP_ANON_TTL_SECONDS
            self.redis_client.setex(
                self._get_temp_history_key(user_id),
                ttl_seconds,
                json.dumps([item.dict() for item in history]),
            )
            self.redis_client.setex(
                self._get_temp_count_key(user_id),
                ttl_seconds,
                str(count),
            )
        except Exception as e:
            print(f"Error updating temporary cache for user {user_id}: {e}")


# ============================================================
#  HiringSessionCache — dedicated to hiringAssistant flow
# ============================================================

class HiringSessionCache:
    """
    Manages Redis state for the hiring assistant interview flow.

    Key schema:  hiring_session:{session_id}
    Value:       JSON blob of HiringSession (see hiringAssistant_schema.py)
    TTL:         2 hours, refreshed on every write
    """

    SESSION_KEY_PREFIX = "hiring_session"

    def __init__(self):
        try:
            self.redis_client = redis.from_url(REDIS_URL, db=REDIS_DB)
            self.redis_client.ping()
            self._connected = True
        except Exception as e:
            print(f"HiringSessionCache: Redis connection failed: {e}")
            self.redis_client = None
            self._connected = False

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    def _key(self, session_id: str) -> str:
        return f"{self.SESSION_KEY_PREFIX}:{session_id}"

    # ------------------------------------------------------------------
    # Core CRUD
    # ------------------------------------------------------------------

    def get_session(self, session_id: str) -> Optional[dict]:
        """Return the full session dict, or None if not found."""
        if not self._connected:
            return None
        try:
            raw = self.redis_client.get(self._key(session_id))
            if raw:
                return json.loads(raw)
            return None
        except Exception as e:
            print(f"HiringSessionCache.get_session error [{session_id}]: {e}")
            return None

    def save_session(self, session_id: str, session_dict: dict) -> bool:
        """
        Persist (overwrite) the session dict and refresh the TTL.
        Always call this after mutating the session object.
        """
        if not self._connected:
            return False
        try:
            self.redis_client.setex(
                self._key(session_id),
                HIRING_SESSION_TTL_SECONDS,
                json.dumps(session_dict),
            )
            return True
        except Exception as e:
            print(f"HiringSessionCache.save_session error [{session_id}]: {e}")
            return False

    def delete_session(self, session_id: str) -> bool:
        """
        Hard-delete the session from Redis immediately.
        Call this after the final payload is sent to the frontend.
        """
        if not self._connected:
            return False
        try:
            self.redis_client.delete(self._key(session_id))
            return True
        except Exception as e:
            print(f"HiringSessionCache.delete_session error [{session_id}]: {e}")
            return False

    def session_exists(self, session_id: str) -> bool:
        if not self._connected:
            return False
        try:
            return bool(self.redis_client.exists(self._key(session_id)))
        except Exception as e:
            print(f"HiringSessionCache.session_exists error [{session_id}]: {e}")
            return False

    # ------------------------------------------------------------------
    # Convenience: atomic field-level helpers
    # ------------------------------------------------------------------

    def get_step(self, session_id: str) -> Optional[str]:
        session = self.get_session(session_id)
        return session.get("step") if session else None

    def append_message(self, session_id: str, role: str, content: str) -> bool:
        """Append one message turn and re-save.  role = 'user' | 'assistant'."""
        session = self.get_session(session_id)
        if session is None:
            return False
        session.setdefault("messages", []).append({"role": role, "content": content})
        return self.save_session(session_id, session)

    def get_messages(self, session_id: str) -> List[dict]:
        """Return the full messages list for building OpenAI chat history."""
        session = self.get_session(session_id)
        return session.get("messages", []) if session else []

    def get_current_question(self, session_id: str) -> Optional[str]:
        """Return the next unanswered question, or None if all answered."""
        session = self.get_session(session_id)
        if not session:
            return None
        questions = session.get("questions", [])
        idx = session.get("current_question_index", 0)
        if idx < len(questions):
            return questions[idx]
        return None

    def record_answer_and_score(
        self, session_id: str, answer: str, score: float
    ) -> bool:
        """
        Append the candidate's answer + internal score, advance the question pointer.
        Returns True if more questions remain, False if interview is complete.
        """
        session = self.get_session(session_id)
        if not session:
            return False
        session.setdefault("answers", []).append(answer)
        session.setdefault("scores", []).append(score)
        session["current_question_index"] = session.get("current_question_index", 0) + 1
        self.save_session(session_id, session)
        remaining = len(session.get("questions", [])) - session["current_question_index"]
        return remaining > 0

    def all_questions_answered(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        if not session:
            return False
        return session.get("current_question_index", 0) >= len(session.get("questions", []))

    def register_tts_key(self, session_id: str, s3_key: str) -> bool:
        """Track an S3 TTS audio key so we can batch-delete it on session close."""
        session = self.get_session(session_id)
        if not session:
            return False
        session.setdefault("tts_turns", []).append(s3_key)
        return self.save_session(session_id, session)

    def get_tts_keys(self, session_id: str) -> List[str]:
        """Return all S3 keys for TTS audio that belong to this session."""
        session = self.get_session(session_id)
        return session.get("tts_turns", []) if session else []

    # ------------------------------------------------------------------
    # Debug helpers (used by /hiring/debug/session/{session_id})
    # ------------------------------------------------------------------

    def raw_session_for_debug(self, session_id: str) -> dict:
        """
        Returns the full session blob as a plain dict.
        Scores are included here — this endpoint must be protected in production.
        """
        session = self.get_session(session_id)
        if not session:
            return {}
        # Annotate for readability in Swagger/Postman
        questions = session.get("questions", [])
        idx = session.get("current_question_index", 0)
        session["_debug"] = {
            "total_questions": len(questions),
            "answered": idx,
            "remaining": max(0, len(questions) - idx),
            "next_question": questions[idx] if idx < len(questions) else None,
            "tts_audio_files_stored": len(session.get("tts_turns", [])),
        }
        return session


# ============================================================
#  Global singletons
# ============================================================

cache_manager = SessionCacheManager()
hiring_cache = HiringSessionCache()