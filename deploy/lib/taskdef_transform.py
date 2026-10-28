#!/usr/bin/env python3
"""Turn `aws ecs describe-task-definition` output into register input (stdlib only).

Reads the describe output on stdin and writes JSON for
`aws ecs register-task-definition --cli-input-json` on stdout.

Usage:
    aws ecs describe-task-definition --task-definition <arn> --output json \\
        | python3 taskdef_transform.py <error_rate> <app_version>

Only the fields this sample uses are kept (an allowlist, so read-only fields
such as taskDefinitionArn, revision, status, or any field added to the
describe output later are dropped). In the sample-app container, ERROR_RATE
and APP_VERSION are replaced when present and appended otherwise. A
Project=intelligent-rollback tag is added.
"""

from __future__ import annotations

import json
import sys
from typing import Any

CONTAINER_NAME = "sample-app"
KEPT_FIELDS = (
    "family",
    "taskRoleArn",
    "executionRoleArn",
    "networkMode",
    "containerDefinitions",
    "requiresCompatibilities",
    "cpu",
    "memory",
    "runtimePlatform",
)
OPTIONAL_FIELDS = ("volumes", "ephemeralStorage")
TAGS = [{"key": "Project", "value": "intelligent-rollback"}]


def _set_env(environment: list[dict[str, str]], name: str, value: str) -> None:
    for entry in environment:
        if entry.get("name") == name:
            entry["value"] = value
            return
    environment.append({"name": name, "value": value})


def transform(described: dict[str, Any], error_rate: str, app_version: str) -> dict[str, Any]:
    source = described["taskDefinition"]
    result: dict[str, Any] = {k: source[k] for k in KEPT_FIELDS if k in source}
    for key in OPTIONAL_FIELDS:
        if source.get(key):
            result[key] = source[key]
    containers = [c for c in result.get("containerDefinitions", []) if c.get("name") == CONTAINER_NAME]
    if not containers:
        raise ValueError(f"container '{CONTAINER_NAME}' not found in the task definition")
    environment = containers[0].setdefault("environment", [])
    _set_env(environment, "ERROR_RATE", error_rate)
    _set_env(environment, "APP_VERSION", app_version)
    result["tags"] = TAGS
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: taskdef_transform.py <error_rate> <app_version>", file=sys.stderr)
        return 2
    try:
        described = json.load(sys.stdin)
        output = transform(described, argv[0], argv[1])
    except (ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: cannot transform task definition: {exc}", file=sys.stderr)
        return 2
    json.dump(output, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
