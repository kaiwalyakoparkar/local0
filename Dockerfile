# Pin a specific patch tag (not the mutable :3.11-slim) for reproducibility.
# ponytail: pin by @sha256 digest in CI once the registry digest is resolvable.
FROM python:3.11.9-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY assets ./assets
COPY ingest.py eval.py ./

# Run as a non-root user. Create the stats dir up front and hand /app to the user;
# a bind-mounted ./data may still be root-owned, but stats.save() is best-effort
# and degrades to in-memory if it can't write.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

EXPOSE 8081
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health').status==200 else 1)"]

# --workers 1: routing counters + config live in-process (single-replica design).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8081", "--workers", "1"]
