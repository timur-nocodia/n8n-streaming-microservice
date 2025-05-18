import os
import uuid
import jwt
import httpx
import redis.asyncio as redis
import asyncio

from openai import AsyncOpenAI
from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from typing import Optional, List

load_dotenv()

app = FastAPI()

# Environment settings
REDIS_URL = os.getenv("REDIS_URL")  # Redis URL
JWT_SECRET = os.getenv("JWT_SECRET")
JWT_ALGORITHM = "HS256"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
BASE_URL = os.getenv("BASE_URL")

# Initialize clients
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
redis_client = redis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)

# Development mode flag
DEV_MODE = os.getenv("DEV_MODE", "false").lower() == "true"

# Get allowed origins from environment
if DEV_MODE:
    ALLOWED_ORIGINS = ["*"]  # Allow all origins in dev mode
else:
    ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    # Validate ALLOWED_ORIGINS in production
    if not ALLOWED_ORIGINS or any(not origin.strip() for origin in ALLOWED_ORIGINS):
        raise ValueError("ALLOWED_ORIGINS must contain valid URLs in production mode")

if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY not found")
if not ANTHROPIC_API_KEY:
    raise ValueError("ANTHROPIC_API_KEY not found")
if not BASE_URL:
    raise ValueError("BASE_URL not found")

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if not DEV_MODE else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"] if DEV_MODE else ["Authorization", "Content-Type", "x-api-key"],
    expose_headers=["*"],
    max_age=3600,
)

# Origin verification middleware (only in production)
@app.middleware("http")
async def verify_origin(request: Request, call_next):
    if not DEV_MODE:
        origin = request.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            raise HTTPException(status_code=403, detail="Origin not allowed")
    
    response = await call_next(request)
    return response

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/init-stream")
async def init_stream(request: Request):
    """
    The n8n requests this endpoint (POST) by passing:
    {
      “n8nToken": ‘...’, (JWT to authorize this call)
      { “resumeUrl”: “...”, (where the microservice will call upon completion)
      “prompt": “...”,
      “userId": ‘...’,
      “chatId": ‘...’,
      “model": “...”, (optional, default gpt-3.5-turbo)
      ...
    }

    Logic:
    1) Check the n8nToken
    2) Generate streamId (UUID)
    3) Save it all in Redis (prompt, userId, chatId, model, resumeUrl) with TTL = 60 sec.
    4) Create a short-lived accessToken (JWT) bound to streamId
    5) Return streamUrl (GET /stream/{streamId}?access_token=xxx) n8n
    """

    data = await request.json()
    n8n_token = data.get("n8nToken")
    resume_url = data.get("resumeUrl")
    prompt = data.get("prompt")
    user_id = data.get("userId")
    chat_id = data.get("chatId")
    model = data.get("model", "gpt-4o-mini")

    if not n8n_token or not prompt or not resume_url:
        raise HTTPException(status_code=400, detail="Missing required fields: n8nToken, prompt, resumeUrl")

    # 1) Validating JWT (n8nToken)
    try:
        payload = jwt.decode(n8n_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        # Here you can check payload[“role”] == “n8n” or exp, etc.
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "n8nToken expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "n8nToken invalid")

    # 2) Generate streamId
    stream_id = str(uuid.uuid4())

    # 3) Put the data into Redis (TTL 60 sec)
    store_data = {
        "prompt": prompt,
        "userId": user_id or "",
        "chatId": chat_id or "",
        "model": model,
        "resumeUrl": resume_url
    }
    await redis_client.hmset(stream_id, store_data)
    await redis_client.expire(stream_id, 60)

    # 4) Generate accessToken (JWT) for streaming
    # Set exp small (e.g. 2 minutes or the same payload[“exp”])
    access_token = jwt.encode(
        {
            "streamId": stream_id,
            "exp": payload.get("exp")  # Can be limited, e.g.: time.time() + 120
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM
    )

    # 5) Generate streamUrl
    stream_url = f"{BASE_URL}/stream/{stream_id}?access_token={access_token}"

    return {
        "ok": True,
        "streamUrl": stream_url
    }

async def count_anthropic_tokens(model: str, messages: list) -> int:
    """Count tokens for Anthropic messages"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages/count_tokens",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "token-counting-2024-11-01",
                    "content-type": "application/json"
                },
                json={
                    "model": model,
                    "messages": messages
                }
            )
            if response.status_code == 200:
                return response.json()["input_tokens"]
    except Exception as e:
        print(f"Error counting tokens: {str(e)}")
    return None

@app.get("/stream/{stream_id}")
async def stream_sse(stream_id: str, access_token: str):
    """
    Frontend -> "GET /stream/{stream_id}?access_token=..."

    Logic:
    1) Decode accessToken, compare streamId
    2) Read prompt / resumeUrl / userId / chatId / model from Redis
    3) Start stream (SSE), send chunks
    4) When finished (finish_reason==“stop” or error) -> collect full answer + usage tokens
       -> do POST to resumeUrl (from n8n)
    """

    # 1) Check accessToken
    try:
        token_payload = jwt.decode(access_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        if token_payload["streamId"] != stream_id:
            raise HTTPException(401, "Token does not match stream_id")
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "accessToken expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "accessToken invalid")

    # 2) Taking data from Redis
    data = await redis_client.hgetall(stream_id)
    if not data:
        raise HTTPException(404, "stream_id not found or expired (TTL)")

    prompt = data["prompt"]
    user_id = data["userId"]
    chat_id = data["chatId"]
    model = data["model"]
    resume_url = data["resumeUrl"]
    max_tokens = int(data.get("max_tokens", 4096))  # default to 4096 if not provided

    async def event_generator():
        full_chunks = []
        usage_input_tokens = None
        usage_output_tokens = None
        try:
            if DEV_MODE:
                print(f"[DEBUG] Starting stream for model {model} with prompt: {prompt}")
            
            if model.startswith("claude"):
                # Count input tokens
                usage_input_tokens = await count_anthropic_tokens(model, [{"role": "user", "content": prompt}])
                
                # Anthropic stream
                resp_stream = await anthropic_client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                    stream=True
                )
                
                async for chunk in resp_stream:
                    if chunk.type == "content_block_delta" and chunk.delta.text:
                        text_part = chunk.delta.text
                        print(f"[DEBUG] Anthropic chunk: {repr(text_part)}")
                        text_part = text_part.replace('\n', '\\n')
                        message = f"data: {text_part}\n\n"
                        yield message
                        full_chunks.append(text_part)
                    elif chunk.type == "message_stop":
                        if DEV_MODE:
                            print("[DEBUG] Received stop signal from Anthropic, sending [DONE]")
                        yield "data: [DONE]\n\n"
                
                # Count output tokens
                final_answer = "".join(full_chunks)
                usage_output_tokens = await count_anthropic_tokens(model, [{"role": "assistant", "content": final_answer}])
                
            else:
                # OpenAI stream
                resp_stream = await openai_client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                    stream=True,
                    stream_options={"include_usage": True}
                )
                
                async for chunk in resp_stream:
                    if not chunk.choices:
                        if hasattr(chunk, 'usage') and chunk.usage:
                            usage_input_tokens = chunk.usage.prompt_tokens
                            usage_output_tokens = chunk.usage.completion_tokens
                        continue

                    choice = chunk.choices[0]
                    finish_reason = choice.finish_reason

                    if choice.delta and choice.delta.content:
                        text_part = choice.delta.content
                        text_part = text_part.replace('\n', '\\n')
                        message = f"data: {text_part}\n\n"
                        yield message
                        full_chunks.append(text_part)
                    elif finish_reason == "stop":
                        yield "data: [DONE]\n\n"

        except asyncio.CancelledError:
            if DEV_MODE:
                print("[DEBUG] Client disconnected - CancelledError caught")
        except Exception as e:
            err_str = f"Error: {str(e)}"
            if DEV_MODE:
                print(f"[DEBUG] Error in stream: {err_str}")
            yield f"data: {err_str}\n\n"
        finally:
            final_answer = "".join(full_chunks)
            if DEV_MODE:
                print(f"[DEBUG] Stream completed. Final answer length: {len(final_answer)}")
            
            await call_resume_callback(
                resume_url=resume_url,
                user_id=user_id,
                chat_id=chat_id,
                prompt=prompt,
                answer=final_answer,
                input_tokens=usage_input_tokens,
                output_tokens=usage_output_tokens
            )
            await redis_client.delete(stream_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
            "Access-Control-Allow-Origin": "*"
        }
    )

async def call_resume_callback(
    resume_url: str,
    user_id: str,
    chat_id: str,
    prompt: str,
    answer: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
):
    """
    POST to n8n resumeUrl
    Pass the final answer, usage tokens, userId / chatId / prompt, etc.
    """
    payload = {
        "userId": user_id,
        "chatId": chat_id,
        "prompt": prompt,
        "answer": answer,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens
    }
    try:
        async with httpx.AsyncClient() as http_client:
            resp = await http_client.post(resume_url, json=payload, timeout=30)
            if DEV_MODE:
                print("call_resume_callback:", resp.status_code, resp.text)
    except Exception as ex:
        if DEV_MODE:
            print("resume callback error:", str(ex))

@app.get("/test-stream")
async def test_stream():
    """Test endpoint that simulates streaming responses"""
    async def test_generator():
        messages = ["Hello", " World", "!", " This", " is", " a", " test", " stream"]
        for msg in messages:
            print(f"[TEST] About to yield: data: {msg}\n\n")
            yield f"data: {msg}\n\n"
            print(f"[TEST] After yield: {msg}")
            await asyncio.sleep(1)  # Simulate delay between chunks
        print("[TEST] About to yield [DONE]")
        yield "data: [DONE]\n\n"
        print("[TEST] After yield [DONE]")

    return StreamingResponse(
        test_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
            "Access-Control-Allow-Origin": "*"
        }
    )