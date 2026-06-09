# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Non-root user
RUN addgroup --system app && adduser --system --ingroup app app
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy service code
COPY . /app/job_parser_service/

# Make sure the package is importable
ENV PYTHONPATH=/app

USER app
EXPOSE 8001

# Graceful shutdown: uvicorn handles SIGTERM
CMD ["uvicorn", "job_parser_service.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8001", \
     "--workers", "1", \
     "--timeout-keep-alive", "30"]
