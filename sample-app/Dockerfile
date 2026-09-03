# ---------------------------------------------------------------------------
# Multi-stage Dockerfile for the Intelligent Rollback sample application.
#
# Stage 1: Install dependencies in a builder image (keeps final image small)
# Stage 2: Copy only what's needed into a slim production image
#
# Security: Runs as non-root user, minimal attack surface, no dev tools.
# ---------------------------------------------------------------------------

# Stage 1: Builder — install Python dependencies
FROM python:3.12-slim AS builder

WORKDIR /build

# Install dependencies into a prefix directory for clean copy
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Production image
FROM python:3.12-slim AS production

# Security: Create non-root user before copying application files
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy installed dependencies from builder stage
COPY --from=builder /install /usr/local

# Copy application code
COPY app.py .

# Set ownership to non-root user
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Environment defaults (overridden by ECS task definition)
ENV PORT=8080 \
    ERROR_RATE=0.0 \
    LATENCY_MS=0 \
    SERVICE_NAME=sample-app \
    CLOUDWATCH_NAMESPACE=IntelligentRollback \
    APP_VERSION=1.0.0

# Expose the application port
EXPOSE 8080

# Health check for container-level monitoring
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

# Run with Gunicorn for production performance
# - 4 workers (adjust based on vCPU allocation in task def)
# - 120s timeout to handle latency injection scenarios
CMD ["python", "-m", "gunicorn", "--bind", "0.0.0.0:8080", "--workers", "4", "--timeout", "120", "--access-logfile", "-", "app:app"]
