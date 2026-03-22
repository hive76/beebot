FROM python:3.12-slim

LABEL maintainer="Hive76"
LABEL description="BeeBot - Slack AI Assistant"

# Don't write .pyc files, don't buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and version
COPY bot/     ./bot/
COPY sync/    ./sync/
COPY VERSION  .

# Data and config directories (mounted as volumes in production)
RUN mkdir -p /app/data /app/config

# Run as non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Healthcheck — verifies the process is alive
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f beebot.py || exit 1

CMD ["python", "bot/beebot.py"]
