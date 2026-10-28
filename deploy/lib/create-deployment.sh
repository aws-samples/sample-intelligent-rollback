# shellcheck shell=bash
# ---------------------------------------------------------------------------
# deploy/lib/create-deployment.sh: start a CodeDeploy blue/green deployment
# of the sample app with a new ERROR_RATE and APP_VERSION.
#
# Sourced by deploy/trigger-bad-deployment.sh and
# deploy/trigger-good-deployment.sh (not executable on its own). The caller
# runs with `set -euo pipefail` and calls:
#
#   create_sample_deployment <error_rate> <app_version> <description>
#
# Steps:
#   0. Refuse to start while another deployment to sample-app-dg is in
#      flight (CodeDeploy allows one in-flight deployment per group).
#   1. Validate the inputs with deploy/lib/validate.py.
#   2. Find the task definition that is serving now (PRIMARY task set).
#   3. Register a copy with the new ERROR_RATE and APP_VERSION
#      (deploy/lib/taskdef_transform.py).
#   4. Render deploy/appspec.yaml with the new ARN and create the deployment.
#   5. Print the deployment ID and the rollback window.
#
# Requires AWS_REGION. ROLLBACK_WINDOW_MINUTES is used only in messages; the
# real value is set on the deployment group by deploy.sh (default 60).
# ---------------------------------------------------------------------------

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CD_APP_NAME="sample-app"
CD_DG_NAME="sample-app-dg"
CD_CLUSTER_NAME="intelligent-rollback-cluster"
CD_SERVICE_NAME="sample-app-service"

CD_TMP_TASKDEF=""
CD_TMP_INPUT=""
cd_cleanup() { rm -f -- ${CD_TMP_TASKDEF:+"$CD_TMP_TASKDEF"} ${CD_TMP_INPUT:+"$CD_TMP_INPUT"}; }
trap cd_cleanup EXIT

create_sample_deployment() {
    local error_rate="$1" app_version="$2" description="$3"
    local region="${AWS_REGION:?Set AWS_REGION to the Region you deployed the sample in}"
    local window="${ROLLBACK_WINDOW_MINUTES:-60}"
    local in_flight id current_td new_td deployment_id

    # Step 0: in-flight pre-check (read-only; nothing is created on failure)
    if ! in_flight="$(aws deploy list-deployments --region "$region" \
            --application-name "$CD_APP_NAME" \
            --deployment-group-name "$CD_DG_NAME" \
            --include-only-statuses Created Queued InProgress Baking Ready \
            --query 'deployments' --output text)"; then
        echo "ERROR: could not list deployments for ${CD_APP_NAME}/${CD_DG_NAME} (see the AWS CLI error above)" >&2
        exit 1
    fi
    if [[ -n "$in_flight" && "$in_flight" != "None" ]]; then
        for id in $in_flight; do
            echo "ERROR: deployment ${id} is still in flight (it stays InProgress during the ${window}-minute rollback window)." \
                "Finish it with: aws deploy continue-deployment --deployment-id ${id} --deployment-wait-type TERMINATION_WAIT," \
                "or stop it with: aws deploy stop-deployment --deployment-id ${id} --auto-rollback-enabled" >&2
        done
        exit 1
    fi

    # Step 1: validate inputs
    python3 "$LIB_DIR/validate.py" rate "$error_rate"
    python3 "$LIB_DIR/validate.py" version "$app_version"

    # Step 2: the task definition that is serving now
    current_td="$(aws ecs describe-services --region "$region" \
        --cluster "$CD_CLUSTER_NAME" --services "$CD_SERVICE_NAME" \
        --query "services[0].taskSets[?status=='PRIMARY'].taskDefinition | [0]" \
        --output text)"
    if [[ -z "$current_td" || "$current_td" == "None" ]]; then
        current_td="$(aws ecs describe-services --region "$region" \
            --cluster "$CD_CLUSTER_NAME" --services "$CD_SERVICE_NAME" \
            --query 'services[0].taskDefinition' --output text)"
    fi
    if [[ -z "$current_td" || "$current_td" == "None" ]]; then
        echo "ERROR: could not determine the current task definition for ${CD_SERVICE_NAME}" >&2
        exit 1
    fi
    echo "Current task definition: ${current_td}"

    # Step 3: register a copy with the new ERROR_RATE and APP_VERSION
    CD_TMP_TASKDEF="$(umask 077 && mktemp /tmp/sample-app-taskdef-XXXXXX)"
    aws ecs describe-task-definition --region "$region" \
        --task-definition "$current_td" --output json \
        | python3 "$LIB_DIR/taskdef_transform.py" "$error_rate" "$app_version" \
        > "$CD_TMP_TASKDEF"
    new_td="$(aws ecs register-task-definition --region "$region" \
        --cli-input-json "file://${CD_TMP_TASKDEF}" \
        --query taskDefinition.taskDefinitionArn --output text)"
    echo "Registered task definition: ${new_td} (ERROR_RATE=${error_rate}, APP_VERSION=${app_version})"

    # Step 4: render the AppSpec and create the deployment
    CD_TMP_INPUT="$(umask 077 && mktemp /tmp/sample-app-deployment-XXXXXX)"
    python3 -c '
import json, sys
template_path, task_definition_arn, application, group, description = sys.argv[1:6]
with open(template_path, encoding="utf-8") as handle:
    rendered = handle.read().replace("<TASK_DEFINITION_ARN>", task_definition_arn)
if "<" in rendered:
    sys.exit("ERROR: the rendered AppSpec still contains a placeholder")
json.dump(
    {
        "applicationName": application,
        "deploymentGroupName": group,
        "revision": {
            "revisionType": "AppSpecContent",
            "appSpecContent": {"content": rendered},
        },
        "description": description,
    },
    sys.stdout,
)' "$LIB_DIR/../appspec.yaml" "$new_td" "$CD_APP_NAME" "$CD_DG_NAME" "$description" \
        > "$CD_TMP_INPUT"
    deployment_id="$(aws deploy create-deployment --region "$region" \
        --cli-input-json "file://${CD_TMP_INPUT}" \
        --query deploymentId --output text)"

    # Step 5: report
    echo ""
    echo "Deployment started: ${deployment_id}"
    echo "  Description: ${description}"
    echo "  Watch it in the AWS CodeDeploy console: Deployments > ${deployment_id}"
    echo "  Traffic shifts 10 percent per minute (about 10 minutes). CodeDeploy then"
    echo "  keeps the original task set for the ${window}-minute rollback window."
    echo "  To end the window early and keep this deployment, run:"
    echo "    aws deploy continue-deployment --deployment-id ${deployment_id} --deployment-wait-type TERMINATION_WAIT"
}
