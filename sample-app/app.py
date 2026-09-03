"""
Sample Application — Flask app with configurable failure injection.

This application simulates a microservice that can be deployed via CodeDeploy
blue/green on ECS. It exposes health and request endpoints, and publishes
custom CloudWatch metrics that feed the rollback alarm.

Failure Injection:
    Set ERROR_RATE (0.0-1.0) to simulate a percentage of 5xx responses.
    Set LATENCY_MS to add artificial latency to responses.

These environment variables are set in the ECS task definition, allowing
you to deploy a "bad" version by updating the task def without code changes.

Environment Variables:
    ERROR_RATE         — Probability of returning HTTP 500 (default: 0.0)
    LATENCY_MS         — Additional response latency in ms (default: 0)
    CLOUDWATCH_NAMESPACE — Custom metric namespace (default: IntelligentRollback)
    SERVICE_NAME       — Service identifier for metrics (default: sample-app)
    PORT               — Listen port (default: 8080)
"""

from __future__ import annotations

import logging
import os
import random
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)

# Failure injection settings (read at startup, can be changed via task def)
ERROR_RATE = float(os.environ.get("ERROR_RATE", "0.0"))
LATENCY_MS = int(os.environ.get("LATENCY_MS", "0"))

# CloudWatch metric configuration
CLOUDWATCH_NAMESPACE = os.environ.get("CLOUDWATCH_NAMESPACE", "IntelligentRollback")
SERVICE_NAME = os.environ.get("SERVICE_NAME", "sample-app")
PORT = int(os.environ.get("PORT", "8080"))

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# CloudWatch client for publishing metrics
# Uses the task role credentials automatically in ECS
try:
    cloudwatch_client = boto3.client("cloudwatch")
    logger.info("CloudWatch client initialized successfully")
except Exception as exc:
    logger.warning("CloudWatch client init failed (metrics disabled): %s", exc)
    cloudwatch_client = None


# ---------------------------------------------------------------------------
# Metrics Publishing
# ---------------------------------------------------------------------------


def publish_metrics(
    request_count: int = 1,
    error_count: int = 0,
    latency_ms: float = 0.0,
) -> None:
    """Publish custom metrics to CloudWatch.

    We publish three metrics per request:
    - RequestCount: Total number of requests processed
    - ErrorCount: Number of 5xx errors returned
    - Latency: Response time in milliseconds

    These feed the CloudWatch alarm that triggers the rollback flow.
    Using put_metric_data with multiple metrics in one call to reduce API costs.
    """
    if cloudwatch_client is None:
        return

    try:
        cloudwatch_client.put_metric_data(
            Namespace=CLOUDWATCH_NAMESPACE,
            MetricData=[
                {
                    "MetricName": "RequestCount",
                    "Value": request_count,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "ServiceName", "Value": SERVICE_NAME},
                    ],
                },
                {
                    "MetricName": "ErrorCount",
                    "Value": error_count,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "ServiceName", "Value": SERVICE_NAME},
                    ],
                },
                {
                    "MetricName": "Latency",
                    "Value": latency_ms,
                    "Unit": "Milliseconds",
                    "Dimensions": [
                        {"Name": "ServiceName", "Value": SERVICE_NAME},
                    ],
                },
            ],
        )
    except ClientError as exc:
        # Don't fail the request if metrics publishing fails
        logger.warning("Failed to publish CloudWatch metrics: %s", exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint for ALB target group health checks.

    Returns 200 unconditionally — even "bad" deployments should pass
    health checks so CodeDeploy doesn't auto-roll-back before the
    AWS DevOps Agent has a chance to evaluate the situation.
    """
    return jsonify(
        {
            "status": "healthy",
            "service": SERVICE_NAME,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "config": {
                "error_rate": ERROR_RATE,
                "latency_ms": LATENCY_MS,
            },
        }
    ), 200


@app.route("/", methods=["GET"])
def index():
    """Main request endpoint with configurable failure injection.

    Simulates a real microservice endpoint. The ERROR_RATE and LATENCY_MS
    env vars control failure behavior, allowing us to deploy "good" and
    "bad" versions of the same code by changing the task definition.
    """
    start_time = time.time()

    # Inject artificial latency (simulates slow database/dependency)
    if LATENCY_MS > 0:
        time.sleep(LATENCY_MS / 1000.0)

    # Determine if this request should "fail"
    should_error = random.random() < ERROR_RATE

    if should_error:
        # Simulate an internal server error
        elapsed_ms = (time.time() - start_time) * 1000
        publish_metrics(request_count=1, error_count=1, latency_ms=elapsed_ms)
        logger.warning("Injected error (rate=%.2f)", ERROR_RATE)
        return jsonify(
            {
                "error": "Internal Server Error",
                "message": "Simulated failure for rollback demonstration",
                "service": SERVICE_NAME,
            }
        ), 500

    # Successful response
    elapsed_ms = (time.time() - start_time) * 1000
    publish_metrics(request_count=1, error_count=0, latency_ms=elapsed_ms)

    return jsonify(
        {
            "message": "Hello from the sample app!",
            "service": SERVICE_NAME,
            "version": os.environ.get("APP_VERSION", "1.0.0"),
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
            "version": os.environ.get("APP_VERSION", "1.0.0"),
            "config": {
                "error_rate": ERROR_RATE,
                "latency_ms": LATENCY_MS,
                "cloudwatch_namespace": CLOUDWATCH_NAMESPACE,
                "port": PORT,
            },
            "environment": {
                "task_arn": os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "local"),
            },
        }
    ), 200


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Starting %s on port %d (error_rate=%.2f, latency_ms=%d)",
        SERVICE_NAME,
        PORT,
        ERROR_RATE,
        LATENCY_MS,
    )
    # In production, Gunicorn runs the app; this is for local dev only
    app.run(host="0.0.0.0", port=PORT, debug=False)
