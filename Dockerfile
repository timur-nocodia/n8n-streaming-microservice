FROM python:3.11-slim

WORKDIR /app

# Install curl for health checks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Gunicorn configuration for production
CMD ["gunicorn", "main:app", \
     "--workers", "4", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--worker-connections", "1000", \
     "--backlog", "2048", \
     "--max-requests", "5000", \
     "--max-requests-jitter", "500", \
     "--timeout", "300", \
     "--keep-alive", "5", \
     "--bind", "0.0.0.0:8000"] 