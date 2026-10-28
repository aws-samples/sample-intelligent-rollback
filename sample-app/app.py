"""
Sample Application: Flask app with configurable failure injection.

This application simulates a microservice that is deployed through AWS
CodeDeploy blue/green on Amazon ECS. It exposes health and request endpoints
and publishes custom Amazon CloudWatch metrics that feed the rollback alarm.

Failure Injection:
    ERROR_RATE simulates a code defect in the new revision: a share of
    requests fails with HTTP 500 and an AttributeError log line.
    DEPENDENCY_FAILURE_RATE simulates an outage of the downstream payments
    dependency: a share of requests fails with HTTP 503 and a ConnectTimeout
    log line. No network call is made. When DEPENDENCY_FAILURE_PARAMETER names
    an AWS Systems Manager parameter, its value overrides the environment
    value, so one command affects the old and new task sets at the same time.
    LATENCY_MS adds artificial latency to responses.

These settings come from the ECS task definition, so a "bad" version can be
deployed by changing the task definition without code changes.

Environment Variables:
    ERROR_RATE                   Probability of returning HTTP 500 (default: 0.0)
    DEPENDENCY_FAILURE_RATE      Probability of a simulated dependency timeout
                                 returning HTTP 503 (default: 0.0)
    DEPENDENCY_FAILURE_PARAMETER Systems Manager parameter (String) that
                                 overrides DEPENDENCY_FAILURE_RATE; read at
                                 most once every 15 seconds per worker
                                 (default: empty, no override)
    LATENCY_MS                   Additional response latency in ms (default: 0)
    APP_VERSION                  Version reported in responses, logs, and the
                                 Version metric dimension (default: 1.0.0)
    CLOUDWATCH_NAMESPACE         Custom metric namespace (default: IntelligentRollback)
    SERVICE_NAME                 Service identifier for metrics (default: sample-app)
    PORT                         Listen port (default: 8080)
    HOST                         Local dev server bind address (default: 127.0.0.1;
                                 the container uses gunicorn, see Dockerfile)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.request
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _parse_rate(name: str, raw: str | None) -> float:
    """Parse a probability in [0, 1]; invalid values fall back to 0.0 with a warning."""
    if raw is None or raw.strip() == "":
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s value %r; using 0.0", name, raw)
        return 0.0
    if not 0.0 <= value <= 1.0:
        logger.warning("%s value %r is outside [0, 1]; using 0.0", name, raw)
        return 0.0
    return value


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Failure injection settings (read at startup, can be changed via task def)
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.0"))
LATENCY_MS = int(os.environ.get("LATENCY_MS", "0"))
DEPENDENCY_FAILURE_RATE = _parse_rate(
    "DEPENDENCY_FAILURE_RATE", os.environ.get("DEPENDENCY_FAILURE_RATE")
)
DEPENDENCY_FAILURE_PARAMETER = os.environ.get("DEPENDENCY_FAILURE_PARAMETER", "").strip()
DEPENDENCY_CACHE_SECONDS = 15.0

# Simulated downstream dependency (no network call is made)
DEPENDENCY_NAME = "payments"
DEPENDENCY_HOST = "payments.example.com"
DEPENDENCY_TIMEOUT_MS = 2000

APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")

# CloudWatch metric configuration
CLOUDWATCH_NAMESPACE = os.environ.get("CLOUDWATCH_NAMESPACE", "IntelligentRollback")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "sample-app")
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "127.0.0.1")

# Amazon ECS task metadata endpoint (set by the ECS agent; absent locally)
METADATA_TIMEOUT_SECONDS = 1

# CloudWatch client for publishing metrics
# Uses the task role credentials automatically in ECS
try:
    cloudwatch_client = boto3.client("cloudwatch")
    logger.info("CloudWatch client initialized successfully")
except Exception as exc:
    logger.warning("CloudWatch client init failed (metrics disabled): %s", exc)
    cloudwatch_client = None

# Systems Manager client, only when the override parameter is configured
ssm_client = None
if DEPENDENCY_FAILURE_PARAMETER:
    try:
        ssm_client = boto3.client("ssm")
    except Exception as exc:
        logger.warning("SSM client init failed (parameter override disabled): %s", exc)
        ssm_client = None

# Per-worker cache for the parameter override
_dependency_cache: dict[str, float | None] = {
    "value": DEPENDENCY_FAILURE_RATE,
    "fetched_at": None,
    "warned_at": None,
}

# Per-worker cache for the task ARN (fetched once at first use)
_task_arn_cache: dict[str, str | None] = {"value": None}


# ---------------------------------------------------------------------------
# Dependency failure rate (environment value or SSM override)
# ---------------------------------------------------------------------------


def _warn_once_per_interval(now: float, message: str, *args) -> None:
    """Log a warning at most once per cache interval."""
    warned_at = _dependency_cache["warned_at"]
    if warned_at is None or now - warned_at >= DEPENDENCY_CACHE_SECONDS:
        logger.warning(message, *args)
        _dependency_cache["warned_at"] = now


def get_dependency_failure_rate() -> float:
    """Return the current dependency failure rate.

    Without DEPENDENCY_FAILURE_PARAMETER this is the environment value. With
    it, the parameter value is read at most once per DEPENDENCY_CACHE_SECONDS.
    Read errors and out-of-range values keep the last good value (initially
    the environment value).
    """
    if not DEPENDENCY_FAILURE_PARAMETER or ssm_client is None:
        return DEPENDENCY_FAILURE_RATE

    now = time.monotonic()
    fetched_at = _dependency_cache["fetched_at"]
    if fetched_at is not None and now - fetched_at < DEPENDENCY_CACHE_SECONDS:
        return float(_dependency_cache["value"])

    # Mark the attempt first so a failing read is retried once per interval,
    # not on every request.
    _dependency_cache["fetched_at"] = now
    try:
        response = ssm_client.get_parameter(Name=DEPENDENCY_FAILURE_PARAMETER)
        raw = response["Parameter"]["Value"]
    except (ClientError, BotoCoreError, KeyError, TypeError) as exc:
        _warn_once_per_interval(
            now,
            "Failed to read parameter %s; keeping dependency failure rate %.2f: %s",
            DEPENDENCY_FAILURE_PARAMETER,
            _dependency_cache["value"],
            exc,
        )
        return float(_dependency_cache["value"])

    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = None
    if value is None or not 0.0 <= value <= 1.0:
        _warn_once_per_interval(
            now,
            "Ignoring parameter %s value %r (expected a number in [0, 1]); "
            "keeping dependency failure rate %.2f",
            DEPENDENCY_FAILURE_PARAMETER,
            raw,
            _dependency_cache["value"],
        )
        return float(_dependency_cache["value"])

    _dependency_cache["value"] = value
    return value


# ---------------------------------------------------------------------------
# Task metadata
# ---------------------------------------------------------------------------


def get_task_arn() -> str:
    """Return the ECS task ARN from the task metadata endpoint (v4).

    Returns "local" when ECS_CONTAINER_METADATA_URI_V4 is not set and
    "unknown" on any error. The result is cached at first use.
    """
    cached = _task_arn_cache["value"]
    if cached is not None:
        return cached

    base_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "")
    if not base_uri:
        result = "local"
    elif not base_uri.startswith(("http://", "https://")):
        result = "unknown"
    else:
        try:
            with urllib.request.urlopen(  # nosec B310 - scheme checked above
                f"{base_uri.rstrip('/')}/task", timeout=METADATA_TIMEOUT_SECONDS
            ) as response:
                task_arn = json.loads(response.read().decode("utf-8")).get("TaskARN")
            result = task_arn if isinstance(task_arn, str) and task_arn else "unknown"
        except Exception as exc:  # any failure means the ARN is unknown
            logger.warning("Failed to read task metadata: %s", exc)
            result = "unknown"

    _task_arn_cache["value"] = result
    return result


# ---------------------------------------------------------------------------
# Metrics Publishing
# ---------------------------------------------------------------------------


def publish_metrics(
    request_count: int = 1,
    error_count: int = 0,
    latency_ms: float = 0.0,
    dependency_health: int = 1,
) -> None:
    """Publish custom metrics to CloudWatch in one PutMetricData call.

    Each request publishes seven data points:
    - RequestCount, ErrorCount, and Latency with [ServiceName], which the
      rollback alarm uses
    - RequestCount, ErrorCount, and Latency with [ServiceName, Version], which
      lets the agent compare error rates per revision
    - DependencyHealth with [ServiceName, Dependency] (1 healthy, 0 failing)
    """
    if cloudwatch_client is None:
        return

    service_dims = [{"Name": "ServiceName", "Value": SERVICE_NAME}]
    version_dims = service_dims + [{"Name": "Version", "Value": APP_VERSION}]
    dependency_dims = service_dims + [{"Name": "Dependency", "Value": DEPENDENCY_NAME}]

    metric_data = []
    for dims in (service_dims, version_dims):
        metric_data.extend(
            [
                {
                    "MetricName": "RequestCount",
                    "Value": request_count,
                    "Unit": "Count",
                    "Dimensions": dims,
                },
                {
                    "MetricName": "ErrorCount",
                    "Value": error_count,
                    "Unit": "Count",
                    "Dimensions": dims,
                },
                {
                    "MetricName": "Latency",
                    "Value": latency_ms,
                    "Unit": "Milliseconds",
                    "Dimensions": dims,
                },
            ]
        )
    metric_data.append(
        {
            "MetricName": "DependencyHealth",
            "Value": dependency_health,
            "Unit": "None",
            "Dimensions": dependency_dims,
        }
    )

    try:
        cloudwatch_client.put_metric_data(
            Namespace=CLOUDWATCH_NAMESPACE,
            MetricData=metric_data,
        )
    except (ClientError, BotoCoreError) as exc:
        # Don't fail the request if metrics publishing fails
        logger.warning("Failed to publish CloudWatch metrics: %s", exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint for ALB target group health checks.

    Returns 200 unconditionally. Even "bad" deployments pass health checks,
    so CodeDeploy does not roll back on its own before the AWS DevOps Agent
    evaluates the situation.
    """
    return jsonify(
        {
            "status": "healthy",
            "service": SERVICE_NAME,
            "version": APP_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config": {
                "error_rate": ERROR_RATE,
                "dependency_failure_rate": get_dependency_failure_rate(),
                "latency_ms": LATENCY_MS,
            },
        }
    ), 200


@app.route("/", methods=["GET"])
def index():
    """Main request endpoint with configurable failure injection.

    The dependency outcome is drawn first (HTTP 503 on a simulated timeout),
    then ERROR_RATE decides whether the simulated code defect fires (HTTP 500).
    """
    start_time = time.time()

    # Inject artificial latency (simulates slow database/dependency)
    if LATENCY_MS > 0:
        time.sleep(LATENCY_MS / 1000.0)

    # Simulated downstream dependency call (no network call is made)
    if random.random() < get_dependency_failure_rate():
        logger.warning(
            "ConnectTimeout to %s after %d ms (dependency=%s, version=%s)",
            DEPENDENCY_HOST,
            DEPENDENCY_TIMEOUT_MS,
            DEPENDENCY_NAME,
            APP_VERSION,
        )
        elapsed_ms = (time.time() - start_time) * 1000
        publish_metrics(
            request_count=1, error_count=1, latency_ms=elapsed_ms, dependency_health=0
        )
        return jsonify(
            {
                "error": "Service Unavailable",
                "message": "Upstream dependency timeout",
                "service": SERVICE_NAME,
                "version": APP_VERSION,
            }
        ), 503

    # Simulated code defect in this revision
    if random.random() < ERROR_RATE:
        logger.error(
            "Unhandled exception in GET / handler: AttributeError: "
            "'NoneType' object has no attribute 'total' (version=%s)",
            APP_VERSION,
        )
        elapsed_ms = (time.time() - start_time) * 1000
        publish_metrics(
            request_count=1, error_count=1, latency_ms=elapsed_ms, dependency_health=1
        )
        return jsonify(
            {
                "error": "Internal Server Error",
                "message": "Simulated failure for rollback demonstration",
                "service": SERVICE_NAME,
                "version": APP_VERSION,
            }
        ), 500

    # Successful response
    elapsed_ms = (time.time() - start_time) * 1000
    publish_metrics(
        request_count=1, error_count=0, latency_ms=elapsed_ms, dependency_health=1
    )

    return jsonify(
        {
            "message": "Hello from the sample app!",
            "service": SERVICE_NAME,
            "version": APP_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "latency_ms": round(elapsed_ms, 2),
        }
    ), 200


@app.route("/info", methods=["GET"])
def info():
    """Service information endpoint for debugging and observability."""
    return jsonify(
        {
            "service": SERVICE_NAME,
            "version": APP_VERSION,
            "config": {
                "error_rate": ERROR_RATE,
                "dependency_failure_rate": get_dependency_failure_rate(),
                "latency_ms": LATENCY_MS,
                "cloudwatch_namespace": CLOUDWATCH_NAMESPACE,
                "port": PORT,
            },
            "environment": {
                "task_arn": get_task_arn(),
            },
        }
    ), 200


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Starting %s %s on port %d (error_rate=%.2f, dependency_failure_rate=%.2f, latency_ms=%d)",
        SERVICE_NAME,
        APP_VERSION,
        PORT,
        ERROR_RATE,
        DEPENDENCY_FAILURE_RATE,
        LATENCY_MS,
    )
    # In production, Gunicorn runs the app; this is for local dev only
    app.run(host=HOST, port=PORT, debug=False)
