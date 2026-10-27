"""
MCP Rollback Tool — AWS Lambda function for the AWS DevOps Agent.

This Lambda implements a Model Context Protocol (MCP) tool that the AWS DevOps
Agent can invoke to manage CodeDeploy deployments. It supports five actions:

    rollback  Stop the deployment with autoRollbackEnabled=True
    stop      Stop the deployment without rollback (operator request only)
    status    Describe one deployment, or the newest one for the group
    list      List recent deployments for the group
    notify    Publish the agent's hold or escalate reasoning to SNS without
              changing the deployment

Every input is validated before any CodeDeploy call: the deployment ID format,
that the deployment belongs to the configured application and deployment
group, and (for rollback/stop) that the deployment is still active. Errors are
returned as validation (400), transient (503, retryable) or permanent (500).

The input contract is described by devops-agent/mcp-tool-definition.json.

Architecture:
    AWS DevOps Agent → Lambda (this) → CodeDeploy API → SNS notification

Environment Variables:
    CONFIG_PARAMETER_NAME — SSM Parameter Store parameter holding a JSON object
                            with application_name, deployment_group and
                            sns_topic_arn (recommended)
    CODEDEPLOY_APP_NAME   — Fallback CodeDeploy application name
    CODEDEPLOY_DG_NAME    — Fallback CodeDeploy deployment group name
    SNS_TOPIC_ARN         — Fallback SNS topic for operator notifications
    AWS_REGION            — AWS region (set automatically by Lambda)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any, Callable

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CONFIG_PARAMETER_NAME = os.environ.get("CONFIG_PARAMETER_NAME", "")
ENV_APP_NAME = os.environ.get("CODEDEPLOY_APP_NAME", "sample-app")
ENV_DG_NAME = os.environ.get("CODEDEPLOY_DG_NAME", "sample-app-dg")
ENV_SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

CONFIG_TTL_SECONDS = 300

# CodeDeploy statuses that mean a deployment is still in flight
ACTIVE_STATUSES = ["Created", "Queued", "InProgress", "Baking", "Ready"]

# Input rules (mirrored in devops-agent/mcp-tool-definition.json). Checked
# with fullmatch so a trailing newline is rejected, as JSON Schema does.
DEPLOYMENT_ID_PATTERN = re.compile(r"^d-[A-Z0-9]{9}$")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._+=,@-]{1,100}$")
MAX_REASON_LENGTH = 1000
MAX_LIST_RESULTS = 25
DEFAULT_LIST_RESULTS = 10
BATCH_GET_LIMIT = 25
NOTIFY_DECISIONS = ("hold", "escalate")
ALLOWED_PARAMS = {
    "action",
    "application_name",
    "deployment_group",
    "deployment_id",
    "reason",
    "decision",
    "max_results",
}

DEFAULT_ROLLBACK_REASON = "Automated rollback triggered by AWS DevOps Agent"

# Error codes that are worth retrying after a short wait
TRANSIENT_ERROR_CODES = {
    "ThrottlingException",
    "Throttling",
    "TooManyRequestsException",
    "RequestLimitExceeded",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "InternalFailure",
    "InternalError",
    "RequestTimeout",
    "RequestTimeoutException",
}
TRANSIENT_EXCEPTIONS = (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError)

# AWS SDK clients (reused across warm invocations)
codedeploy_client = boto3.client(
    "codedeploy", config=Config(retries={"mode": "standard", "max_attempts": 3})
)
sns_client = boto3.client("sns")
ssm_client = boto3.client("ssm")

_config_cache: dict[str, Any] = {"value": None, "loaded_at": 0.0}


class ValidationError(Exception):
    """Raised when the agent supplies input outside the tool contract."""

    def __init__(self, message: str, error_code: str = "InvalidParameter") -> None:
        super().__init__(message)
        self.error_code = error_code


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config() -> dict[str, str]:
    """Return tool configuration, cached per container for CONFIG_TTL_SECONDS.

    Reads the JSON parameter named by CONFIG_PARAMETER_NAME from Parameter
    Store. Missing keys, a missing parameter, or a read error fall back to the
    environment variables so the tool keeps working.
    """
    now = time.monotonic()
    cached = _config_cache["value"]
    if cached is not None and now - _config_cache["loaded_at"] < CONFIG_TTL_SECONDS:
        return cached

    stored: dict[str, Any] = {}
    if CONFIG_PARAMETER_NAME:
        try:
            response = ssm_client.get_parameter(Name=CONFIG_PARAMETER_NAME)
            value = json.loads(response.get("Parameter", {}).get("Value", "{}"))
            if isinstance(value, dict):
                stored = value
            else:
                logger.warning("Config parameter is not a JSON object; using env fallback")
        except ClientError as exc:
            logger.warning(
                "Could not read config parameter (%s); using env fallback",
                exc.response.get("Error", {}).get("Code", "Unknown"),
            )
        except ValueError:
            logger.warning("Config parameter is not valid JSON; using env fallback")

    config = {
        "application_name": str(stored.get("application_name") or ENV_APP_NAME),
        "deployment_group": str(stored.get("deployment_group") or ENV_DG_NAME),
        "sns_topic_arn": str(stored.get("sns_topic_arn") or ENV_SNS_TOPIC_ARN),
    }
    _config_cache["value"] = config
    _config_cache["loaded_at"] = now
    return config


def _classify_error(exc: Exception) -> tuple[str, str, bool]:
    """Return (error_code, error_type, retryable) for an AWS SDK exception."""
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        if code in TRANSIENT_ERROR_CODES:
            return code, "transient", True
        return code, "permanent", False
    if isinstance(exc, TRANSIENT_EXCEPTIONS):
        return type(exc).__name__, "transient", True
    return type(exc).__name__, "permanent", False


def _format_time(value: Any) -> str:
    """Render a CodeDeploy timestamp (datetime from boto3) as ISO 8601."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else ""


def _publish_notification(subject: str, message: str) -> str | None:
    """Publish an operational notification to SNS.

    Returns None on success, or an error code. Callers decide whether a
    failed publish fails the action (it does for notify, not for rollback).
    """
    topic_arn = _load_config()["sns_topic_arn"]
    if not topic_arn:
        logger.warning("SNS topic not configured; skipping notification")
        return "NotificationTopicNotConfigured"

    try:
        sns_client.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],  # SNS subject max 100 chars
            Message=message,
        )
        logger.info("Notification published")
        return None
    except (ClientError, BotoCoreError) as exc:
        code, _, _ = _classify_error(exc)
        logger.error("Failed to publish SNS notification: %s", code)
        return code


def _build_response(
    success: bool,
    action: str,
    message: str,
    details: dict[str, Any] | None = None,
    status_code: int | None = None,
) -> dict[str, Any]:
    """Build a structured JSON response for the AWS DevOps Agent.

    The agent expects a consistent schema so it can parse results
    and make subsequent decisions.
    """
    if status_code is None:
        status_code = 200 if success else 500
    return {
        "statusCode": status_code,
        "body": json.dumps(
            {
                "success": success,
                "action": action,
                "message": message,
                "details": details or {},
                "timestamp": int(time.time()),
            }
        ),
    }


def _error_response(
    action: str,
    message: str,
    error_code: str,
    error_type: str,
    deployment_id: str | None = None,
) -> dict[str, Any]:
    """Build an error response with error_code, error_type and retryable."""
    retryable = error_type == "transient"
    details: dict[str, Any] = {
        "error_code": error_code,
        "error_type": error_type,
        "retryable": retryable,
    }
    if deployment_id:
        details["deployment_id"] = deployment_id
    status_code = {"validation": 400, "transient": 503}.get(error_type, 500)
    return _build_response(False, action, message, details, status_code)


def _aws_error_response(
    action: str, prefix: str, exc: Exception, deployment_id: str | None = None
) -> dict[str, Any]:
    code, error_type, _ = _classify_error(exc)
    logger.error("%s: %s (%s)", prefix, code, error_type)
    return _error_response(action, f"{prefix}: {code}", code, error_type, deployment_id)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_params(action: str, params: dict[str, Any]) -> None:
    """Check every supplied field against the tool contract."""
    unknown = sorted(set(params) - ALLOWED_PARAMS)
    if unknown:
        raise ValidationError(f"Unknown parameter(s): {', '.join(unknown)}")

    for field in ("application_name", "deployment_group"):
        if field in params:
            value = params[field]
            if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value):
                raise ValidationError(f"{field} has an invalid format")

    if "deployment_id" in params:
        value = params["deployment_id"]
        if not isinstance(value, str) or not DEPLOYMENT_ID_PATTERN.fullmatch(value):
            raise ValidationError(
                "deployment_id must match d-XXXXXXXXX (9 uppercase letters or digits)"
            )

    if "reason" in params:
        value = params["reason"]
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("reason must be a non-empty string")
        if len(value) > MAX_REASON_LENGTH:
            raise ValidationError(f"reason must be at most {MAX_REASON_LENGTH} characters")

    if "decision" in params and params["decision"] not in NOTIFY_DECISIONS:
        raise ValidationError("decision must be one of: hold, escalate")

    if "max_results" in params:
        value = params["max_results"]
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 1 <= value <= MAX_LIST_RESULTS
        ):
            raise ValidationError(f"max_results must be an integer from 1 to {MAX_LIST_RESULTS}")

    if action == "notify":
        if "reason" not in params:
            raise ValidationError("reason is required for notify")
        if "decision" not in params:
            raise ValidationError("decision is required for notify")


def _resolve_target(params: dict[str, Any]) -> tuple[str, str]:
    """Return the (application, deployment group) this call may act on.

    Overrides are accepted only when they equal the configured pair, so the
    agent cannot point the tool at another team's deployment group.
    """
    config = _load_config()
    app_name = params.get("application_name", config["application_name"])
    dg_name = params.get("deployment_group", config["deployment_group"])
    if app_name != config["application_name"] or dg_name != config["deployment_group"]:
        raise ValidationError(
            f"Deployment group {app_name}/{dg_name} is not managed by this tool",
            error_code="DeploymentGroupNotManaged",
        )
    return app_name, dg_name


def _validate_deployment(
    deployment_id: str, app_name: str, dg_name: str, require_active: bool
) -> dict[str, Any]:
    """Confirm the deployment belongs to the group (and is active if required).

    Returns the deploymentInfo from GetDeployment. AWS errors propagate.
    """
    info = codedeploy_client.get_deployment(deploymentId=deployment_id).get(
        "deploymentInfo", {}
    )
    if info.get("applicationName") != app_name or info.get("deploymentGroupName") != dg_name:
        raise ValidationError(
            f"Deployment {deployment_id} does not belong to {app_name}/{dg_name}",
            error_code="DeploymentGroupMismatch",
        )
    status = info.get("status", "Unknown")
    if require_active and status not in ACTIVE_STATUSES:
        raise ValidationError(
            f"Deployment {deployment_id} is not active, current status {status}",
            error_code="DeploymentNotActive",
        )
    return info


def _newest(infos: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not infos:
        return None
    return max(infos, key=lambda info: _format_time(info.get("createTime")))


def _get_deployment_infos(
    app_name: str, dg_name: str, statuses: list[str] | None = None
) -> list[dict[str, Any]]:
    """List deployments for the group and return their details, newest first."""
    kwargs: dict[str, Any] = {"applicationName": app_name, "deploymentGroupName": dg_name}
    if statuses:
        kwargs["includeOnlyStatuses"] = statuses
    deployment_ids = codedeploy_client.list_deployments(**kwargs).get("deployments", [])
    if not deployment_ids:
        return []
    infos = codedeploy_client.batch_get_deployments(
        deploymentIds=deployment_ids[:BATCH_GET_LIMIT]
    ).get("deploymentsInfo", [])
    # ListDeployments does not document an ordering, so sort explicitly
    return sorted(infos, key=lambda info: _format_time(info.get("createTime")), reverse=True)


def _get_active_deployment(app_name: str, dg_name: str) -> dict[str, Any] | None:
    """Return the newest in-flight deployment for the group, or None."""
    return _newest(_get_deployment_infos(app_name, dg_name, ACTIVE_STATUSES))


def _find_target_deployment(
    action: str, params: dict[str, Any], app_name: str, dg_name: str
) -> dict[str, Any]:
    """Validate the requested deployment, or find the active one."""
    deployment_id = params.get("deployment_id")
    if deployment_id:
        return _validate_deployment(deployment_id, app_name, dg_name, require_active=True)
    info = _get_active_deployment(app_name, dg_name)
    if info is None:
        raise ValidationError(
            f"No active deployment found for {app_name}/{dg_name}",
            error_code="NoActiveDeployment",
        )
    return info


# ---------------------------------------------------------------------------
# Action Handlers
# ---------------------------------------------------------------------------


def handle_rollback(params: dict[str, Any]) -> dict[str, Any]:
    """Stop the active deployment and trigger automatic rollback.

    This is the primary action the AWS DevOps Agent invokes when it determines
    a deployment is causing issues. We use StopDeployment with
    autoRollbackEnabled=True so CodeDeploy reverts traffic to the
    previous stable task set.
    """
    app_name, dg_name = _resolve_target(params)
    reason = params.get("reason", DEFAULT_ROLLBACK_REASON).strip()
    deployment_id = params.get("deployment_id")

    try:
        # Step 1: Validate the deployment ID and group, or find the active one
        info = _find_target_deployment("rollback", params, app_name, dg_name)
        deployment_id = info.get("deploymentId", deployment_id)

        # Step 2: Stop the deployment with auto-rollback enabled
        response = codedeploy_client.stop_deployment(
            deploymentId=deployment_id,
            autoRollbackEnabled=True,
        )
    except (ClientError, BotoCoreError) as exc:
        code, error_type, retryable = _classify_error(exc)
        logger.error("Rollback failed: %s (%s)", code, error_type)

        # Notify operators even on failure so they can intervene manually
        _publish_notification(
            subject=f"[FAILED] Rollback attempt for {app_name}",
            message=(
                f"Automated rollback FAILED for deployment {deployment_id or 'unknown'}.\n"
                f"Error: {code} ({error_type}, retryable={retryable})\n"
                f"Reason: {reason}\n\n"
                "Manual intervention may be required."
            ),
        )
        return _error_response(
            "rollback", f"Failed to stop deployment: {code}", code, error_type, deployment_id
        )

    status = response.get("status", "Unknown")
    status_message = response.get("statusMessage", "")
    logger.info("StopDeployment called: deployment_id=%s status=%s", deployment_id, status)

    # Step 3: Notify operators of successful rollback initiation
    _publish_notification(
        subject=f"[ROLLBACK] {app_name} deployment rolled back",
        message=(
            f"Deployment {deployment_id} has been stopped and rollback initiated.\n\n"
            f"Application: {app_name}\n"
            f"Deployment Group: {dg_name}\n"
            f"Status: {status}\n"
            f"Reason: {reason}\n\n"
            "Traffic is being reverted to the previous stable version."
        ),
    )

    return _build_response(
        success=True,
        action="rollback",
        message=f"Rollback initiated for deployment {deployment_id}",
        details={
            "deployment_id": deployment_id,
            "status": status,
            "status_message": status_message,
            "application_name": app_name,
            "deployment_group": dg_name,
            "reason": reason,
        },
    )


def handle_stop(params: dict[str, Any]) -> dict[str, Any]:
    """Stop the active deployment WITHOUT rollback.

    Use this when an operator wants to halt a deployment but keep the
    current (new) task set active, for example for investigation.
    """
    app_name, dg_name = _resolve_target(params)
    deployment_id = params.get("deployment_id")

    try:
        info = _find_target_deployment("stop", params, app_name, dg_name)
        deployment_id = info.get("deploymentId", deployment_id)
        response = codedeploy_client.stop_deployment(
            deploymentId=deployment_id,
            autoRollbackEnabled=False,  # Stop only, no rollback
        )
    except (ClientError, BotoCoreError) as exc:
        return _aws_error_response("stop", "Failed to stop deployment", exc, deployment_id)

    status = response.get("status", "Unknown")
    logger.info("Deployment %s stopped (no rollback): status=%s", deployment_id, status)

    _publish_notification(
        subject=f"[STOPPED] {app_name} deployment halted",
        message=(
            f"Deployment {deployment_id} has been stopped (no rollback).\n"
            f"Application: {app_name}\n"
            f"Status: {status}\n"
        ),
    )

    return _build_response(
        success=True,
        action="stop",
        message=f"Deployment {deployment_id} stopped without rollback",
        details={"deployment_id": deployment_id, "status": status},
    )


def handle_status(params: dict[str, Any]) -> dict[str, Any]:
    """Get the current deployment status for a deployment group.

    The agent uses this to understand the current state before deciding
    whether to roll back.
    """
    app_name, dg_name = _resolve_target(params)
    deployment_id = params.get("deployment_id")

    try:
        if deployment_id:
            info = _validate_deployment(deployment_id, app_name, dg_name, require_active=False)
        else:
            # No deployment_id specified: use the newest deployment
            info = _newest(_get_deployment_infos(app_name, dg_name))
            if info is None:
                return _build_response(
                    success=True,
                    action="status",
                    message=f"No deployments found for {app_name}/{dg_name}",
                    details={"has_deployments": False},
                )
            deployment_id = info.get("deploymentId")
    except (ClientError, BotoCoreError) as exc:
        return _aws_error_response(
            "status", "Failed to get deployment status", exc, deployment_id
        )

    details = {
        "deployment_id": deployment_id,
        "status": info.get("status", "Unknown"),
        "create_time": _format_time(info.get("createTime")),
        "complete_time": _format_time(info.get("completeTime")),
        "description": info.get("description", ""),
        "creator": info.get("creator", ""),
        "rollback_info": info.get("rollbackInfo", {}),
        "error_information": info.get("errorInformation", {}),
        "deployment_overview": info.get("deploymentOverview", {}),
    }

    logger.info("Status retrieved for deployment %s: %s", deployment_id, details["status"])

    return _build_response(
        success=True,
        action="status",
        message=f"Deployment {deployment_id} status: {details['status']}",
        details=details,
    )


def handle_list(params: dict[str, Any]) -> dict[str, Any]:
    """List recent deployments for the deployment group, newest first.

    Provides the agent with historical context to make better decisions
    (e.g., detecting repeated failures).
    """
    app_name, dg_name = _resolve_target(params)
    max_results = params.get("max_results", DEFAULT_LIST_RESULTS)

    try:
        infos = _get_deployment_infos(app_name, dg_name)[:max_results]
    except (ClientError, BotoCoreError) as exc:
        return _aws_error_response("list", "Failed to list deployments", exc)

    deployments_summary = [
        {
            "deployment_id": info.get("deploymentId"),
            "status": info.get("status"),
            "create_time": _format_time(info.get("createTime")),
            "complete_time": _format_time(info.get("completeTime")),
            "description": info.get("description", ""),
        }
        for info in infos
    ]

    logger.info("Listed %d deployments for %s/%s", len(deployments_summary), app_name, dg_name)

    return _build_response(
        success=True,
        action="list",
        message=f"Found {len(deployments_summary)} deployments for {app_name}/{dg_name}",
        details={
            "application_name": app_name,
            "deployment_group": dg_name,
            "deployments": deployments_summary,
            "total_count": len(deployments_summary),
        },
    )


def handle_notify(params: dict[str, Any]) -> dict[str, Any]:
    """Publish a hold or escalate decision to operators. No CodeDeploy mutation.

    The runbook directs the agent here when the evidence does not support a
    rollback, so a human sees the reasoning behind the hold.
    """
    app_name, dg_name = _resolve_target(params)
    decision = params["decision"]
    reason = params["reason"].strip()
    deployment_id = params.get("deployment_id")
    status = "Unknown"

    try:
        if deployment_id:
            info = _validate_deployment(deployment_id, app_name, dg_name, require_active=False)
        else:
            info = _get_active_deployment(app_name, dg_name) or {}
            deployment_id = info.get("deploymentId")
        if info:
            status = info.get("status", "Unknown")
    except (ClientError, BotoCoreError) as exc:
        return _aws_error_response(
            "notify", "Failed to look up deployment", exc, deployment_id
        )

    if decision == "hold":
        subject = f"[HOLD] {app_name} deployment not rolled back"
        footer = "The agent is holding and monitoring. No rollback was performed."
    else:
        subject = f"[ESCALATE] {app_name} deployment needs human review"
        footer = "The agent could not reach a confident decision. Please review."

    error_code = _publish_notification(
        subject=subject,
        message=(
            f"Deployment: {deployment_id or 'none active'}\n"
            f"Application: {app_name}\n"
            f"Deployment Group: {dg_name}\n"
            f"Status: {status}\n"
            f"Decision: {decision}\n"
            f"Reason: {reason}\n\n"
            f"{footer}"
        ),
    )
    if error_code:
        error_type = "transient" if error_code in TRANSIENT_ERROR_CODES else "permanent"
        return _error_response(
            "notify",
            f"Failed to publish {decision} notification: {error_code}",
            error_code,
            error_type,
            deployment_id,
        )

    return _build_response(
        success=True,
        action="notify",
        message=f"Published {decision} notification for {app_name}/{dg_name}",
        details={
            "deployment_id": deployment_id,
            "decision": decision,
            "reason": reason,
            "notification_published": True,
        },
    )


# ---------------------------------------------------------------------------
# Lambda Handler
# ---------------------------------------------------------------------------

# Action dispatch table
ACTION_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "rollback": handle_rollback,
    "stop": handle_stop,
    "status": handle_status,
    "list": handle_list,
    "notify": handle_notify,
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point — routes to the appropriate action handler.

    Expected event schema (from AWS DevOps Agent MCP tool invocation), see
    devops-agent/mcp-tool-definition.json:
    {
        "action": "rollback" | "stop" | "status" | "list" | "notify",
        "application_name": "optional, must equal the configured application",
        "deployment_group": "optional, must equal the configured group",
        "deployment_id": "optional, d-XXXXXXXXX",
        "reason": "why the action is being taken (required for notify)",
        "decision": "hold" | "escalate" (notify only, required),
        "max_results": 10
    }
    """
    # Support both direct invocation and API Gateway proxy format
    if isinstance(event, dict) and isinstance(event.get("body"), str):
        try:
            params = json.loads(event["body"])
        except json.JSONDecodeError:
            return _error_response(
                "unknown", "Invalid JSON in request body", "InvalidJson", "validation"
            )
    else:
        params = event

    if not isinstance(params, dict):
        return _error_response(
            "unknown", "Request must be a JSON object", "InvalidRequest", "validation"
        )

    raw_action = params.get("action")
    action = raw_action.lower().strip() if isinstance(raw_action, str) else ""
    # Log the action and parameter names only, not their values
    logger.info("Received action=%s params=%s", action or "missing", sorted(params))

    if action not in ACTION_HANDLERS:
        valid_actions = ", ".join(ACTION_HANDLERS)
        return _error_response(
            action or "missing",
            f"Invalid action '{action}'. Valid actions: {valid_actions}",
            "InvalidAction",
            "validation",
        )

    try:
        _validate_params(action, params)
        return ACTION_HANDLERS[action](params)
    except ValidationError as exc:
        logger.warning("Validation failed for action %s: %s", action, exc.error_code)
        return _error_response(
            action,
            str(exc),
            exc.error_code,
            "validation",
            params.get("deployment_id") if isinstance(params.get("deployment_id"), str) else None,
        )
    except Exception:
        # Catch-all for unexpected errors — never let the Lambda crash silently.
        # Full detail stays in the logs; the caller gets a generic message.
        logger.exception("Unhandled exception in action '%s'", action)
        return _error_response(action, "Internal error", "InternalError", "permanent")
