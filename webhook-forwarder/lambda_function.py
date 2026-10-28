"""
Webhook Forwarder: bridges a CloudWatch alarm to the AWS DevOps Agent generic webhook.

A CloudWatch alarm action invokes this Lambda function asynchronously. The
function looks up the in-flight CodeDeploy deployment, builds the documented
incident envelope, signs it with the Agent Space webhook secret, and POSTs it
to the generic webhook. After a successful delivery it publishes an
[INVESTIGATING] notice to SNS so operators know an investigation is starting.

Signing follows the AWS DevOps Agent generic webhook (HMAC, version 1):
    x-amzn-event-timestamp: <%Y-%m-%dT%H:%M:%S.mmmZ>
    x-amzn-event-signature: base64(HMAC-SHA256(secret, "<timestamp>:<body>"))
https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-invoking-devops-agent-through-webhook.html

The outbound payload is described by devops-agent/deployment-metadata-schema.json.

Delivery and configuration errors raise, so Lambda asynchronous retries apply
and the event finally reaches the on-failure destination. The incidentId is
deterministic per alarm transition, so DevOps Agent deduplicates retries.

Environment Variables:
    WEBHOOK_URL               AWS DevOps Agent webhook endpoint URL (https only)
    WEBHOOK_SECRET_PARAMETER  Name of the SSM SecureString parameter that holds
                              the webhook signing secret (required)
    CODEDEPLOY_APP_NAME       CodeDeploy application to look up (default: sample-app)
    CODEDEPLOY_DG_NAME        CodeDeploy deployment group (default: sample-app-dg)
    SNS_TOPIC_ARN             Topic for the [INVESTIGATING] notice (optional)
    ROLLBACK_WINDOW_MINUTES   Rollback window quoted in the notice (default: 60)
    AWS_REGION                AWS Region (set automatically by Lambda)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)

WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_SECRET_PARAMETER = os.environ.get("WEBHOOK_SECRET_PARAMETER", "")

# Deployment context used to enrich the webhook payload
CODEDEPLOY_APP_NAME = os.environ.get("CODEDEPLOY_APP_NAME", "sample-app")
CODEDEPLOY_DG_NAME = os.environ.get("CODEDEPLOY_DG_NAME", "sample-app-dg")

# [INVESTIGATING] notice
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ROLLBACK_WINDOW_MINUTES = os.environ.get("ROLLBACK_WINDOW_MINUTES", "")
DEFAULT_ROLLBACK_WINDOW_MINUTES = 60

# CodeDeploy statuses that mean a deployment is still in flight
ACTIVE_STATUSES = ["Created", "Queued", "InProgress", "Baking", "Ready"]

# BatchGetDeployments accepts at most 25 IDs per call
BATCH_GET_LIMIT = 25
# Upper bound on deployment IDs scanned across ListDeployments pages
MAX_SCANNED_DEPLOYMENTS = 100
# Only deployments created this recently are considered in flight
LOOKBACK_DAYS = 30

# Webhook envelope limits (see deployment-metadata-schema.json)
MAX_INCIDENT_ID_LENGTH = 128
MAX_ALARM_NAME_IN_ID = 80
MAX_TITLE_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 4000
MAX_UNTRUSTED_TEXT_LENGTH = 500

# Delivery retry policy: 3 attempts, 1 s then 2 s backoff, no retry on 4xx
MAX_ATTEMPTS = 3
REQUEST_TIMEOUT_SECONDS = 10

_INCIDENT_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9-]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_WHITESPACE = re.compile(r"\s+")

codedeploy_client = boto3.client("codedeploy")
ssm_client = boto3.client("ssm")
sns_client = boto3.client("sns")

# Cached per Lambda execution environment so SSM is read once per container
_webhook_secret_cache: str | None = None


class WebhookDeliveryError(RuntimeError):
    """The webhook could not be delivered. The message never contains the URL."""


# ---------------------------------------------------------------------------
# Signing and secrets
# ---------------------------------------------------------------------------


def _event_timestamp() -> str:
    """Return the current UTC time as %Y-%m-%dT%H:%M:%S.mmmZ."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _compute_hmac_signature(timestamp: str, body: bytes, secret: str) -> str:
    """Return base64(HMAC-SHA256(secret, timestamp + ":" + body)).

    The value is sent in the x-amzn-event-signature header. The timestamp is
    the same value sent in x-amzn-event-timestamp.
    """
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b":" + body,
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def _get_webhook_secret() -> str:
    """Return the signing secret from Parameter Store. The value is never logged."""
    global _webhook_secret_cache
    if _webhook_secret_cache:
        return _webhook_secret_cache

    if not WEBHOOK_SECRET_PARAMETER:
        raise ValueError("WEBHOOK_SECRET_PARAMETER is not configured")

    try:
        response = ssm_client.get_parameter(
            Name=WEBHOOK_SECRET_PARAMETER, WithDecryption=True
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        raise ValueError(f"Could not read webhook secret parameter ({code})") from None
    secret = response.get("Parameter", {}).get("Value", "")

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


def _webhook_host() -> str:
    """Return only the host of the webhook URL, for logs (the path is a credential)."""
    return urlparse(WEBHOOK_URL).hostname or "unknown"


def _rollback_window_minutes() -> int:
    """Parse ROLLBACK_WINDOW_MINUTES; invalid or missing values fall back to 60."""
    try:
        value = int(ROLLBACK_WINDOW_MINUTES)
    except ValueError:
        value = 0
    if value <= 0:
        logger.warning(
            "ROLLBACK_WINDOW_MINUTES is missing or invalid; using %d",
            DEFAULT_ROLLBACK_WINDOW_MINUTES,
        )
        return DEFAULT_ROLLBACK_WINDOW_MINUTES
    return value


# ---------------------------------------------------------------------------
# Text and time helpers
# ---------------------------------------------------------------------------


def _sanitize_text(value: Any, limit: int = MAX_UNTRUSTED_TEXT_LENGTH) -> str:
    """Flatten untrusted text: control characters become spaces, whitespace
    collapses, and the result is truncated to ``limit`` characters with '...'."""
    text = _CONTROL_CHARS.sub(" ", str(value or ""))
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _quote(text: str) -> str:
    """Wrap text in double quotes, escaping backslashes and inner quotes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_time(value: Any) -> datetime | None:
    """Parse a datetime or an ISO 8601 string (including '+0000' offsets) to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    """Render a UTC datetime as %Y-%m-%dT%H:%M:%S.mmmZ."""
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _compact_utc(value: datetime) -> str:
    """Render a UTC datetime as YYYYMMDDTHHMMSSmmmZ (used in incidentId)."""
    return value.strftime("%Y%m%dT%H%M%S") + f"{value.microsecond // 1000:03d}Z"


def _format_time(value: Any, missing: str = "unknown") -> str:
    parsed = _parse_time(value)
    return _iso_utc(parsed) if parsed else missing


def _format_number(value: float) -> str:
    return f"{value:g}"


# ---------------------------------------------------------------------------
# Deployment lookup
# ---------------------------------------------------------------------------


def _get_deployment_context() -> dict[str, Any]:
    """Look up the newest in-flight deployment for the configured deployment group.

    Pages through ListDeployments (in-flight statuses, created in the last 30
    days, at most 100 IDs), then BatchGetDeployments in chunks of 25, and picks
    the newest by createTime. ``active_deployment`` is True, False, or None when
    the lookup failed.
    """
    base = {
        "application_name": CODEDEPLOY_APP_NAME,
        "deployment_group": CODEDEPLOY_DG_NAME,
    }
    try:
        deployment_ids: list[str] = []
        paginator = codedeploy_client.get_paginator("list_deployments")
        pages = paginator.paginate(
            applicationName=CODEDEPLOY_APP_NAME,
            deploymentGroupName=CODEDEPLOY_DG_NAME,
            includeOnlyStatuses=ACTIVE_STATUSES,
            createTimeRange={
                "start": datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
            },
        )
        for page in pages:
            deployment_ids.extend(page.get("deployments", []))
            if len(deployment_ids) >= MAX_SCANNED_DEPLOYMENTS:
                break
        deployment_ids = deployment_ids[:MAX_SCANNED_DEPLOYMENTS]

        infos: list[dict[str, Any]] = []
        for start in range(0, len(deployment_ids), BATCH_GET_LIMIT):
            batch = codedeploy_client.batch_get_deployments(
                deploymentIds=deployment_ids[start : start + BATCH_GET_LIMIT]
            )
            infos.extend(batch.get("deploymentsInfo", []))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        logger.warning("Could not fetch deployment context: %s", code)
        return {"active_deployment": None, **base, "error": code}
    except BotoCoreError as exc:
        logger.warning("Could not fetch deployment context: %s", type(exc).__name__)
        return {"active_deployment": None, **base, "error": type(exc).__name__}

    if not infos:
        return {"active_deployment": False, **base}

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    newest = max(infos, key=lambda info: _parse_time(info.get("createTime")) or epoch)
    return {
        "active_deployment": True,
        "deployment_id": newest.get("deploymentId", ""),
        "status": newest.get("status", "unknown"),
        **base,
        "create_time": newest.get("createTime"),
        "complete_time": newest.get("completeTime"),
        "description": newest.get("description", ""),
    }


# ---------------------------------------------------------------------------
# Alarm parsing
# ---------------------------------------------------------------------------


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
    """Extract threshold, recent datapoints, and period from the reasonData JSON."""
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


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def _build_incident_id(
    alarm_name: str,
    state_time: datetime | None,
    event_id: str,
    request_id: str | None,
) -> str:
    """Deterministic per alarm transition so retries of the same event dedupe."""
    name = _INCIDENT_ID_DISALLOWED.sub("-", alarm_name)[:MAX_ALARM_NAME_IN_ID] or "alarm"
    if state_time is not None:
        suffix = _compact_utc(state_time)
    elif event_id:
        suffix = event_id
    elif request_id:
        suffix = request_id
    else:
        suffix = _compact_utc(datetime.now(timezone.utc))
    incident_id = _INCIDENT_ID_DISALLOWED.sub("-", f"{name}-{suffix}")
    return incident_id[:MAX_INCIDENT_ID_LENGTH]


def _describe_metric(metric: dict[str, Any]) -> str:
    if not metric:
        return "unknown"
    dims = " ".join(
        f"{key}={value}" for key, value in sorted((metric.get("dimensions") or {}).items())
    )
    target = f"{metric.get('namespace', '')}/{metric.get('metric_name', '')}"
    if dims:
        target = f"{target} {dims}"
    return f"{target}, stat {metric.get('stat')}, period {metric.get('period')}s"


def _transform_alarm_event(
    alarm_event: dict[str, Any],
    deployment_context: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the AWS DevOps Agent generic webhook incident envelope.

    Only title, description, priority, and the incident reference reach the
    agent, so all evidence goes into fixed-order ``Key: value`` lines in
    ``description``. The alarm reason and deployment description are untrusted
    and are sanitized.

    Input format (CloudWatch alarm action, Lambda target):
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
    raw_name = detail.get("alarmName") or alarm_event.get("AlarmName") or "Unknown"
    alarm_name = _sanitize_text(raw_name, 255) or "Unknown"
    state = detail.get("state") or {}
    new_state = _get_alarm_state(detail)
    reason = state.get("reason") or detail.get("NewStateReason") or ""
    previous_state = (detail.get("previousState") or {}).get("value") or detail.get(
        "OldStateValue"
    )
    reason_data = _parse_reason_data(state)
    metric = _extract_metric_info(detail)

    resources = alarm_event.get("resources") or []
    alarm_arn = alarm_event.get("alarmArn") or (resources[0] if resources else "")

    state_time = _parse_time(state.get("timestamp") or detail.get("StateChangeTime"))
    event_time = state_time or _parse_time(alarm_event.get("time"))
    timestamp = _iso_utc(event_time or datetime.now(timezone.utc))

    if deployment_context is None:
        deployment_context = _get_deployment_context()
    active = deployment_context.get("active_deployment")
    deployment_id = deployment_context.get("deployment_id") or ""
    app_name = deployment_context.get("application_name", CODEDEPLOY_APP_NAME)
    group_name = deployment_context.get("deployment_group", CODEDEPLOY_DG_NAME)
    scope = f"({app_name}/{group_name})"

    if active is True:
        in_flight = "yes"
        title = f"{alarm_name} in ALARM during deployment {deployment_id} {scope}"
    elif active is False:
        in_flight = "no"
        title = f"{alarm_name} in ALARM, no deployment in flight {scope}"
    else:
        in_flight = f"lookup failed ({deployment_context.get('error', 'Unknown')})"
        title = f"{alarm_name} in ALARM, deployment lookup failed {scope}"
    priority = "MEDIUM" if active is False else "HIGH"

    threshold = reason_data["threshold"]
    datapoints = reason_data["recent_datapoints"]
    period = reason_data["evaluated_period"] or metric.get("period")
    account = alarm_event.get("accountId") or alarm_event.get("account") or "unknown"
    region = alarm_event.get("region") or os.environ.get("AWS_REGION", "") or "unknown"
    deployment_description = _sanitize_text(deployment_context.get("description"))

    lines = [
        f"Alarm: {alarm_name}",
        f"Alarm ARN: {alarm_arn or 'unknown'}",
        f"State: {new_state} (previous: {previous_state or 'unknown'})",
        f"State changed at: {_format_time(state_time)}",
        f"Alarm reason (from CloudWatch): {_sanitize_text(reason)}",
        f"Metric: {_describe_metric(metric)}",
        f"Metric expression: {metric.get('expression') or 'none'}",
        f"Threshold: {_format_number(threshold) if threshold is not None else 'unknown'}",
        "Recent datapoints: "
        + (", ".join(_format_number(dp) for dp in datapoints) if datapoints else "none"),
        f"Evaluation period: {period} seconds" if period else "Evaluation period: unknown",
        f"Deployment in flight: {in_flight}",
        f"Deployment ID: {deployment_id or 'none'}",
        f"Deployment status: {deployment_context.get('status') or 'unknown'}",
        f"Deployment created at: {_format_time(deployment_context.get('create_time'))}",
        "Deployment completed at: "
        + _format_time(deployment_context.get("complete_time"), missing="not completed"),
        f"Application / deployment group: {app_name} / {group_name}",
        f"Account / Region: {account} / {region}",
        "Deployment description (untrusted, set by whoever created the deployment): "
        + _quote(deployment_description),
    ]

    return {
        "eventType": "incident",
        "incidentId": _build_incident_id(
            str(raw_name), state_time, str(alarm_event.get("id") or ""), request_id
        ),
        "action": "created",
        "priority": priority,
        "title": title[:MAX_TITLE_LENGTH],
        "description": "\n".join(lines)[:MAX_DESCRIPTION_LENGTH],
        "timestamp": timestamp,
        "service": CODEDEPLOY_APP_NAME,
    }


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _post_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """POST the signed payload to the AWS DevOps Agent webhook endpoint.

    Every attempt serializes the body, takes a fresh timestamp, and re-signs,
    so the bytes signed are exactly the bytes sent. Three attempts with 1 s
    and 2 s backoff; 4xx responses are not retried. Raises WebhookDeliveryError
    when delivery fails.
    """
    _validate_webhook_url()
    secret = _get_webhook_secret()
    host = _webhook_host()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        timestamp = _event_timestamp()
        headers = {
            "Content-Type": "application/json",
            "x-amzn-event-timestamp": timestamp,
            "x-amzn-event-signature": _compute_hmac_signature(timestamp, body, secret),
        }
        try:
            req = Request(url=WEBHOOK_URL, data=body, headers=headers, method="POST")
            # Scheme is checked as https in _validate_webhook_url
            with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
                response.read()
                logger.info(
                    "Webhook delivered: host=%s status=%d attempt=%d",
                    host,
                    response.status,
                    attempt,
                )
                return {"status_code": response.status, "attempt": attempt}
        except HTTPError as exc:
            logger.warning(
                "Webhook HTTP error: host=%s attempt=%d/%d status=%d reason=%s",
                host,
                attempt,
                MAX_ATTEMPTS,
                exc.code,
                exc.reason,
            )
            # Client errors will not fix themselves
            if 400 <= exc.code < 500 or attempt == MAX_ATTEMPTS:
                raise WebhookDeliveryError(
                    f"Webhook delivery failed: HTTP {exc.code}"
                ) from None
        except (URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            logger.warning(
                "Webhook connection error: host=%s attempt=%d/%d reason=%s",
                host,
                attempt,
                MAX_ATTEMPTS,
                reason,
            )
            if attempt == MAX_ATTEMPTS:
                raise WebhookDeliveryError(
                    "Webhook delivery failed: connection error"
                ) from None

        # Exponential backoff: 1 s, 2 s
        time.sleep(2 ** (attempt - 1))

    raise WebhookDeliveryError("Webhook delivery failed after all retries")


def _publish_investigating(alarm_name: str, deployment_id: str) -> None:
    """Tell operators an investigation started. Failures are logged, never raised."""
    if not SNS_TOPIC_ARN:
        logger.warning("SNS_TOPIC_ARN is not configured; skipping [INVESTIGATING] notice")
        return

    window = _rollback_window_minutes()
    message = (
        f"Alarm: {alarm_name}\n"
        f"Deployment: {deployment_id or 'none in flight'}\n\n"
        "Open the investigation in the AWS DevOps Agent Operator Web App. "
        "The agent will recommend roll back, hold, or escalate. "
        "Approve or reject any proposed action before the deployment's rollback "
        f"window closes (about {window} minutes after traffic shifting completes)."
    )
    subject = f"[INVESTIGATING] {CODEDEPLOY_APP_NAME} alarm sent to AWS DevOps Agent"
    try:
        sns_client.publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)
    except (ClientError, BotoCoreError) as exc:
        code = (
            exc.response.get("Error", {}).get("Code", "Unknown")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
        logger.warning("Could not publish [INVESTIGATING] notice: %s", code)


# ---------------------------------------------------------------------------
# Lambda Handler
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point: transform the alarm event and forward it to the webhook.

    Invoked asynchronously by a CloudWatch alarm action (Lambda target) or by an
    EventBridge rule for CloudWatch Alarm State Change events. Raises on
    configuration and delivery errors so Lambda retries the event.
    """
    # Alarm events carry no secrets; log them for troubleshooting
    logger.info("Received alarm event: %s", json.dumps(event, default=str))

    # Only forward ALARM state changes (not OK or INSUFFICIENT_DATA).
    # Checked first so OK transitions make no AWS API calls.
    alert_state = _get_alarm_state(_extract_alarm_detail(event))
    if alert_state != "ALARM":
        logger.info("Skipping non-ALARM state: %s", alert_state)
        return {
            "statusCode": 200,
            "body": json.dumps({"message": f"Skipped: state is {alert_state}"}),
        }

    try:
        # Fail fast on configuration before calling CodeDeploy
        _validate_webhook_url()
        _get_webhook_secret()

        deployment_context = _get_deployment_context()
        webhook_payload = _transform_alarm_event(
            event,
            deployment_context=deployment_context,
            request_id=getattr(context, "aws_request_id", None),
        )
        deployment_id = deployment_context.get("deployment_id") or ""
        logger.info(
            "Built incident: incidentId=%s priority=%s deployment_id=%s",
            webhook_payload["incidentId"],
            webhook_payload["priority"],
            deployment_id or "none",
        )

        result = _post_webhook(webhook_payload)
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        raise WebhookDeliveryError(f"Configuration error: {exc}") from exc
    except WebhookDeliveryError as exc:
        logger.error("%s", exc)
        raise
    except Exception:
        logger.exception("Unexpected error")
        raise

    alarm_name = _sanitize_text(
        _extract_alarm_detail(event).get("alarmName") or event.get("AlarmName"), 255
    )
    _publish_investigating(alarm_name or "Unknown", deployment_id)

    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "message": "Webhook delivered successfully",
                "webhook_status": result["status_code"],
                "attempt": result["attempt"],
                "incident_id": webhook_payload["incidentId"],
            }
        ),
    }
