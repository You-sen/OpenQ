from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # --- Existing chat services (Groq / Llama) ---
    GROQ_API_KEY: str = ""
    MODEL: str = "llama-3.1-8b-instant"

    # --- OpenAI (hiringAssistant, helpAssistant, salesAssistant) ---
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"          # used by hiring / help / sales assistants
    OPENAI_TTS_MODEL: str = "tts-1"
    OPENAI_TTS_VOICE: str = "nova"             # nova = warm, professional
    OPENAI_WHISPER_MODEL: str = "whisper-1"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"

    # --- OTP Backend ---
    OTP_CHECKER: bool = True
    OTP_BACKEND_SEND_URL: str = ""
    OTP_BACKEND_VERIFY_URL: str = ""
    OTP_BACKEND_TIMEOUT: int = 10
    # --- Redis ---
    REDIS_URL: str = "redis://redis:6379"
    REDIS_DB: int = 0
    CACHE_TTL_HOURS: int = 24

    # --- AWS S3 ---
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "eu-north-1"
    S3_BUCKET_NAME: str = "mycvconnect"
    S3_ENDPOINT: Optional[str] = None

    # --- ChromaDB ---
    CHROMA_PATH: str = "/app/chroma_db"
    KNOWLEDGE_DIR: str = "/app/knowledge"
    CHROMA_COLLECTION: str = "help_knowledge"

    # --- App ---
    ENVIRONMENT: str = "development"           # set to "production" to disable debug endpoints

    KNOWLEDGE_DIR: str = "/app/app/knowledge"
    
    class Config:
        env_file = ".env"
        extra = "ignore"                       # ignore unknown env vars silently


settings = Settings()