"""
helpAssistant.py
----------------
Core logic for the knowledge-base help assistant.

Pipeline per request:
  1. Embed user question → ChromaDB semantic search → top-K relevant chunks
  2. Build system prompt with retrieved chunks injected
  3. GPT-4o-mini call with conversation history
  4. (Voice only) OpenAI TTS → S3 → presigned URL

Knowledge ingestion (run once / on deploy):
  Call `ingest_knowledge_base()` at app startup via lifespan event.
  Reads all .md files from /app/knowledge/, chunks them, embeds + stores in ChromaDB.
  Skips files that are already ingested (idempotent).
"""

import re
import logging
import tempfile
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.utils import embedding_functions
from openai import AsyncOpenAI

from app.services.helpAssistant.helpAssistant_schema import (
    HelpMessage,
    HelpResponse,
    KnowledgeChunk,
)
from app.utils.upload_to_bucket import upload_bytes_to_s3, generate_presigned_url

logger = logging.getLogger(__name__)

# ============================================================
#  Config
# ============================================================

from app.core.config import settings

MODEL = settings.OPENAI_MODEL
OPENAI_API_KEY = settings.OPENAI_API_KEY

KNOWLEDGE_DIR = Path(settings.KNOWLEDGE_DIR)
CHROMA_PATH = settings.CHROMA_PATH
CHROMA_COLLECTION = settings.CHROMA_COLLECTION

# Chunking
CHUNK_SIZE = 400        # characters per chunk
CHUNK_OVERLAP = 80      # overlap between chunks to preserve context

# Retrieval
TOP_K = 4               # number of chunks to inject per query
MAX_HISTORY = 6         # history turns fed to GPT

client = AsyncOpenAI(api_key=OPENAI_API_KEY)


# ============================================================
#  ChromaDB client — singleton
# ============================================================

_chroma_client: Optional[chromadb.PersistentClient] = None
_collection = None


def _get_collection():
    """Return (or lazily create) the ChromaDB collection."""
    global _chroma_client, _collection
    if _collection is not None:
        return _collection

    _chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)

    # Use OpenAI embeddings for best semantic quality
    ef = embedding_functions.OpenAIEmbeddingFunction(
        api_key=OPENAI_API_KEY,
        model_name="text-embedding-3-small",
    )

    _collection = _chroma_client.get_or_create_collection(
        name=CHROMA_COLLECTION,
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )
    return _collection


# ============================================================
#  Chunker
# ============================================================

def _chunk_text(text: str, source: str) -> list[dict]:
    """
    Split markdown text into overlapping chunks.
    Tries to split on double newlines (paragraph boundaries) first,
    falls back to character-based splitting.

    Returns list of { id, document, metadata } ready for ChromaDB upsert.
    """
    # Normalize whitespace
    text = re.sub(r"\n{3,}", "\n\n", text.strip())

    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    chunk_index = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current) + len(para) <= CHUNK_SIZE:
            current = (current + "\n\n" + para).strip()
        else:
            if current:
                chunks.append(current)
                # Overlap: carry last CHUNK_OVERLAP chars into next chunk
                current = current[-CHUNK_OVERLAP:] + "\n\n" + para
            else:
                # Para itself is larger than chunk size — hard split
                for i in range(0, len(para), CHUNK_SIZE - CHUNK_OVERLAP):
                    chunks.append(para[i:i + CHUNK_SIZE])
                current = ""

    if current.strip():
        chunks.append(current.strip())

    result = []
    for i, chunk in enumerate(chunks):
        chunk_id = f"{source}::chunk_{i}"
        result.append({
            "id": chunk_id,
            "document": chunk,
            "metadata": {"source": source, "chunk_index": i},
        })

    return result


# ============================================================
#  Ingestion — run at startup
# ============================================================

def ingest_knowledge_base(force: bool = False) -> dict:
    """
    Read all .md files from KNOWLEDGE_DIR, chunk them, embed and store in ChromaDB.
    Idempotent: skips files already ingested unless force=True.

    Args:
        force: if True, re-ingests all files even if already present

    Returns:
        { "ingested": [...], "skipped": [...], "total_chunks": N }

    Call this from your FastAPI lifespan:
        @asynccontextmanager
        async def lifespan(app):
            ingest_knowledge_base()
            yield
    """
    collection = _get_collection()
    md_files = sorted(KNOWLEDGE_DIR.glob("*.md"))

    if not md_files:
        logger.warning(f"No .md files found in {KNOWLEDGE_DIR}")
        return {"ingested": [], "skipped": [], "total_chunks": 0}

    ingested = []
    skipped = []
    total_chunks = 0

    for md_path in md_files:
        source = md_path.name  # e.g. "faq.md"

        # Check if already ingested (look for any chunk from this file)
        if not force:
            existing = collection.get(where={"source": source}, limit=1)
            if existing["ids"]:
                logger.info(f"Skipping already-ingested: {source}")
                skipped.append(source)
                continue

        text = md_path.read_text(encoding="utf-8")
        chunks = _chunk_text(text, source)

        if not chunks:
            logger.warning(f"No chunks generated for {source}")
            continue

        # Upsert into ChromaDB (handles duplicates gracefully)
        collection.upsert(
            ids=[c["id"] for c in chunks],
            documents=[c["document"] for c in chunks],
            metadatas=[c["metadata"] for c in chunks],
        )

        logger.info(f"Ingested {source}: {len(chunks)} chunks")
        ingested.append(source)
        total_chunks += len(chunks)

    return {
        "ingested": ingested,
        "skipped": skipped,
        "total_chunks": total_chunks,
    }


# ============================================================
#  Retrieval
# ============================================================

def _retrieve_relevant_chunks(query: str, top_k: int = TOP_K) -> list[KnowledgeChunk]:
    """
    Embed the user query and retrieve top-K most relevant chunks from ChromaDB.
    Returns list of KnowledgeChunk sorted by relevance.
    """
    collection = _get_collection()

    try:
        results = collection.query(
            query_texts=[query],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )

        chunks = []
        for i, doc in enumerate(results["documents"][0]):
            meta = results["metadatas"][0][i]
            dist = results["distances"][0][i]
            chunks.append(KnowledgeChunk(
                chunk_id=results["ids"][0][i],
                source=meta.get("source", "unknown"),
                content=doc,
                score=dist,
            ))

        return chunks

    except Exception as e:
        logger.error(f"ChromaDB retrieval failed: {e}", exc_info=True)
        return []


# ============================================================
#  Prompt builder
# ============================================================

ASSISTANT_PERSONA = """You are a helpful, knowledgeable support assistant for this company.
Your personality:
- Friendly, clear, and concise — answer the question directly
- Ground every answer strictly in the provided knowledge base context below
- If the answer is not in the context, say: "I don't have that information right now, but you can reach our team at [contact details from context]."
- Never make up facts, prices, or policies not present in the context
- Keep answers to 2-4 sentences unless more detail is clearly needed
- Use natural language, not bullet points, unless listing steps or options
- If the user writes in another language, respond in that same language"""


def _build_system_prompt(chunks: list[KnowledgeChunk]) -> str:
    if not chunks:
        context_block = "No relevant knowledge base content found for this query."
    else:
        sections = []
        for chunk in chunks:
            sections.append(f"[Source: {chunk.source}]\n{chunk.content}")
        context_block = "\n\n---\n\n".join(sections)

    return f"""{ASSISTANT_PERSONA}

=== KNOWLEDGE BASE CONTEXT ===
{context_block}
=== END OF CONTEXT ===

Answer the user's question using only the context above."""


# ============================================================
#  TTS helper (reused pattern from hiringAssistant)
# ============================================================

async def _text_to_speech(text: str) -> Optional[str]:
    """Convert reply text to MP3 via OpenAI TTS, upload to S3, return presigned URL."""
    try:
        response = await client.audio.speech.create(
            model="tts-1",
            voice="alloy",
            input=text,
            response_format="mp3",
        )
        audio_bytes = response.content

        import uuid
        s3_key = f"audio/help-tts/{uuid.uuid4()}.mp3"
        result = upload_bytes_to_s3(
            data=audio_bytes,
            object_name=s3_key,
            content_type="audio/mpeg",
            public=False,
        )
        if result["success"]:
            return generate_presigned_url(s3_key, expires_in=300)
        return None
    except Exception as e:
        logger.error(f"TTS failed: {e}", exc_info=True)
        return None


# ============================================================
#  Main entry points
# ============================================================

async def process_help_chat(request: HelpMessage) -> HelpResponse:
    """
    Text chat: retrieve relevant knowledge chunks → GPT reply.
    """
    # 1. Retrieve relevant chunks
    chunks = _retrieve_relevant_chunks(request.message)
    sources = list(dict.fromkeys(c.source for c in chunks))  # unique, order-preserving

    # 2. Build prompt and message list
    system_prompt = _build_system_prompt(chunks)
    history = (request.history or [])[-MAX_HISTORY:]
    messages = [
        {"role": m["role"], "content": m["content"]}
        for m in history
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    messages.append({"role": "user", "content": request.message})

    # 3. GPT call
    try:
        response = await client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system_prompt}] + messages,
            max_tokens=500,
            temperature=0.3,   # low temp — factual, grounded answers
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"GPT call failed: {e}", exc_info=True)
        reply = "I'm having trouble right now. Please try again in a moment."
        sources = []

    return HelpResponse(message=reply, sources=sources)


async def process_help_voice(audio_bytes: bytes) -> HelpResponse:
    """
    Voice: Whisper transcription → same pipeline as text → TTS reply.
    """
    # 1. Transcribe
    try:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            with open(tmp.name, "rb") as f:
                transcription = await client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="text",
                )
        transcript = transcription.strip() if isinstance(transcription, str) else str(transcription)
        logger.info(f"Whisper transcript (help): {transcript[:120]}")
    except Exception as e:
        logger.error(f"Whisper transcription failed: {e}", exc_info=True)
        return HelpResponse(
            message="I couldn't catch that. Could you try speaking again or type your question?",
            sources=[],
        )

    # 2. Run through text pipeline (no history on voice — single turn)
    text_response = await process_help_chat(HelpMessage(message=transcript, history=[]))

    # 3. TTS
    audio_url = await _text_to_speech(text_response.message)

    return HelpResponse(
        message=text_response.message,
        sources=text_response.sources,
        audio_url=audio_url,
    )