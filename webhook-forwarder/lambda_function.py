"""
Webhook Forwarder — CloudWatch Alarm → AWS DevOps Agent webhook bridge.

This Lambda is invoked directly by a CloudWatch Alarm action. It transforms
the alarm event into the schema the AWS DevOps Agent webhook endpoint
receives, enriches it with the active CodeDeploy deployment, signs the
payload with HMAC-SHA256, and POSTs it.

Why a separate Lambda instead of direct SNS → webhook?
  1. The AWS DevOps Agent requires HMAC-signed payloads for authentication
  2. We transform the alarm payload into a richer schema with deployment context
  3. Decouples alarm configuration from agent endpoint changes

The outbound payload is described by devops-agent/deployment-metadata-schema.json.

Environment Variables:
    WEBHOOK_URL               — AWS DevOps Agent webhook endpoint URL (https only)
    WEBHOOK_SECRET_PARAMETER  — Name of the SSM SecureString parameter that holds
                                the shared HMAC secret (recommended)
    WEBHOOK_SECRET            — Shared HMAC secret, local testing only; ignored
                                when WEBHOOK_SECRET_PARAMETER is set
    CODEDEPLOY_APP_NAME       — CodeDeploy application to look up (default: sample-app)
    CODEDEPLOY_DG_NAME        — CodeDeploy deployment group (default: sample-app-dg)
    AWS_REGION                — AWS region (set automatically by Lambda)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET_PARAMETER = os.environ.get("WEBHOOK_SECRET_PARAMETER", "")
# Local testing only; ignored when WEBHOOK_SECRET_PARAMETER is set
_WEBHOOK_SECRET_ENV = os.environ.get("WEBHOOK_SECRET", "")

# Deployment context used to enrich the webhook payload
CODEDEPLOY_APP_NAME = os.environ.get("CODEDEPLOY_APP_NAME", "sample-app")
CODEDEPLOY_DG_NAME = os.environ.get("CODEDEPLOY_DG_NAME", "sample-app-dg")

# CodeDeploy statuses that mean a deployment is still in flight
ACTIVE_STATUSES = ["Created", "Queued", "InProgress", "Baking", "Ready"]

# BatchGetDeployments accepts at most 25 IDs per call
BATCH_GET_LIMIT = 25

codedeploy_client = boto3.client("codedeploy")
ssm_client = boto3.client("ssm")

# Cached per Lambda execution environment so SSM is read once per container
_webhook_secret_cache: str | None = None


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


def _get_webhook_secret() -> str:
    """Return the HMAC secret from Parameter Store, or the env var for local tests.

    The value is never logged.
    """
    global _webhook_secret_cache
    if _webhook_secret_cache:
        return _webhook_secret_cache

    if WEBHOOK_SECRET_PARAMETER:
        try:
            response = ssm_client.get_parameter(
                Name=WEBHOOK_SECRET_PARAMETER, WithDecryption=True
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            raise ValueError(
                f"Could not read webhook secret parameter ({code})"
            ) from None
        secret = response.get("Parameter", {}).get("Value", "")
    else:
        secret = _WEBHOOK_SECRET_ENV

    if not secret:
        raise ValueError("Webhook secret is not configured")

    _webhook_secret_cache = secret
    return secret


def _validate_webhook_url() -> None:
    """Require an https webhook URL before any request is made."""
    if not WEBHOOK_URL:
        raise ValueError("WEBHOOK_URL environment variable is not configured")
    if urlparse(WEBHOOK_URL).scheme != "https":
        raise ValueError("WEBHOOK_URL must use https")


def _format_time(value: Any) -> str:
    """Render a CodeDeploy timestamp (datetime from boto3) as ISO 8601."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else ""


def _get_deployment_context() -> dict[str, Any]:
    """Look up the active deployment for the configured deployment group.

    Uses ListDeployments filtered to in-flight statuses, then
    BatchGetDeployments to pick the newest by createTime. This gives the
    AWS DevOps Agent the deployment ID it needs to correlate the alarm with
    a specific deployment without additional API calls.
    """
    base = {
        "application_name": CODEDEPLOY_APP_NAME,
        "deployment_group": CODEDEPLOY_DG_NAME,
    }
    try:
        response = codedeploy_client.list_deployments(
            applicationName=CODEDEPLOY_APP_NAME,
            deploymentGroupName=CODEDEPLOY_DG_NAME,
            includeOnlyStatuses=ACTIVE_STATUSES,
        )
        deployment_ids = response.get("deployments", [])

        if not deployment_ids:
            return {"active_deployment": False, **base}

        batch = codedeploy_client.batch_get_deployments(
            deploymentIds=deployment_ids[:BATCH_GET_LIMIT]
        )
        infos = batch.get("deploymentsInfo", [])
        if not infos:
            return {"active_deployment": False, **base}

        # ListDeployments does not document an ordering, so sort explicitly
        newest = max(
            infos,
            key=lambda info: _format_time(info.get("createTime")),
        )

        return {
            "active_deployment": True,
            "deployment_id": newest.get("deploymentId", ""),
            "status": newest.get("status", "Unknown"),
            **base,
            "create_time": _format_time(newest.get("createTime")),
            "complete_time": _format_time(newest.get("completeTime")),
            "description": newest.get("description", ""),
        }
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.warning("Could not fetch deployment context: %s", code)
        return {"active_deployment": None, **base, "error": code}


def _extract_alarm_detail(event: dict[str, Any]) -> dict[str, Any]:
    """Return the alarm body for the supported event shapes.

    - CloudWatch alarm Lambda action: fields under "alarmData"
    - EventBridge "CloudWatch Alarm State Change": fields under "detail"
    - Legacy/SNS-style: top-level AlarmName / NewStateValue
    """
    return event.get("alarmData") or event.get("detail") or event


def _get_alarm_state(detail: dict[str, Any]) -> str:
    """Return the new alarm state; never default to ALARM."""
    state = detail.get("state") or {}
    return state.get("value") or detail.get("NewStateValue") or "UNKNOWN"


def _parse_reason_data(state: dict[str, Any]) -> dict[str, Any]:
    """Extract threshold and recent datapoints from the reasonData JSON string."""
    result: dict[str, Any] = {
        "threshold": None,
        "recent_datapoints": [],
        "evaluated_period": None,
    }
    try:
        data = json.loads(state.get("reasonData") or "{}")
    except (ValueError, TypeError):
        return result
    if not isinstance(data, dict):
        return result

    threshold = data.get("threshold")
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        result["threshold"] = float(threshold)
    datapoints = data.get("recentDatapoints")
    if isinstance(datapoints, list):
        result["recent_datapoints"] = [
            float(dp)
            for dp in datapoints
            if isinstance(dp, (int, float)) and not isinstance(dp, bool)
        ]
    period = data.get("period")
    if isinstance(period, int) and not isinstance(period, bool):
        result["evaluated_period"] = period
    return result


def _extract_metric_info(detail: dict[str, Any]) -> dict[str, Any]:
    """Describe the metric behind the alarm.

    For metric math alarms the first entry with metricStat names the source
    metric, and the entry with returnData=true carries the expression.
    """
    config = detail.get("configuration") or {}
    metrics = config.get("metrics") or []
    stat_entry = next((m for m in metrics if m.get("metricStat")), None)
    if stat_entry is None:
        return {}

    metric_stat = stat_entry.get("metricStat", {})
    metric = metric_stat.get("metric", {})
    returned = next((m for m in metrics if m.get("returnData")), None)
    expression = returned.get("expression") if returned else None

    return {
        "namespace": metric.get("namespace", ""),
        "metric_name": metric.get("name", ""),
        "dimensions": metric.get("dimensions", {}),
        "period": metric_stat.get("period", 60),
        "stat": metric_stat.get("stat", "Average"),
        "expression": expression,
    }


def _transform_alarm_event(
    alarm_event: dict[str, Any],
    deployment_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transform a CloudWatch alarm event into the AWS DevOps Agent webhook schema.

    The payload carries:
    - alert metadata (name, state, threshold, recent datapoints, timestamp)
    - metric details (what triggered the alarm)
    - deployment context (the active deployment ID and its status)
    - recommended actions

    Input format (CloudWatch Alarm → Lambda direct invocation):
    {
        "source": "aws.cloudwatch",
        "alarmArn": "arn:aws:cloudwatch:...:alarm:...",
        "accountId": "111122223333",
        "time": "...",
        "region": "us-east-1",
        "alarmData": {
            "alarmName": "...",
            "state": {"value": "ALARM", "reason": "...", "reasonData": "{...}",
                      "timestamp": "..."},
            "previousState": {"value": "OK", ...},
            "configuration": {"metrics": [...]}
        }
    }
    """
    detail = _extract_alarm_detail(alarm_event)
    alarm_name = detail.get("alarmName") or alarm_event.get("AlarmName") or "Unknown"
    state = detail.get("state") or {}
    new_state = _get_alarm_state(detail)
    reason = state.get("reason") or detail.get("NewStateReason") or ""
    previous_state = (detail.get("previousState") or {}).get("value") or detail.get(
        "OldStateValue"
    )
    reason_data = _parse_reason_data(state)

    if deployment_context is None:
        deployment_context = _get_deployment_context()

    return {
        "version": "1.0",
        "source": "aws-cloudwatch-alarm",
        "event_type": "deployment_health_alert",
        "timestamp": int(time.time()),
        "alert": {
            "name": alarm_name,
            "arn": alarm_event.get("alarmArn") or None,
            "state": new_state,
            "previous_state": previous_state or None,
            "reason": reason,
            "state_timestamp": state.get("timestamp") or None,
            "threshold": reason_data["threshold"],
            "recent_datapoints": reason_data["recent_datapoints"],
            "severity": "critical" if new_state == "ALARM" else "info",
        },
        "metric": _extract_metric_info(detail),
        "deployment": deployment_context,
        "context": {
            "account_id": alarm_event.get("accountId") or alarm_event.get("account", ""),
            "region": alarm_event.get("region", os.environ.get("AWS_REGION", "")),
            "source_event_id": alarm_event.get("id", ""),
        },
        "recommended_actions": [
            "Check deployment status",
            "Review error rate metrics",
            "Consider rollback if deployment is causing the issue",
        ],
    }


def _post_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """POST the signed payload to the AWS DevOps Agent webhook endpoint.

    Uses urllib (stdlib) to avoid requiring the `requests` package in Lambda.
    Includes retry logic for transient failures.
    """
    _validate_webhook_url()
    secret = _get_webhook_secret()

    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = _compute_hmac_signature(payload_bytes, secret)

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
            # Scheme is checked as https in _validate_webhook_url
            with urlopen(req, timeout=10) as response:  # nosec B310
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

        # Exponential backoff: 1s, 2s
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
    # Alarm events carry no secrets; log them for troubleshooting
    logger.info("Received alarm event: %s", json.dumps(event, default=str))

    try:
        # Only forward ALARM state changes (not OK or INSUFFICIENT_DATA).
        # Checked first so OK transitions make no AWS API calls.
        alert_state = _get_alarm_state(_extract_alarm_detail(event))
        if alert_state != "ALARM":
            logger.info("Skipping non-ALARM state: %s", alert_state)
            return {
                "statusCode": 200,
                "body": json.dumps({"message": f"Skipped — state is {alert_state}"}),
            }

        # Fail fast on configuration before calling CodeDeploy
        _validate_webhook_url()
        _get_webhook_secret()

        # Transform the alarm event into webhook schema (adds deployment context)
        webhook_payload = _transform_alarm_event(event)

        logger.info(
            "Transformed payload: alert=%s deployment_id=%s",
            webhook_payload["alert"]["name"],
            webhook_payload["deployment"].get("deployment_id"),
        )

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
            "body": json.dumps({"error": "Webhook delivery failed"}),
        }
    except Exception:
        logger.exception("Unexpected error")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Internal error"}),
        }
