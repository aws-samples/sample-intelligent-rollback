#!/usr/bin/env python3
"""Input validators for the deploy scripts (stdlib only).

deploy/deploy.sh and deploy/lib/create-deployment.sh call this module so the
unit tests exercise the exact code the scripts run.

Usage:
    python3 validate.py cidr <IPv4 CIDR>          IPv4 network, no host bits, prefix > 0
    python3 validate.py rate <value>              float in [0, 1]
    python3 validate.py version <value>           ^[0-9A-Za-z.+-]{1,32}$
    python3 validate.py int-range <min> <max> <value>

Exit status is 0 when the value is valid and 2 otherwise, with a one-line
message on stderr.
"""

from __future__ import annotations

import ipaddress
import math
import re
import sys

VERSION_PATTERN = re.compile(r"^[0-9A-Za-z.+-]{1,32}$")


def check_cidr(value: str) -> str | None:
    """Return an error message, or None when value is an allowed IPv4 CIDR."""
    hint = (
        "Set ALLOWED_CIDR to your public IPv4 address with /32 "
        "(find it with: curl -s https://checkip.amazonaws.com)"
    )
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return f"ALLOWED_CIDR '{value}' is not a valid IPv4 CIDR. {hint}"
    if network.version != 4:
        return f"ALLOWED_CIDR '{value}' is IPv6; this sample supports IPv4 only. {hint}"
    if network.prefixlen == 0:
        return f"ALLOWED_CIDR '{value}' opens the load balancer to the internet. {hint}"
    return None


def check_rate(value: str) -> str | None:
    try:
        rate = float(value)
    except ValueError:
        return f"rate '{value}' is not a number between 0 and 1"
    if not math.isfinite(rate) or not 0.0 <= rate <= 1.0:
        return f"rate '{value}' must be between 0 and 1"
    return None


def check_version(value: str) -> str | None:
    if not VERSION_PATTERN.match(value):
        return f"version '{value}' must match {VERSION_PATTERN.pattern}"
    return None


def check_int_range(minimum: str, maximum: str, value: str) -> str | None:
    try:
        low, high = int(minimum), int(maximum)
    except ValueError:
        return f"int-range bounds '{minimum}' and '{maximum}' must be integers"
    if not re.fullmatch(r"[0-9]+", value):
        return f"value '{value}' must be an integer between {low} and {high}"
    number = int(value)
    if not low <= number <= high:
        return f"value '{value}' must be an integer between {low} and {high}"
    return None


def main(argv: list[str]) -> int:
    checks = {
        "cidr": (check_cidr, 1),
        "rate": (check_rate, 1),
        "version": (check_version, 1),
        "int-range": (check_int_range, 3),
    }
    if not argv or argv[0] not in checks:
        print("usage: validate.py {cidr|rate|version|int-range} <args>", file=sys.stderr)
        return 2
    check, arg_count = checks[argv[0]]
    args = argv[1:]
    if len(args) != arg_count:
        print(f"usage: validate.py {argv[0]} expects {arg_count} argument(s)", file=sys.stderr)
        return 2
    error = check(*args)
    if error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
