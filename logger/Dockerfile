# EasyADSB Flight Logger
# Version: 1.2.0
# Lightweight Python container for flight logging and API

FROM python:3.11-slim

LABEL maintainer="EasyADSB"
LABEL description="Flight logger with REST API for EasyADSB"
LABEL version="1.2.0"

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app.py .

# Create data directory
RUN mkdir -p /data

# Expose API port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# Run the application
CMD ["python", "-u", "app.py"]
