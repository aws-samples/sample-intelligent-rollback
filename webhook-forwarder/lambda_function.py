"""
Webhook Forwarder — CloudWatch Alarm → AWS DevOps Agent webhook bridge.

This Lambda is invoked directly by a CloudWatch Alarm action. It transforms
the alarm event into the schema expected by the Amazon Q Developer Agent for
DevOps webhook endpoint, signs the payload with HMAC-SHA256, and POSTs it.

Why a separate Lambda instead of direct SNS → webhook?
  1. The AWS DevOps Agent requires HMAC-signed payloads for authentication
  2. We transform the alarm payload into a richer schema with deployment context
  3. Decouples alarm configuration from agent endpoint changes

Environment Variables:
    WEBHOOK_URL     — AWS DevOps Agent webhook endpoint URL
    WEBHOOK_SECRET  — Shared HMAC secret for payload signing
    AWS_REGION      — AWS region (set automatically by Lambda)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# Optional: enrich the webhook payload with deployment context
CODEDEPLOY_APP_NAME = os.environ.get("CODEDEPLOY_APP_NAME", "sample-app")
CODEDEPLOY_DG_NAME = os.environ.get("CODEDEPLOY_DG_NAME", "sample-app-dg")

codedeploy_client = boto3.client("codedeploy")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_hmac_signature(payload: bytes, secret: str) -> str:
    """Compute HMAC-SHA256 hex digest for webhook authentication.

    Uses the standard `hmac` module with SHA-256. The resulting hex string
    is sent in the X-Hub-Signature-256 header.
    """
    mac = hmac.new(
        key=secret.encode("utf-8"),
        msg=payload,
        digestmod=hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


def _get_deployment_context() -> dict[str, Any]:
    """Fetch current deployment state to enrich the webhook payload.

    This gives the AWS DevOps Agent immediate context about what's deploying,
    so it can make faster decisions without additional API calls.
    """
    try:
        response = codedeploy_client.list_deployments(
            applicationName=CODEDEPLOY_APP_NAME,
            deploymentGroupName=CODEDEPLOY_DG_NAME,
            includeOnlyStatuses=["InProgress", "Queued", "Ready"],
        )
        deployment_ids = response.get("deployments", [])

        if not deployment_ids:
            return {"active_deployment": False}

        # Get details of the most recent active deployment
        deploy_info = codedeploy_client.get_deployment(
            deploymentId=deployment_ids[0]
        )
        info = deploy_info.get("deploymentInfo", {})

        return {
            "active_deployment": True,
            "deployment_id": deployment_ids[0],
            "status": info.get("status", "Unknown"),
            "application_name": CODEDEPLOY_APP_NAME,
            "deployment_group": CODEDEPLOY_DG_NAME,
            "create_time": str(info.get("createTime", "")),
            "description": info.get("description", ""),
        }
    except ClientError as exc:
        logger.warning("Could not fetch deployment context: %s", exc)
        return {"active_deployment": None, "error": str(exc)}


def _transform_alarm_event(alarm_event: dict[str, Any]) -> dict[str, Any]:
    """Transform CloudWatch alarm event into AWS DevOps Agent webhook schema.

    The agent expects a structured alert payload with:
    - alert metadata (source, severity, timestamp)
    - metric details (what triggered the alarm)
    - deployment context (what's currently deploying)
    - recommended actions

    Input format (CloudWatch Alarm → Lambda direct invocation):
    {
        "source": "aws.cloudwatch",
        "detail-type": "CloudWatch Alarm State Change",
        "detail": {
            "alarmName": "...",
            "state": {"value": "ALARM", "reason": "..."},
            "configuration": {...}
        }
    }
    """
    # Extract alarm details — handle both EventBridge and direct formats
    detail = alarm_event.get("detail", alarm_event)
    alarm_name = detail.get("alarmName", alarm_event.get("AlarmName", "Unknown"))
    state = detail.get("state", {})
    new_state = state.get("value", detail.get("NewStateValue", "ALARM"))
    reason = state.get("reason", detail.get("NewStateReason", ""))

    # Extract metric information from alarm configuration
    config = detail.get("configuration", {})
    metrics = config.get("metrics", [])
    metric_info = {}
    if metrics:
        metric_stat = metrics[0].get("metricStat", {})
        metric = metric_stat.get("metric", {})
        metric_info = {
            "namespace": metric.get("namespace", ""),
            "metric_name": metric.get("name", ""),
            "dimensions": metric.get("dimensions", {}),
            "period": metric_stat.get("period", 60),
            "stat": metric_stat.get("stat", "Average"),
        }

    # Enrich with deployment context
    deployment_context = _get_deployment_context()

    # Build the webhook payload
    webhook_payload = {
        "version": "1.0",
        "source": "aws-cloudwatch-alarm",
        "event_type": "deployment_health_alert",
        "timestamp": int(time.time()),
        "alert": {
            "name": alarm_name,
            "state": new_state,
            "reason": reason,
            "severity": "critical" if new_state == "ALARM" else "info",
        },
        "metric": metric_info,
        "deployment": deployment_context,
        "context": {
            "account_id": alarm_event.get("account", ""),
            "region": alarm_event.get("region", os.environ.get("AWS_REGION", "")),
            "source_event_id": alarm_event.get("id", ""),
        },
        "recommended_actions": [
            "Check deployment status",
            "Review error rate metrics",
            "Consider rollback if deployment is causing the issue",
        ],
    }

    return webhook_payload


def _post_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """POST the signed payload to the AWS DevOps Agent webhook endpoint.

    Uses urllib (stdlib) to avoid requiring the `requests` package in Lambda.
    Includes retry logic for transient failures.
    """
    if not WEBHOOK_URL:
        raise ValueError("WEBHOOK_URL environment variable is not configured")
    if not WEBHOOK_SECRET:
        raise ValueError("WEBHOOK_SECRET environment variable is not configured")

    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = _compute_hmac_signature(payload_bytes, WEBHOOK_SECRET)

    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature,
        "X-Webhook-Source": "aws-intelligent-rollback",
        "X-Webhook-Timestamp": str(int(time.time())),
    }

    # Retry up to 3 times with exponential backoff
    max_retries = 3
    for attempt in range(max_retries):
        try:
            req = Request(
                url=WEBHOOK_URL,
                data=payload_bytes,
                headers=headers,
                method="POST",
            )
            with urlopen(req, timeout=10) as response:
                response_body = response.read().decode("utf-8")
                logger.info(
                    "Webhook delivered successfully: status=%d attempt=%d",
                    response.status,
                    attempt + 1,
                )
                return {
                    "status_code": response.status,
                    "body": response_body,
                    "attempt": attempt + 1,
                }
        except HTTPError as exc:
            logger.warning(
                "Webhook HTTP error (attempt %d/%d): %d %s",
                attempt + 1,
                max_retries,
                exc.code,
                exc.reason,
            )
            if attempt == max_retries - 1:
                raise
            # Don't retry 4xx errors (client errors won't fix themselves)
            if 400 <= exc.code < 500:
                raise
        except URLError as exc:
            logger.warning(
                "Webhook connection error (attempt %d/%d): %s",
                attempt + 1,
                max_retries,
                exc.reason,
            )
            if attempt == max_retries - 1:
                raise

        # Exponential backoff: 1s, 2s, 4s
        time.sleep(2**attempt)

    # Should not reach here, but just in case
    raise RuntimeError("Webhook delivery failed after all retries")


# ---------------------------------------------------------------------------
# Lambda Handler
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point — transforms alarm event and forwards to webhook.

    Invoked directly by CloudWatch Alarm action (Lambda target) or via
    EventBridge rule for CloudWatch Alarm State Change events.
    """
    logger.info("Received alarm event: %s", json.dumps(event))

    # Validate configuration
    if not WEBHOOK_URL or not WEBHOOK_SECRET:
        logger.error("Missing required environment variables: WEBHOOK_URL, WEBHOOK_SECRET")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Webhook not configured"}),
        }

    try:
        # Transform the alarm event into webhook schema
        webhook_payload = _transform_alarm_event(event)

        logger.info(
            "Transformed payload: alert=%s deployment_active=%s",
            webhook_payload["alert"]["name"],
            webhook_payload["deployment"].get("active_deployment"),
        )

        # Only forward ALARM state changes (not OK or INSUFFICIENT_DATA)
        alert_state = webhook_payload["alert"]["state"]
        if alert_state != "ALARM":
            logger.info("Skipping non-ALARM state: %s", alert_state)
            return {
                "statusCode": 200,
                "body": json.dumps({"message": f"Skipped — state is {alert_state}"}),
            }

        # Deliver the webhook
        result = _post_webhook(webhook_payload)

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "message": "Webhook delivered successfully",
                    "webhook_status": result["status_code"],
                    "attempt": result["attempt"],
                }
            ),
        }
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": str(exc)}),
        }
    except (HTTPError, URLError) as exc:
        logger.error("Webhook delivery failed: %s", exc)
        return {
            "statusCode": 502,
            "body": json.dumps({"error": f"Webhook delivery failed: {exc}"}),
        }
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Internal error: {exc}"}),
        }
