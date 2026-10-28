#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# trigger-bad-deployment.sh: Scenario A. Starts a blue/green deployment of
# release 2.0.0 with ERROR_RATE=0.5, which simulates a code defect in the new
# revision. The alarm fires during traffic shifting and AWS DevOps Agent
# should recommend ROLL BACK.
#
# Run ./deploy/generate-traffic.sh in another terminal first.
#
# Usage:
#   export AWS_REGION=us-east-1
#   ./deploy/trigger-bad-deployment.sh
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/create-deployment.sh
source "$SCRIPT_DIR/lib/create-deployment.sh"

create_sample_deployment 0.5 2.0.0 \
    "Release 2.0.0 (simulated defect): changes the GET / request handler in sample-app/app.py"
