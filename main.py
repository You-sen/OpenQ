import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
# from app.services.chat.chatbot_route import router as chat_router
# from app.services.chat_without_login.chatbot_without_login_route import router as temp_chat_router
from app.services.hiringAssistant.hiringAssistant_router import router as hiring_router
from app.services.salesAssistant.salesAssistant_route import router as sales_router
from app.services.helpAssistant.helpAssistant_route import router as help_router
from app.services.helpAssistant.helpAssistant import ingest_knowledge_base

load_dotenv()



@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once on startup — ingests .md knowledge files into ChromaDB."""
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Starting up: ingesting knowledge base...")
    try:
        result = ingest_knowledge_base()
        logger.info(
            f"Knowledge base ready — "
            f"ingested: {result['ingested']}, "
            f"skipped: {result['skipped']}, "
            f"total chunks: {result['total_chunks']}"
        )
    except Exception as e:
        logger.error(f"Knowledge base ingestion failed on startup: {e}")
    yield
    # (shutdown logic here if needed)

app = FastAPI(
    title="openQ",
    description="AI-powered chatbot service with conversation memory",
    version="1.0.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
# app.include_router(chat_router)
# app.include_router(temp_chat_router)
app.include_router(hiring_router)
app.include_router(sales_router)
app.include_router(help_router)

@app.get("/")
async def root():
    return {
        "message": "Welcome to openQ",
        "endpoints": {
            # "chat": "/api/chat",
            # "temp_chat": "/api/chatbot-temp",
            "hiring": "/hiring/chat/text",
            "sales": "/sales/chat",
            "help": "/help/chat",
            "docs": "/docs"
        }
    }

@app.get("/health")
async def health_check():
    return JSONResponse(
        status_code=200,
        content={"status": "healthy", "service": "openQ"}
    )

# Error handlers
@app.exception_handler(404)
async def not_found(request, exc):
    return JSONResponse(
        status_code=404,
        content={"error": "Endpoint not found"}
    )

@app.exception_handler(500)
async def internal_error(request, exc):
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"}
    )

if __name__ == "__main__":
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=8085, 
        reload=True
    )