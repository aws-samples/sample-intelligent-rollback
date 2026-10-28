#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# trigger-good-deployment.sh: Scenario B. Starts a blue/green deployment of
# release 2.1.0 with ERROR_RATE=0.0 (a healthy revision). Right after it
# starts, raise /intelligent-rollback/dependency-failure-rate to simulate a
# downstream outage; AWS DevOps Agent should recommend HOLD.
#
# Run Scenario A first, and run ./deploy/generate-traffic.sh in another
# terminal.
#
# Usage:
#   export AWS_REGION=us-east-1
#   ./deploy/trigger-good-deployment.sh
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/create-deployment.sh
source "$SCRIPT_DIR/lib/create-deployment.sh"

create_sample_deployment 0.0 2.1.0 \
    "Release 2.1.0: logging change only in sample-app/app.py"
