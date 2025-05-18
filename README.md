# AI Streaming Microservice

## Quick Start with Docker Compose

1. Copy `.env.example` to `.env` and fill in your API keys and configuration.
2. Start the service:

```bash
docker-compose up --build
```

A microservice for streaming AI responses from OpenAI and Anthropic APIs with n8n integration.

## Requirements
- Docker and Docker Compose
- n8n instance (included in docker-compose)
- PostgreSQL (for n8n, included)
- Redis (included)

## Features
- Supports both OpenAI and Anthropic models
- Server-Sent Events (SSE) streaming
- Redis for temporary session storage
- JWT authentication
- n8n integration
- Token usage tracking
- CORS support
- Health checks

## Setup

### Environment Variables
Copy `.env.example` to `.env` and fill in your own values:

```bash
cp .env.example .env
```

### Running the Stack
```bash
docker compose up -d
```

This will start:
- AI Streaming service on port 8000

### Persistent Data
By default, this minimal setup does not use Docker volumes for persistence. If you add Redis or other services, you may want to configure volumes as needed.

## API Endpoints

### 1. POST /init-stream
Called by n8n to initialize streaming session. 

**Request Payload:**
```json
{
    "n8nToken": "JWT_TOKEN",          // Required: JWT token for authentication
    "resumeUrl": "https://your-n8n/webhook/...",  // Required: Webhook URL for completion callback
    "prompt": "Your prompt",          // Required: The prompt to send to AI
    "userId": "user123",              // Optional: User identifier
    "chatId": "chat123",              // Optional: Chat/conversation identifier
    "model": "gpt-4",                 // Optional: AI model to use (default: gpt-4)
    "max_tokens": 4096                // Optional: Maximum tokens in response (default: 4096)
}
```

**Supported Models:**
- OpenAI models, Anthropic models as per their APIs.

**Response:**
```json
{
    "ok": true,
    "streamUrl": "https://your-domain/stream/uuid?access_token=jwt"
}
```

**Callback to resumeUrl Payload:**
```json
{
    "userId": "user123",
    "chatId": "chat123",
    "prompt": "Original prompt",
    "answer": "Complete AI response",
    "input_tokens": 123,    // Number of tokens in prompt
    "output_tokens": 456    // Number of tokens in response
}
```

### 2. GET /stream/{stream_id}
Frontend endpoint for SSE streaming:
```javascript
const eventSource = new EventSource(`/stream/${streamId}?access_token=${token}`);
```

### 3. GET /health
Health check endpoint.

## Development

1. Set development environment:
```bash
DEV_MODE=true
ALLOWED_ORIGINS=http://localhost:3000
```

2. Generate test token:
```bash
python generate_token.py
```

## Production Setup
1. Set proper ALLOWED_ORIGINS
2. Disable DEV_MODE
3. Use secure JWT_SECRET
4. Configure proper domain names
5. Set up SSL/TLS
6. Configure proper backup strategy for volumes
7. Monitor health endpoints

## Security Notes
- All API keys are handled securely
- CORS is strictly configured in production
- JWT is used for authentication
- Redis data has TTL for cleanup
- PostgreSQL uses non-root user
- Health checks ensure service availability

## Detailed Architecture

### Components Overview
1. **AI Streaming Service**
   - Handles real-time streaming of AI responses
   - Manages API keys securely
   - Routes requests to appropriate AI provider (OpenAI/Anthropic)
   - Implements SSE (Server-Sent Events) protocol
   - Tracks token usage and handles errors

2. **n8n Integration**
   - Acts as orchestrator for the workflow
   - Initiates streaming sessions via `/init-stream`
   - Receives completion callbacks
   - Handles user management and billing
   - Stores conversation history

3. **Redis Layer**
   - Temporary session storage (60s TTL)
   - Stores streaming metadata
   - Handles stream IDs and access tokens
   - No permanent data storage
   - Runs on non-standard port (6381) to avoid conflicts

4. **PostgreSQL Database**
   - Stores n8n workflows and credentials
   - Maintains execution history
   - Handles user data and configurations
   - Uses non-root user for security

## Architecture explanation

1. **Flow**:
   - n8n calls `/init-stream` with JWT token
   - Service creates streamId, stores data in Redis (60s TTL)
   - Returns streamUrl to n8n
   - Frontend connects to streamUrl via EventSource
   - Service streams AI response
   - On completion, calls n8n resumeUrl with results

2. **Security**:
   - JWT authentication for n8n requests
   - Access tokens for streaming
   - CORS protection
   - Redis TTL for session cleanup

3. **Models Support**:
   - OpenAI models (gpt-4o, etc.)
   - Anthropic models (claude-3.5-Sonnet, etc.)
   - Automatic API routing based on model name

4. **Token Usage**:
   - Tracks input/output tokens
   - Reports usage in callback
   - Supports both OpenAI and Anthropic token counting

### Security Architecture
1. **Authentication Layers**
   - JWT for n8n authentication
   - Access tokens for stream access
   - PostgreSQL user isolation
   - Redis session management

2. **Data Protection**
   - API keys never stored permanently
   - All sessions have TTL
   - CORS protection
   - SSL/TLS encryption

3. **Error Handling**
   - Graceful error recovery
   - Client disconnection handling
   - Token validation
   - Rate limiting

### Scaling Considerations
1. **Horizontal Scaling**
   - Stateless application design
   - Redis for session sharing
   - Load balancer ready
   - Health check endpoints

2. **Resource Management**
   - Token usage tracking
   - Session cleanup
   - Database connection pooling
   - Memory management

### Integration Points

1. **Frontend Integration**
   ```javascript
   // 1. Initialize chat with your backend
   const startChat = async (prompt, options = {}) => {
     const response = await fetch('/your-backend/start-chat', {
       method: 'POST',
       headers: {
         'Content-Type': 'application/json'
       },
       body: JSON.stringify({
         prompt,
         model: options.model || 'gpt-4o-mini'      // Optional: AI model
         max_tokens: options.maxTokens || 4096,     // Optional: Response length
         userId: options.userId,                    // Optional: User tracking
         chatId: options.chatId                     // Optional: Chat tracking
       })
     });
     const { streamUrl } = await response.json();
     return streamUrl;
   };

   // 2. Connect to SSE stream with error handling
   const connectToStream = (streamUrl, {
     onMessage = console.log,
     onError = console.error,
     onComplete = () => {}
   } = {}) => {
     const eventSource = new EventSource(streamUrl);
     
     eventSource.onmessage = (event) => {
       if (event.data === "[DONE]") {
         eventSource.close();
         onComplete();
       } else {
         // Handle streaming response
         onMessage(event.data);
       }
     };

     eventSource.onerror = (error) => {
       console.error('SSE Error:', error);
       eventSource.close();
       onError(error);
     };

     return eventSource;
   };

   // 3. Usage Example
   try {
     // Start chat
     const streamUrl = await startChat("What is AI?", {
       model: "gpt-4o-mini",
       maxTokens: 2000,
       userId: "user_123",
       chatId: "chat_456"
     });

     // Initialize streaming
     let fullResponse = '';
     const stream = connectToStream(streamUrl, {
       onMessage: (chunk) => {
         fullResponse += chunk;
         // Update UI with incoming chunks
         updateUI(chunk);
       },
       onError: (error) => {
         showErrorToUser("Connection lost");
       },
       onComplete: () => {
         // Handle completion
         saveToHistory(fullResponse);
       }
     });

     // Optional: Cleanup on component unmount
     return () => stream.close();
   } catch (error) {
     handleError(error);
   }
   ```