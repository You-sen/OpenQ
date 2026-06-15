# openQ-AI

openQ is an AI-powered chatbot backend built with FastAPI, integrating multiple specialized AI assistants for hiring, sales, and general help/knowledge-base tasks. It supports both text and voice interactions, semantic search, and stateful multi-step conversation flows.

## Detailed Workflow & Architecture

The application is split into three primary assistant services, each serving a distinct purpose:

1. **Hiring Assistant (`app/services/hiringAssistant`)**
   - **Type**: Stateful
   - **Storage**: Redis (for session management and caching)
   - **Workflow**: Manages a multi-step candidate onboarding process. The AI sequentially collects the candidate's name, email, background, and skills. Upon capturing the email, it triggers an OTP validation flow. Finally, it prompts for a resume upload (PDF), analyzes it, and produces a final consolidated candidate payload.

2. **Sales Assistant (`app/services/salesAssistant`)**
   - **Type**: Stateless
   - **Workflow**: Operates in "catalog" (exploration/suggestion) or "selection" (affirming benefits) modes. Since it is stateless, the frontend must pass the current conversation history, selected items, and user context on every request.

3. **Help Assistant (`app/services/helpAssistant`)**
   - **Type**: Stateless (RAG-based)
   - **Storage**: ChromaDB (Vector Database)
   - **Workflow**: Uses Retrieval-Augmented Generation (RAG). At application startup, it ingests `.md` files from `app/knowledge` into ChromaDB. It semantically searches these chunks to answer user queries strictly based on the provided documentation, avoiding hallucinations. Returns the sources used.

---

## Code Structure

```text
openQ-AI/
├── app/
│   ├── core/
│   │   └── config.py              # Pydantic settings loading from .env
│   ├── knowledge/                 # Markdown files for the Help Assistant RAG
│   ├── services/
│   │   ├── helpAssistant/         # RAG knowledge-base assistant logic and routes
│   │   ├── hiringAssistant/       # Stateful onboarding assistant logic and routes
│   │   └── salesAssistant/        # Stateless sales assistant logic and routes
│   └── utils/
│       ├── cache_manager.py       # Redis cache and session management
│       └── upload_to_bucket.py    # AWS S3 integration for resumes and TTS audio
├── chroma_db/                     # Local persistent storage for ChromaDB
├── .env                           # Environment variables
├── docker-compose.yml             # Docker compose for running App + Redis
├── main.py                        # FastAPI application entry point, lifecycle, and routers
├── readme.md                      # Documentation
└── requirements.txt               # Python dependencies
```

---

## Endpoints: When the Frontend Will Call What

### 1. Hiring Assistant (`/hiring/*`)
The frontend must generate and maintain a UUID `session_id` throughout the candidate's session.

*   `POST /hiring/chat/text`: Called on every text message sent by the candidate. On the very first message, the frontend should include `job_details` context. The response might contain `action: show_file_input` when the AI finishes its questions.
*   `POST /hiring/chat/voice`: Called when the candidate records and sends an audio message. Returns the transcription, AI reply, and an `audio_url` for the frontend to play.
*   `POST /hiring/otp/send`: Called by the frontend immediately after the AI successfully collects the candidate's email. Triggers an email OTP code.
*   `POST /hiring/otp/verify`: Called when the candidate submits the 4-digit code. Validates the code and allows the chat to proceed.
*   `POST /hiring/resume/upload`: Called when the frontend shows the file input (triggered by the `show_file_input` action). Accepts a PDF, analyzes it, and returns the final `action: close_session` along with the finalized candidate JSON payload.
*   `DELETE /hiring/audio/{session_id}`: **Crucial:** The frontend MUST call this when the chat widget is closed or the session concludes to clean up the temporary TTS audio files from S3.
*   `GET /hiring/debug/session/{id}`: *Development only.* Used to inspect the current Redis session state.

### 2. Sales Assistant (`/sales/*`)
Since this is stateless, the frontend is responsible for maintaining history and state.

*   `POST /sales/chat`: Called for text chat. The frontend sends `mode` (catalog/selection), `catalog`, `selected_services`, `history`, and `context`.
*   `POST /sales/voice`: Called for voice messages. Operates similarly to `/chat` but accepts multipart form data containing the audio file. Returns an `audio_url`.
*   `DELETE /sales/audio/{s3_key}`: **Crucial:** The frontend MUST collect S3 keys returned in voice responses and call this endpoint when the session ends to clean up TTS audio.

### 3. Help Assistant (`/help/*`)
*   `POST /help/chat`: Called for text questions. The frontend sends the user message and conversation `history`. Returns a textual response and a list of `sources` (e.g., `["services.md"]`).
*   `POST /help/voice`: Called for audio questions. Transcribes, answers, and returns an `audio_url`.
*   `DELETE /help/audio/{s3_key}`: **Crucial:** The frontend MUST clean up the generated TTS audio files using the returned S3 keys when the session ends.
*   `POST /help/ingest`: Manually re-trigger knowledge base ingestion if `.md` files are updated without restarting the server.

---

## How to Set Up Locally

1. **Clone the Repository and Navigate to the Directory**:
   ```bash
   git clone <repo-url>
   cd openQ-AI
   ```

2. **Set up a Virtual Environment**:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: .\venv\Scripts\activate
   ```

3. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Variables**:
   Create a `.env` file in the root directory based on `app/core/config.py`. Minimum requirements:
   ```env
   OPENAI_API_KEY=your_openai_api_key
   ENVIRONMENT=development
   REDIS_URL=redis://localhost:6379
   OTP_CHECKER=False  # Set to False to mock OTP locally and print it to console
   ```

5. **Start Redis**:
   You can run Redis locally using Docker:
   ```bash
   docker-compose up -d redis
   ```

6. **Run the Application**:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8085 --reload
   ```
   *(Alternatively, run everything via `docker-compose up --build`)*

The API will be available at `http://localhost:8085`. Interactive API docs are at `http://localhost:8085/docs`.

---

## How to Set Up in Production

1. **Update Environment Variables**:
   In your production `.env` or CI/CD secrets manager, set:
   ```env
   ENVIRONMENT=production
   OPENAI_API_KEY=your_openai_api_key
   REDIS_URL=redis://<your-production-redis-url>:6379
   
   # AWS S3 Integration for Resumes & Audio
   S3_ACCESS_KEY=your_s3_access_key
   S3_SECRET_KEY=your_s3_secret_key
   S3_REGION=eu-north-1
   S3_BUCKET_NAME=your_bucket_name
   
   # OTP Verification Backend
   OTP_CHECKER=True
   OTP_BACKEND_SEND_URL=https://auth.yourdomain.com/otp/send
   OTP_BACKEND_VERIFY_URL=https://auth.yourdomain.com/otp/verify
   ```

2. **Disable Debug Endpoints**:
   Setting `ENVIRONMENT=production` automatically disables the `/hiring/debug/session/{id}` endpoint to prevent exposing internal state and candidate scores.

3. **Deployment**:
   - Use a robust ASGI server like `gunicorn` with `uvicorn` workers for optimal performance:
     ```bash
     gunicorn main:app -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8085
     ```
   - Build the provided `Dockerfile` and deploy it to a container orchestration platform (e.g., AWS ECS, Kubernetes, Railway, or Render).
   - Ensure that the external Redis instance and S3 buckets are properly secured and accessible by the container.
   - For ChromaDB persistence, ensure that the `/app/chroma_db` directory is mapped to a persistent volume so the vector embeddings aren't lost on container restarts.
