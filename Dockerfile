FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

LABEL org.opencontainers.image.title="Prometheus Relay" \
      org.opencontainers.image.description="Self-hosted Douyin streak automation console" \
      org.opencontainers.image.source="https://github.com/MinG-98/prometheus-relay" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HEADLESS=true \
    DEBUG=false

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.txt

COPY . .

# Do not run browser automation as root. The Playwright image already
# contains Chromium and its system dependencies.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "main.py"]
