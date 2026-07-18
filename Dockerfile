# Slim Python image; the app is pure-Python plus curl_cffi wheels.
FROM python:3.12-slim AS base

# No .pyc, unbuffered logs (so JSON logs stream in real time).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install runtime dependencies first for better layer caching.
COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

# Copy the application code.
COPY glassdoor_scraper ./glassdoor_scraper
COPY api ./api
COPY glassdoor_jobs.py ./

# Run as a non-root user. The SQLite database lives under /data (a mounted volume),
# owned by the app user so it is writable.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

ENV GLASSDOOR_DB_PATH=/data/glassdoor_scraper.db \
    GLASSDOOR_API_HOST=0.0.0.0 \
    GLASSDOOR_API_PORT=8000

EXPOSE 8000

# A simple healthcheck against the /healthz endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
url='http://127.0.0.1:'+os.environ.get('GLASSDOOR_API_PORT','8000')+'/healthz'; \
sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"

# uvicorn honors GLASSDOOR_API_HOST/PORT via the shell form below.
CMD ["sh", "-c", "uvicorn api.main:app --host \"$GLASSDOOR_API_HOST\" --port \"$GLASSDOOR_API_PORT\""]
