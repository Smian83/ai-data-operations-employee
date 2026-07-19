# syntax=docker/dockerfile:1

# =============================================================================
# Stage 1: builder — install dependencies into a virtualenv
# Build context is the repo root (see docker-compose.yml), so paths below
# are relative to the repo root, not this docker/ directory.
# =============================================================================
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY backend/requirements.txt .
RUN pip install --upgrade pip \
    && pip install -r requirements.txt

# =============================================================================
# Stage 2: runtime — minimal image with only the built venv + app code
# =============================================================================
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Create a non-root user to run the application
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser backend/app ./app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
    sys.exit(0) if urllib.request.urlopen('http://localhost:8000/health').getcode() == 200 else sys.exit(1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
