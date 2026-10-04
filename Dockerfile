FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HEADLESS=1 \
    PORT=7860 \
    EMBED_SANDBOX=1 \
    MAX_CONCURRENT_RUNS=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .
# Hugging Face Spaces runs the container as uid 1000, so data dirs must be writable.
RUN mkdir -p /app/data && chmod -R 777 /app/data /ms-playwright

EXPOSE 7860
CMD ["sh", "-c", "python -m worker serve --port ${PORT:-7860}"]
