FROM python:3.13-slim AS base
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

FROM base AS deps
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS runner
ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8559 \
    METADATA_DIR=/data/metadata \
    DUMPS_DIR=/data/dumps \
    DB_PATH=/data/cfsmcp2.sqlite3 \
    ZVEC_DIR=/data/zvec \
    LM_STUDIO_URL=http://host.docker.internal:1234
COPY --from=deps /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY app ./app
RUN useradd -m appuser && mkdir -p /data && chown -R appuser:appuser /data /app
USER appuser
EXPOSE 8559
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8559/api/health || exit 1
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8559"]
