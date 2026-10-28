"""
MCP Streamable HTTP layer for the mcp-rollback-tool AWS Lambda function.

Amazon API Gateway (REST API, AWS_IAM authorization, Lambda proxy integration)
forwards POST /mcp requests here. This module implements the stateless subset
of the Model Context Protocol (MCP) Streamable HTTP transport that AWS DevOps
Agent needs: initialize, ping, tools/list, and tools/call, answered with
Content-Type application/json. It issues no Mcp-Session-Id, sends no
server-initiated messages, and opens no SSE stream.

The module uses only the Python standard library and never imports
lambda_function. lambda_function.lambda_handler passes in two callables:

    run_action(action, params)    runs one tool action and returns the
                                  {"statusCode", "body"} response
    error_response(action, message, error_code, error_type)
                                  builds an error response in the same shape

Access control: API Gateway verifies the SigV4 signature before the request
reaches this code. As a fail-closed second check, any event without a caller
identity (requestContext.identity.userArn) is refused with HTTP 403.

Tool classification: get_deployment_status and list_deployments are
read-only. rollback_deployment, stop_deployment, and notify_operators change
state and need operator approval in AWS DevOps Agent.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
MCP_RESOURCE = "/mcp"
MAX_BODY_BYTES = 65536

SERVER_INFO = {"name": "intelligent-rollback", "version": "2.0.0"}
SERVER_INSTRUCTIONS = (
    "Tools for the AWS CodeDeploy blue/green deployment of the configured "
    "application and deployment group. Follow the deployment-rollback-decision "
    "skill. During an autonomous investigation, call only the read-only tools "
    "get_deployment_status and list_deployments, then recommend roll back, "
    "hold, or escalate with the exact tool call you propose. "
    "rollback_deployment, stop_deployment, and notify_operators change state: "
    "call them only when an operator asks for the action in chat, and only "
    "after the operator approves it."
)

JSON_HEADERS = {"Content-Type": "application/json"}

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
# Limits are literal values so this module stays free of lambda_function
# imports. tests/test_contract_schemas.py asserts each one equals the
# lambda_function constant that enforces it, and that
# devops-agent/mcp-tool-definition.json lists exactly these tools.

_MUTATIVE_NOTE = (
    "This tool changes state and requires operator approval. Do not call it "
    "during an autonomous investigation; recommend it instead and call it only "
    "when an operator asks for it in chat."
)


def _application_name() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 100,
        "pattern": "^[A-Za-z0-9._+=,@-]{1,100}$",
        "description": "Optional. Must equal the CodeDeploy application this tool is configured for; any other value is rejected.",
    }


def _deployment_group() -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 100,
        "pattern": "^[A-Za-z0-9._+=,@-]{1,100}$",
        "description": "Optional. Must equal the CodeDeploy deployment group this tool is configured for; any other value is rejected.",
    }


def _deployment_id(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 11,
        "maxLength": 11,
        "pattern": "^d-[A-Z0-9]{9}$",
        "description": description,
    }


def _reason(description: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 1000,
        "pattern": "\\S",
        "description": description,
    }


def _output_schema() -> dict[str, Any]:
    """Schema of the parsed response body (structuredContent)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["success", "action", "message", "details", "timestamp"],
        "properties": {
            "success": {"type": "boolean"},
            "action": {
                "type": "string",
                "description": "The action that was handled: status, list, rollback, stop, or notify.",
            },
            "message": {"type": "string", "description": "Human-readable summary."},
            "timestamp": {"type": "integer", "description": "Unix epoch seconds."},
            "details": {
                "type": "object",
                "description": "Action-specific fields. Error responses always include error_code, error_type and retryable.",
                "properties": {
                    "deployment_id": {"type": ["string", "null"]},
                    "status": {"type": "string"},
                    "status_message": {"type": "string"},
                    "application_name": {"type": "string"},
                    "deployment_group": {"type": "string"},
                    "reason": {"type": "string"},
                    "decision": {"type": "string", "enum": ["hold", "escalate"]},
                    "notification_published": {"type": "boolean"},
                    "has_deployments": {"type": "boolean"},
                    "create_time": {"type": "string"},
                    "complete_time": {"type": "string"},
                    "description": {"type": "string"},
                    "creator": {"type": "string"},
                    "rollback_info": {"type": "object"},
                    "error_information": {"type": "object"},
                    "deployment_overview": {"type": "object"},
                    "deployments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "deployment_id": {"type": "string"},
                                "status": {"type": "string"},
                                "create_time": {"type": "string"},
                                "complete_time": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "total_count": {"type": "integer"},
                    "error_code": {"type": "string"},
                    "error_type": {
                        "type": "string",
                        "enum": ["validation", "transient", "permanent"],
                    },
                    "retryable": {"type": "boolean"},
                },
            },
        },
    }


_MUTATIVE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_deployment_status",
        "title": "Get CodeDeploy deployment status",
        "description": (
            "Read-only. Describe one AWS CodeDeploy deployment of the configured "
            "deployment group, or the newest one if deployment_id is omitted: status, "
            "create and complete times, description, rollback info, error information, "
            "and the traffic-shift overview. Safe to call during an investigation."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "deployment_id": _deployment_id(
                    "Optional CodeDeploy deployment ID, for example the Deployment ID line "
                    "of the incident description. Must belong to the configured deployment "
                    "group. If omitted, the newest deployment is described."
                ),
                "application_name": _application_name(),
                "deployment_group": _deployment_group(),
            },
        },
        "outputSchema": _output_schema(),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "list_deployments",
        "title": "List recent CodeDeploy deployments",
        "description": (
            "Read-only. List recent AWS CodeDeploy deployments of the configured "
            "deployment group (created in the last 30 days), newest first, with "
            "status, times, and description. Use it to spot repeated failures. Safe "
            "to call during an investigation."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "default": 10,
                    "description": "Number of deployments to return.",
                },
                "application_name": _application_name(),
                "deployment_group": _deployment_group(),
            },
        },
        "outputSchema": _output_schema(),
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "rollback_deployment",
        "title": "Roll back a CodeDeploy deployment",
        "description": (
            "Stop an active AWS CodeDeploy deployment with autoRollbackEnabled=true, "
            "which reroutes traffic to the previous task set. The deployment must "
            "belong to the configured deployment group and still be active. "
            + _MUTATIVE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deployment_id", "reason"],
            "properties": {
                "deployment_id": _deployment_id(
                    "CodeDeploy deployment ID to roll back. Must belong to the configured "
                    "deployment group and be active."
                ),
                "reason": _reason(
                    "Evidence that supports the rollback. Included in the SNS notification."
                ),
                "application_name": _application_name(),
                "deployment_group": _deployment_group(),
            },
        },
        "outputSchema": _output_schema(),
        "annotations": dict(_MUTATIVE_ANNOTATIONS),
    },
    {
        "name": "stop_deployment",
        "title": "Stop a CodeDeploy deployment without rollback",
        "description": (
            "Stop an active AWS CodeDeploy deployment without rollback, keeping the "
            "new task set in place. Use only when an operator asks to halt the "
            "deployment. " + _MUTATIVE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["deployment_id", "reason"],
            "properties": {
                "deployment_id": _deployment_id(
                    "CodeDeploy deployment ID to stop. Must belong to the configured "
                    "deployment group and be active."
                ),
                "reason": _reason("Why the deployment is being stopped."),
                "application_name": _application_name(),
                "deployment_group": _deployment_group(),
            },
        },
        "outputSchema": _output_schema(),
        "annotations": dict(_MUTATIVE_ANNOTATIONS),
    },
    {
        "name": "notify_operators",
        "title": "Notify operators of a hold or escalate decision",
        "description": (
            "Publish a hold or escalate decision and its reasoning to the operators' "
            "Amazon SNS topic. Does not change the deployment. hold means the "
            "deployment is not the likely cause; escalate means the evidence is "
            "ambiguous and a human should decide. " + _MUTATIVE_NOTE
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "reason"],
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["hold", "escalate"],
                    "description": "hold or escalate.",
                },
                "reason": _reason("Evidence behind the decision. Included in the SNS notification."),
                "deployment_id": _deployment_id(
                    "Optional CodeDeploy deployment ID. Must belong to the configured "
                    "deployment group. If omitted, the newest active deployment is used."
                ),
                "application_name": _application_name(),
                "deployment_group": _deployment_group(),
            },
        },
        "outputSchema": _output_schema(),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": True,
        },
    },
]

# Per-tool argument allowlist, derived from the schemas so it cannot drift
TOOL_ARGS = {t["name"]: frozenset(t["inputSchema"]["properties"]) for t in TOOLS}
TOOL_ACTIONS = {
    "get_deployment_status": "status",
    "list_deployments": "list",
    "rollback_deployment": "rollback",
    "stop_deployment": "stop",
    "notify_operators": "notify",
}

RunAction = Callable[[str, dict], dict]
ErrorResponse = Callable[[str, str, str, str], dict]


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _http(status: int, body: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Build an API Gateway proxy response. body None means an empty body."""
    return {
        "statusCode": status,
        "headers": {**JSON_HEADERS, **(headers or {})},
        "body": "" if body is None else json.dumps(body),
        "isBase64Encoded": False,
    }


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return _http(200, {"jsonrpc": "2.0", "id": request_id, "result": result})


def _rpc_error(status: int, request_id: Any, code: int, message: str) -> dict[str, Any]:
    return _http(
        status,
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
    )


def _echo_id(message: dict[str, Any]) -> Any:
    """Return the request id if it is a string or integer, else None."""
    value = message.get("id")
    if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    return None


def _tool_result(resp: dict[str, Any]) -> dict[str, Any]:
    """Wrap a run_action or error_response result as a tools/call result."""
    body = json.loads(resp["body"])
    return {
        "content": [{"type": "text", "text": resp["body"]}],
        "structuredContent": body,
        "isError": not body["success"],
    }


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------


def _decode_body(event: dict[str, Any]) -> bytes:
    """Return the raw request body bytes. Raises ValueError on bad base64."""
    raw = event.get("body")
    if raw is None:
        return b""
    if not isinstance(raw, str):
        raise ValueError("body is not a string")
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("invalid base64 body") from exc
    return raw.encode("utf-8")


def _is_response_object(message: dict[str, Any]) -> bool:
    return (
        message.get("jsonrpc") == "2.0"
        and "method" not in message
        and "id" in message
        and ("result" in message or "error" in message)
    )


def _handle_tools_call(
    request_id: Any,
    params: Any,
    run_action: RunAction,
    error_response: ErrorResponse,
    log_fields: dict[str, Any],
) -> dict[str, Any]:
    params = params if isinstance(params, dict) else {}
    name = params.get("name")
    if not isinstance(name, str) or name not in TOOL_ACTIONS:
        return _rpc_error(200, request_id, INVALID_PARAMS, "Unknown tool")
    log_fields["tool"] = name

    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return _rpc_error(200, request_id, INVALID_PARAMS, "arguments must be an object")
    log_fields["args"] = sorted(arguments)

    action = TOOL_ACTIONS[name]
    extra = sorted(set(arguments) - TOOL_ARGS[name])
    if extra:
        resp = error_response(
            action,
            f"Unknown argument(s) for {name}: {', '.join(extra)}",
            "UnknownParameter",
            "validation",
        )
        return _rpc_result(request_id, _tool_result(resp))

    resp = run_action(action, dict(arguments))
    return _rpc_result(request_id, _tool_result(resp))


def _dispatch(
    message: dict[str, Any],
    run_action: RunAction,
    error_response: ErrorResponse,
    log_fields: dict[str, Any],
) -> dict[str, Any]:
    method = message["method"]
    request_id = _echo_id(message)
    params = message.get("params")

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return _rpc_result(
            request_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": dict(SERVER_INFO),
                "instructions": SERVER_INSTRUCTIONS,
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        return _rpc_result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        return _handle_tools_call(request_id, params, run_action, error_response, log_fields)
    return _rpc_error(200, request_id, METHOD_NOT_FOUND, "Method not found")


def handle_http_event(
    event: dict[str, Any], run_action: RunAction, error_response: ErrorResponse
) -> dict[str, Any]:
    """Handle one API Gateway proxy event and return the proxy response."""
    if (
        not isinstance(event, dict)
        or "httpMethod" not in event
        or not isinstance(event.get("requestContext"), dict)
    ):
        logger.warning("NonApiGatewayEvent")
        return _http(400, {"error": "Unsupported invocation"})

    context = event["requestContext"]
    request_id = context.get("requestId")
    identity = context.get("identity")
    caller_arn = identity.get("userArn") if isinstance(identity, dict) else None
    if not isinstance(caller_arn, str) or not caller_arn:
        # Fail closed: with AWS_IAM authorization API Gateway always sets userArn
        logger.warning("MissingCallerIdentity request_id=%s", request_id)
        return _http(403, {"error": "Unauthenticated"})

    if event.get("resource") != MCP_RESOURCE:
        return _http(404, {"error": "Not found"})
    if event.get("httpMethod") != "POST":
        return _http(405, {"error": "Method not allowed"}, {"Allow": "POST"})

    log_fields: dict[str, Any] = {"method": None, "tool": None, "args": None}
    try:
        try:
            raw = _decode_body(event)
        except ValueError:
            return _rpc_error(400, None, PARSE_ERROR, "Parse error")
        if len(raw) > MAX_BODY_BYTES:
            return _rpc_error(413, None, INVALID_REQUEST, "Request body too large")
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return _rpc_error(400, None, PARSE_ERROR, "Parse error")

        if isinstance(message, list):
            return _rpc_error(400, None, INVALID_REQUEST, "Batch requests are not supported")
        if not isinstance(message, dict):
            return _rpc_error(400, None, INVALID_REQUEST, "Invalid Request")
        if _is_response_object(message):
            return _http(202)
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return _rpc_error(400, _echo_id(message), INVALID_REQUEST, "Invalid Request")

        log_fields["method"] = message["method"]
        if "id" not in message:
            # Notification, for example notifications/initialized
            return _http(202)
        return _dispatch(message, run_action, error_response, log_fields)
    except Exception:
        logger.exception("Unhandled exception in MCP layer request_id=%s", request_id)
        return _rpc_error(500, None, INTERNAL_ERROR, "Internal error")
    finally:
        # Names only, never argument values, headers, or the body
        logger.info(
            "MCP request method=%s tool=%s args=%s caller=%s request_id=%s",
            log_fields["method"],
            log_fields["tool"],
            log_fields["args"],
            caller_arn,
            request_id,
        )
