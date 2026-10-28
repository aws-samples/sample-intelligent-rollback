#!/usr/bin/env python3
"""Smoke test for the SigV4-protected MCP endpoint (read-only calls only).

Signs each request with botocore SigV4Auth (service execute-api) using the
default credential chain, then sends the same sequence AWS DevOps Agent uses
to validate a registration: initialize, notifications/initialized, and
tools/list. With --status it also calls the read-only get_deployment_status
tool. It never calls a mutating tool.

The caller needs execute-api:Invoke on
arn:aws:execute-api:<region>:<account>:<api-id>/v1/POST/mcp. The DevOps Agent
role intelligent-rollback-devops-agent-mcp-role trusts only
aidevops.amazonaws.com, so run this with your own operator credentials.

Usage (from the tests virtual environment, which has botocore):
    python deploy/mcp_smoke_test.py \\
        --endpoint https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp \\
        --region <region> [--status]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any

import botocore.session
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

PROTOCOL_VERSION = "2025-06-18"


def _post(endpoint: str, region: str, credentials: Any, message: dict[str, Any]) -> tuple[int, Any]:
    body = json.dumps(message).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    request = AWSRequest(method="POST", url=endpoint, data=body, headers=headers)
    SigV4Auth(credentials, "execute-api", region).add_auth(request)
    prepared = urllib.request.Request(
        endpoint, data=body, headers=dict(request.headers.items()), method="POST"
    )
    try:
        with urllib.request.urlopen(prepared, timeout=15) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    if not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--endpoint", required=True, help="https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp")
    parser.add_argument("--region", required=True, help="Region of the API (signing Region)")
    parser.add_argument("--status", action="store_true", help="also call get_deployment_status")
    args = parser.parse_args()

    if not args.endpoint.startswith("https://"):
        print("ERROR: --endpoint must be an https:// URL", file=sys.stderr)
        return 2

    credentials = botocore.session.get_session().get_credentials()
    if credentials is None:
        print("ERROR: no AWS credentials found in the default credential chain", file=sys.stderr)
        return 2
    credentials = credentials.get_frozen_credentials()

    steps: list[tuple[str, dict[str, Any], int]] = [
        (
            "initialize",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "intelligent-rollback-smoke-test", "version": "1.0.0"},
                },
            },
            200,
        ),
        ("notifications/initialized", {"jsonrpc": "2.0", "method": "notifications/initialized"}, 202),
        ("tools/list", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, 200),
    ]
    if args.status:
        steps.append(
            (
                "tools/call get_deployment_status",
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "get_deployment_status", "arguments": {}},
                },
                200,
            )
        )

    failed = False
    for label, message, expected in steps:
        status, payload = _post(args.endpoint, args.region, credentials, message)
        ok = status == expected and not (isinstance(payload, dict) and "error" in payload)
        print(f"{'PASS' if ok else 'FAIL'} {label}: HTTP {status}")
        if label == "tools/list" and isinstance(payload, dict):
            names = [tool["name"] for tool in payload.get("result", {}).get("tools", [])]
            print(f"     tools: {', '.join(names)}")
        elif label.startswith("tools/call") and isinstance(payload, dict):
            result = payload.get("result", {})
            print(f"     isError: {result.get('isError')}")
            print(f"     {json.dumps(result.get('structuredContent'), indent=2)}")
        if not ok:
            print(f"     response: {payload}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
