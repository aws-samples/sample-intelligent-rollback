"""
MCP Rollback Tool — AWS Lambda function for the AWS DevOps Agent.

This Lambda implements a Model Context Protocol (MCP) tool that the AWS DevOps
Agent can invoke to manage CodeDeploy deployments. It
supports four actions: rollback, stop, status, and list.

Architecture:
    AWS DevOps Agent → Lambda (this) → CodeDeploy API → SNS notification

Environment Variables:
    CODEDEPLOY_APP_NAME  — Default CodeDeploy application name
    CODEDEPLOY_DG_NAME   — Default CodeDeploy deployment group name
    SNS_TOPIC_ARN        — SNS topic for rollback notifications
    AWS_REGION           — AWS region (set automatically by Lambda)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Environment-driven defaults — the agent can override these per invocation
DEFAULT_APP_NAME = os.environ.get("CODEDEPLOY_APP_NAME", "sample-app")
DEFAULT_DG_NAME = os.environ.get("CODEDEPLOY_DG_NAME", "sample-app-dg")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")

# AWS SDK clients (reused across warm invocations)
codedeploy_client = boto3.client("codedeploy")
sns_client = boto3.client("sns")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _publish_notification(subject: str, message: str) -> None:
    """Publish an operational notification to SNS.

    Non-critical: if SNS publish fails we log but don't fail the action,
    because the rollback itself already succeeded.
    """
    if not SNS_TOPIC_ARN:
        logger.warning("SNS_TOPIC_ARN not configured — skipping notification")
        return

    try:
        sns_client.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=subject[:100],  # SNS subject max 100 chars
            Message=message,
        )
        logger.info("Notification published to %s", SNS_TOPIC_ARN)
    except ClientError as exc:
        logger.error("Failed to publish SNS notification: %s", exc)


def _get_active_deployment(app_name: str, dg_name: str) -> str | None:
    """Find the currently in-progress deployment for a deployment group.

    Returns the deployment ID if one is active, or None if no deployment
    is currently in progress.
    """
    try:
        response = codedeploy_client.list_deployments(
            applicationName=app_name,
            deploymentGroupName=dg_name,
            includeOnlyStatuses=["InProgress", "Queued", "Ready"],
        )
        deployments = response.get("deployments", [])
        if deployments:
            # Return the most recent active deployment
            return deployments[0]
        return None
    except ClientError as exc:
        logger.error("Error listing deployments: %s", exc)
        raise


def _build_response(
    success: bool,
    action: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured JSON response for the AWS DevOps Agent.

    The agent expects a consistent schema so it can parse results
    and make subsequent decisions.
    """
    return {
        "statusCode": 200 if success else 500,
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


# ---------------------------------------------------------------------------
# Action Handlers
# ---------------------------------------------------------------------------


def handle_rollback(params: dict[str, Any]) -> dict[str, Any]:
    """Stop the active deployment and trigger automatic rollback.

    This is the primary action the AWS DevOps Agent invokes when it determines
    a deployment is causing issues. We use StopDeployment with
    autoRollbackEnabled=true so CodeDeploy reverts traffic to the
    previous stable task set.
    """
    app_name = params.get("application_name", DEFAULT_APP_NAME)
    dg_name = params.get("deployment_group", DEFAULT_DG_NAME)
    reason = params.get("reason", "Automated rollback triggered by AWS DevOps Agent")

    logger.info(
        "Rollback requested for app=%s dg=%s reason=%s",
        app_name,
        dg_name,
        reason,
    )

    # Step 1: Find the active deployment
    deployment_id = params.get("deployment_id") or _get_active_deployment(
        app_name, dg_name
    )

    if not deployment_id:
        return _build_response(
            success=False,
            action="rollback",
            message=f"No active deployment found for {app_name}/{dg_name}",
        )

    # Step 2: Stop the deployment with auto-rollback enabled
    try:
        response = codedeploy_client.stop_deployment(
            deploymentId=deployment_id,
            autoRollbackEnabled=True,
        )
        status = response.get("status", "Unknown")
        status_message = response.get("statusMessage", "")

        logger.info(
            "StopDeployment called: deployment_id=%s status=%s",
            deployment_id,
            status,
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        error_msg = exc.response["Error"]["Message"]
        logger.error("StopDeployment failed: %s — %s", error_code, error_msg)

        # Notify operators even on failure so they can intervene manually
        _publish_notification(
            subject=f"[FAILED] Rollback attempt for {app_name}",
            message=(
                f"Automated rollback FAILED for deployment {deployment_id}.\n"
                f"Error: {error_code} — {error_msg}\n"
                f"Reason: {reason}\n\n"
                "Manual intervention may be required."
            ),
        )

        return _build_response(
            success=False,
            action="rollback",
            message=f"Failed to stop deployment: {error_code} — {error_msg}",
            details={"deployment_id": deployment_id, "error_code": error_code},
        )

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
        },
    )


def handle_stop(params: dict[str, Any]) -> dict[str, Any]:
    """Stop the active deployment WITHOUT rollback.

    Use this when the agent wants to halt a deployment but keep the
    current (new) task set active — e.g., for investigation.
    """
    app_name = params.get("application_name", DEFAULT_APP_NAME)
    dg_name = params.get("deployment_group", DEFAULT_DG_NAME)

    deployment_id = params.get("deployment_id") or _get_active_deployment(
        app_name, dg_name
    )

    if not deployment_id:
        return _build_response(
            success=False,
            action="stop",
            message=f"No active deployment found for {app_name}/{dg_name}",
        )

    try:
        response = codedeploy_client.stop_deployment(
            deploymentId=deployment_id,
            autoRollbackEnabled=False,  # Stop only, no rollback
        )
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
    except ClientError as exc:
        error_msg = exc.response["Error"]["Message"]
        logger.error("StopDeployment (no rollback) failed: %s", error_msg)
        return _build_response(
            success=False,
            action="stop",
            message=f"Failed to stop deployment: {error_msg}",
            details={"deployment_id": deployment_id},
        )


def handle_status(params: dict[str, Any]) -> dict[str, Any]:
    """Get the current deployment status for a deployment group.

    The agent uses this to understand the current state before deciding
    whether to roll back.
    """
    app_name = params.get("application_name", DEFAULT_APP_NAME)
    dg_name = params.get("deployment_group", DEFAULT_DG_NAME)
    deployment_id = params.get("deployment_id")

    try:
        # If no deployment_id specified, get the most recent one
        if not deployment_id:
            response = codedeploy_client.list_deployments(
                applicationName=app_name,
                deploymentGroupName=dg_name,
            )
            deployments = response.get("deployments", [])
            if not deployments:
                return _build_response(
                    success=True,
                    action="status",
                    message=f"No deployments found for {app_name}/{dg_name}",
                    details={"has_deployments": False},
                )
            deployment_id = deployments[0]

        # Get detailed deployment info
        deploy_info = codedeploy_client.get_deployment(deploymentId=deployment_id)
        info = deploy_info.get("deploymentInfo", {})

        details = {
            "deployment_id": deployment_id,
            "status": info.get("status", "Unknown"),
            "create_time": str(info.get("createTime", "")),
            "complete_time": str(info.get("completeTime", "")),
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
    except ClientError as exc:
        error_msg = exc.response["Error"]["Message"]
        logger.error("GetDeployment failed: %s", error_msg)
        return _build_response(
            success=False,
            action="status",
            message=f"Failed to get deployment status: {error_msg}",
        )


def handle_list(params: dict[str, Any]) -> dict[str, Any]:
    """List recent deployments for an application.

    Provides the agent with historical context to make better decisions
    (e.g., detecting repeated failures).
    """
    app_name = params.get("application_name", DEFAULT_APP_NAME)
    dg_name = params.get("deployment_group", DEFAULT_DG_NAME)
    max_results = min(params.get("max_results", 10), 25)  # Cap at 25

    try:
        response = codedeploy_client.list_deployments(
            applicationName=app_name,
            deploymentGroupName=dg_name,
        )
        deployment_ids = response.get("deployments", [])[:max_results]

        # Fetch summary for each deployment
        deployments_summary = []
        if deployment_ids:
            batch_response = codedeploy_client.batch_get_deployments(
                deploymentIds=deployment_ids
            )
            for info in batch_response.get("deploymentsInfo", []):
                deployments_summary.append(
                    {
                        "deployment_id": info.get("deploymentId"),
                        "status": info.get("status"),
                        "create_time": str(info.get("createTime", "")),
                        "complete_time": str(info.get("completeTime", "")),
                        "description": info.get("description", ""),
                    }
                )

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
    except ClientError as exc:
        error_msg = exc.response["Error"]["Message"]
        logger.error("ListDeployments failed: %s", error_msg)
        return _build_response(
            success=False,
            action="list",
            message=f"Failed to list deployments: {error_msg}",
        )


# ---------------------------------------------------------------------------
# Lambda Handler
# ---------------------------------------------------------------------------

# Action dispatch table
ACTION_HANDLERS = {
    "rollback": handle_rollback,
    "stop": handle_stop,
    "status": handle_status,
    "list": handle_list,
}


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point — routes to the appropriate action handler.

    Expected event schema (from AWS DevOps Agent MCP tool invocation):
    {
        "action": "rollback" | "stop" | "status" | "list",
        "application_name": "optional-override",
        "deployment_group": "optional-override",
        "deployment_id": "optional-specific-id",
        "reason": "why the action is being taken",
        "max_results": 10
    }
    """
    logger.info("Received event: %s", json.dumps(event))

    # Support both direct invocation and API Gateway proxy format
    if "body" in event and isinstance(event["body"], str):
        try:
            params = json.loads(event["body"])
        except json.JSONDecodeError:
            return _build_response(
                success=False,
                action="unknown",
                message="Invalid JSON in request body",
            )
    else:
        params = event

    # Validate the action
    action = params.get("action", "").lower().strip()
    if action not in ACTION_HANDLERS:
        valid_actions = ", ".join(ACTION_HANDLERS.keys())
        return _build_response(
            success=False,
            action=action or "missing",
            message=f"Invalid action '{action}'. Valid actions: {valid_actions}",
        )

    # Dispatch to the appropriate handler
    try:
        return ACTION_HANDLERS[action](params)
    except Exception as exc:
        # Catch-all for unexpected errors — never let the Lambda crash silently
        logger.exception("Unhandled exception in action '%s': %s", action, exc)
        return _build_response(
            success=False,
            action=action,
            message=f"Internal error: {str(exc)}",
        )
