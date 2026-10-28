#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# generate-traffic.sh: send steady HTTP traffic to the sample app through
# the Application Load Balancer, so the error-rate alarm has requests to
# measure. Prints a status-code histogram once a minute.
#
# Usage:
#   export AWS_REGION=us-east-1
#   ./deploy/generate-traffic.sh                 # 5 requests/second, 30 minutes
#   RATE=10 DURATION_MINUTES=60 ./deploy/generate-traffic.sh
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REGION="${AWS_REGION:?Set AWS_REGION to the Region you deployed the sample in}"
RATE="${RATE:-5}"
DURATION_MINUTES="${DURATION_MINUTES:-30}"
ALB_NAME="intelligent-rollback-alb"

python3 "$SCRIPT_DIR/lib/validate.py" int-range 1 50 "$RATE"
python3 "$SCRIPT_DIR/lib/validate.py" int-range 1 240 "$DURATION_MINUTES"

ALB_DNS="$(aws elbv2 describe-load-balancers --region "$REGION" --names "$ALB_NAME" \
    --query 'LoadBalancers[0].DNSName' --output text)"
if [[ -z "$ALB_DNS" || "$ALB_DNS" == "None" ]]; then
    echo "ERROR: load balancer ${ALB_NAME} not found in ${REGION}" >&2
    exit 1
fi
URL="http://${ALB_DNS}/"

echo "Sending ${RATE} requests/second to ${URL} for ${DURATION_MINUTES} minutes (Ctrl+C to stop)"

sleep_interval="$(python3 -c 'import sys; print(1.0 / int(sys.argv[1]))' "$RATE")"
end_time=$(( $(date +%s) + DURATION_MINUTES * 60 ))
minute=1

# Works with the bash 3.2 that ships with macOS (no associative arrays).
while (( $(date +%s) < end_time )); do
    minute_end=$(( $(date +%s) + 60 ))
    codes=""
    while (( $(date +%s) < minute_end && $(date +%s) < end_time )); do
        # curl prints 000 when the request fails (timeout, connection error)
        code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$URL" || true)"
        codes+="${code:-000}"$'\n'
        sleep "$sleep_interval"
    done
    summary="$(printf '%s' "$codes" | sort | uniq -c | awk '{printf "%s=%s ", $2, $1}')"
    echo "[minute ${minute}] $(date -u +%H:%M:%SZ) status codes: ${summary:-none}"
    minute=$(( minute + 1 ))
done

echo "Traffic generation finished."
