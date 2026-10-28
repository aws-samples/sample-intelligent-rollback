#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# cleanup.sh: tear down the resources created by deploy.sh.
#
# Deletes resources in dependency order and waits for slow deletions (ECS
# service, load balancer, NAT gateway) instead of sleeping a fixed time.
# Only this sample's resources are touched: names created by deploy.sh, task
# definitions of family sample-app, and Elastic IPs of this sample's NAT
# gateway or tagged Project=intelligent-rollback.
#
# Every failed step is reported with the AWS CLI error, and the script exits
# 1 if anything remains. It is safe to run again.
#
# Usage:
#   export AWS_REGION=us-east-1
#   ./deploy/cleanup.sh
# ---------------------------------------------------------------------------

# Not using -e: a failed delete is recorded and cleanup continues.
set -uo pipefail

# Same pattern as the other deploy scripts (no sibling files are needed here).
# shellcheck disable=SC2034
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_NAME="intelligent-rollback"
REGION="${AWS_REGION:?Set AWS_REGION to the Region you deployed the sample in}"
export AWS_DEFAULT_REGION="$REGION"
CODEDEPLOY_APP_NAME="sample-app"
CODEDEPLOY_DG_NAME="sample-app-dg"
ECS_CLUSTER_NAME="${PROJECT_NAME}-cluster"
ECS_SERVICE_NAME="sample-app-service"
REST_API_NAME="${PROJECT_NAME}-mcp"
DLQ_NAME="${PROJECT_NAME}-webhook-dlq"
ELEVATED_ROLE_NAME="DevOpsAgent-ElevatedAction-${PROJECT_NAME}"
ACTIVE_STATUSES=" Created Queued InProgress Baking Ready "

if ! AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text); then
    echo "ERROR: could not call AWS STS. Check your AWS credentials." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Helpers (bash 3.2 compatible: failures are kept in a newline-separated list)
# ---------------------------------------------------------------------------

FAILURES=""
FAILURE_COUNT=0

record_failure() {
    FAILURES+="  - $1"$'\n'
    FAILURE_COUNT=$((FAILURE_COUNT + 1))
}

# exists <describe command...>: true when the command succeeds
exists() {
    "$@" > /dev/null 2>&1
}

# present <query command...>: true when the command succeeds and prints a value
present() {
    local output
    output=$("$@" 2> /dev/null) || return 1
    [[ -n "$output" && "$output" != "None" ]]
}

# attempt <label> <command...>: run a command; on failure print its stderr
# and record the failure.
attempt() {
    local label="$1" output
    shift
    if output=$("$@" 2>&1); then
        echo "  done: ${label}"
        return 0
    fi
    echo "  FAILED: ${label}"
    printf '%s\n' "$output" | sed 's/^/    /'
    record_failure "${label}: $(printf '%s' "$output" | head -n 1)"
    return 1
}

# attempt_until_free <label> <command...>: like attempt, but retries for up
# to 5 minutes while AWS reports DependencyViolation (network interfaces of
# deleted tasks and load balancers take a few minutes to go away).
attempt_until_free() {
    local label="$1" output try
    shift
    for try in $(seq 1 20); do
        if output=$("$@" 2>&1); then
            echo "  done: ${label}"
            return 0
        fi
        [[ "$output" == *DependencyViolation* ]] || break
        echo "  waiting: ${label} still has dependencies (try ${try}/20)"
        sleep 15
    done
    echo "  FAILED: ${label}"
    printf '%s\n' "$output" | sed 's/^/    /'
    record_failure "${label}: $(printf '%s' "$output" | head -n 1)"
    return 1
}

echo "============================================="
echo " Intelligent Rollback: cleanup"
echo " Region:  ${REGION}"
echo " Account: ${AWS_ACCOUNT_ID}"
echo "============================================="
echo ""
echo "WARNING: This deletes all resources created by deploy.sh in this Region."
echo "If you registered an elevated role for AWS DevOps Agent, deregister it from the"
echo "Agent Space's AWS account association first."
echo "Press Ctrl+C within 10 seconds to abort..."
sleep 10

# ---------------------------------------------------------------------------
# Step 0: Stop in-flight deployments (no rollback: everything is deleted next)
# ---------------------------------------------------------------------------
echo "[0/12] Stopping in-flight CodeDeploy deployments..."
if exists aws deploy get-application --application-name "$CODEDEPLOY_APP_NAME"; then
    if IN_FLIGHT=$(aws deploy list-deployments \
            --application-name "$CODEDEPLOY_APP_NAME" \
            --deployment-group-name "$CODEDEPLOY_DG_NAME" \
            --include-only-statuses Created Queued InProgress Baking Ready \
            --query 'deployments' --output text 2>&1); then
        for id in $IN_FLIGHT; do
            [[ "$id" == "None" ]] && continue
            attempt "stop deployment $id" \
                aws deploy stop-deployment --deployment-id "$id" --no-auto-rollback-enabled || continue
            status="unknown"
            for poll in $(seq 1 20); do
                status=$(aws deploy get-deployment --deployment-id "$id" \
                    --query 'deploymentInfo.status' --output text 2>&1) || status="unknown"
                [[ "$ACTIVE_STATUSES" == *" ${status} "* ]] || break
                echo "  waiting: deployment $id is ${status} (poll ${poll}/20)"
                sleep 15
            done
            if [[ "$ACTIVE_STATUSES" == *" ${status} "* ]]; then
                record_failure "deployment $id still in flight (status ${status}); stop it in the CodeDeploy console"
            else
                echo "  deployment $id is ${status}"
            fi
        done
    else
        echo "  FAILED: list in-flight deployments"
        printf '%s\n' "$IN_FLIGHT" | sed 's/^/    /'
        record_failure "list in-flight deployments: $(printf '%s' "$IN_FLIGHT" | head -n 1)"
    fi
fi

# ---------------------------------------------------------------------------
# Step 1: CloudWatch alarm
# ---------------------------------------------------------------------------
echo "[1/12] Deleting CloudWatch alarm..."
if present aws cloudwatch describe-alarms --alarm-names "${PROJECT_NAME}-error-rate" \
        --query 'MetricAlarms[0].AlarmName' --output text; then
    attempt "alarm ${PROJECT_NAME}-error-rate" \
        aws cloudwatch delete-alarms --alarm-names "${PROJECT_NAME}-error-rate"
fi

# ---------------------------------------------------------------------------
# Step 2: Lambda functions and the MCP REST API
# ---------------------------------------------------------------------------
echo "[2/12] Deleting Lambda functions and the MCP endpoint..."
for FUNCTION_NAME in mcp-rollback-tool webhook-forwarder; do
    if exists aws lambda get-function --function-name "$FUNCTION_NAME"; then
        # Event invoke configs and resource policies are deleted with the function
        attempt "Lambda function $FUNCTION_NAME" \
            aws lambda delete-function --function-name "$FUNCTION_NAME"
    fi
done

# API Gateway allows duplicate names, so delete every REST API with this name.
# DeleteRestApi is limited to 1 request every 30 seconds per account.
if API_IDS=$(aws apigateway get-rest-apis \
        --query "items[?name=='${REST_API_NAME}'].id" --output text 2>&1); then
    first_api=true
    for API_ID in $API_IDS; do
        [[ "$API_ID" == "None" ]] && continue
        if [[ "$first_api" != true ]]; then
            echo "  waiting 31 seconds (DeleteRestApi rate limit)..."
            sleep 31
        fi
        first_api=false
        attempt "REST API $API_ID (${REST_API_NAME})" \
            aws apigateway delete-rest-api --rest-api-id "$API_ID"
    done
else
    record_failure "list REST APIs named ${REST_API_NAME}: $(printf '%s' "$API_IDS" | head -n 1)"
fi

# ---------------------------------------------------------------------------
# Step 3: SQS dead-letter queue
# ---------------------------------------------------------------------------
echo "[3/12] Deleting SQS dead-letter queue..."
if DLQ_URL=$(aws sqs get-queue-url --queue-name "$DLQ_NAME" --query 'QueueUrl' --output text 2> /dev/null); then
    attempt "SQS queue $DLQ_NAME" aws sqs delete-queue --queue-url "$DLQ_URL"
fi

# ---------------------------------------------------------------------------
# Step 4: SNS topic and subscriptions
# ---------------------------------------------------------------------------
echo "[4/12] Deleting SNS topic..."
SNS_TOPIC_ARN="arn:aws:sns:${REGION}:${AWS_ACCOUNT_ID}:${PROJECT_NAME}-notifications"
if exists aws sns get-topic-attributes --topic-arn "$SNS_TOPIC_ARN"; then
    # Confirmed subscriptions; pending ones go away with the topic
    for SUB_ARN in $(aws sns list-subscriptions-by-topic --topic-arn "$SNS_TOPIC_ARN" \
            --query 'Subscriptions[?SubscriptionArn!=`PendingConfirmation`].SubscriptionArn' --output text); do
        [[ "$SUB_ARN" == "None" ]] && continue
        attempt "SNS subscription $SUB_ARN" aws sns unsubscribe --subscription-arn "$SUB_ARN"
    done
    attempt "SNS topic ${PROJECT_NAME}-notifications" aws sns delete-topic --topic-arn "$SNS_TOPIC_ARN"
fi

# ---------------------------------------------------------------------------
# Step 5: Parameter Store parameters
# ---------------------------------------------------------------------------
echo "[5/12] Deleting Parameter Store parameters..."
for PARAMETER in "/${PROJECT_NAME}/config" "/${PROJECT_NAME}/webhook-secret" "/${PROJECT_NAME}/dependency-failure-rate"; do
    if exists aws ssm get-parameter --name "$PARAMETER"; then
        attempt "parameter $PARAMETER" aws ssm delete-parameter --name "$PARAMETER"
    fi
done

# ---------------------------------------------------------------------------
# Step 6: CodeDeploy deployment group and application
# ---------------------------------------------------------------------------
echo "[6/12] Deleting CodeDeploy deployment group and application..."
if exists aws deploy get-deployment-group --application-name "$CODEDEPLOY_APP_NAME" \
        --deployment-group-name "$CODEDEPLOY_DG_NAME"; then
    attempt "deployment group $CODEDEPLOY_DG_NAME" aws deploy delete-deployment-group \
        --application-name "$CODEDEPLOY_APP_NAME" --deployment-group-name "$CODEDEPLOY_DG_NAME"
fi
if exists aws deploy get-application --application-name "$CODEDEPLOY_APP_NAME"; then
    attempt "CodeDeploy application $CODEDEPLOY_APP_NAME" \
        aws deploy delete-application --application-name "$CODEDEPLOY_APP_NAME"
fi

# ---------------------------------------------------------------------------
# Step 7: ECS service, cluster, and task definitions
# ---------------------------------------------------------------------------
echo "[7/12] Deleting ECS service, cluster, and task definitions..."
if present aws ecs describe-services --cluster "$ECS_CLUSTER_NAME" --services "$ECS_SERVICE_NAME" \
        --query "services[?status=='ACTIVE'].serviceName" --output text; then
    attempt "scale $ECS_SERVICE_NAME to 0" aws ecs update-service --cluster "$ECS_CLUSTER_NAME" \
        --service "$ECS_SERVICE_NAME" --desired-count 0
    attempt "ECS service $ECS_SERVICE_NAME" aws ecs delete-service --cluster "$ECS_CLUSTER_NAME" \
        --service "$ECS_SERVICE_NAME" --force
    echo "  waiting for the ECS service to become inactive..."
    attempt "wait for ECS service $ECS_SERVICE_NAME to become inactive" \
        aws ecs wait services-inactive --cluster "$ECS_CLUSTER_NAME" --services "$ECS_SERVICE_NAME"
fi
if present aws ecs describe-clusters --clusters "$ECS_CLUSTER_NAME" \
        --query "clusters[?status=='ACTIVE'].clusterName" --output text; then
    attempt "ECS cluster $ECS_CLUSTER_NAME" aws ecs delete-cluster --cluster "$ECS_CLUSTER_NAME"
fi
# Family exactly sample-app (the prefix filter alone would also match sample-app-foo)
for TD in $(aws ecs list-task-definitions --family-prefix "sample-app" --status ACTIVE \
        --query "taskDefinitionArns[?contains(@, ':task-definition/sample-app:')]" --output text); do
    [[ "$TD" == "None" ]] && continue
    attempt "task definition $TD" aws ecs deregister-task-definition --task-definition "$TD"
done

# ---------------------------------------------------------------------------
# Step 8: Load balancer and target groups
# ---------------------------------------------------------------------------
echo "[8/12] Deleting the load balancer and target groups..."
if ALB_ARN=$(aws elbv2 describe-load-balancers --names "${PROJECT_NAME}-alb" \
        --query 'LoadBalancers[0].LoadBalancerArn' --output text 2> /dev/null) \
        && [[ -n "$ALB_ARN" && "$ALB_ARN" != "None" ]]; then
    # Listeners are deleted with the load balancer
    if attempt "load balancer ${PROJECT_NAME}-alb" \
            aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN"; then
        echo "  waiting for the load balancer to be deleted..."
        attempt "wait for load balancer deletion" \
            aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN"
    fi
fi
for TG_NAME in "${PROJECT_NAME}-blue" "${PROJECT_NAME}-green"; do
    if TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
            --query 'TargetGroups[0].TargetGroupArn' --output text 2> /dev/null) \
            && [[ -n "$TG_ARN" && "$TG_ARN" != "None" ]]; then
        attempt_until_free "target group $TG_NAME" \
            aws elbv2 delete-target-group --target-group-arn "$TG_ARN"
    fi
done

# ---------------------------------------------------------------------------
# Step 9: ECR repository
# ---------------------------------------------------------------------------
echo "[9/12] Deleting ECR repository..."
if exists aws ecr describe-repositories --repository-names "${PROJECT_NAME}-sample-app"; then
    attempt "ECR repository ${PROJECT_NAME}-sample-app" \
        aws ecr delete-repository --repository-name "${PROJECT_NAME}-sample-app" --force
fi

# ---------------------------------------------------------------------------
# Step 10: NAT gateway, Elastic IPs, and the VPC
# ---------------------------------------------------------------------------
echo "[10/12] Deleting VPC and networking..."

NAT_ALLOCATION_IDS=""
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=${PROJECT_NAME}-vpc" \
    --query 'Vpcs[0].VpcId' --output text 2>&1) || {
        record_failure "look up VPC ${PROJECT_NAME}-vpc: $(printf '%s' "$VPC_ID" | head -n 1)"
        VPC_ID=""
    }

if [[ -n "$VPC_ID" && "$VPC_ID" != "None" ]]; then
    NAT_IDS=$(aws ec2 describe-nat-gateways \
        --filter "Name=vpc-id,Values=$VPC_ID" "Name=state,Values=pending,available,deleting,failed" \
        --query 'NatGateways[].NatGatewayId' --output text)
    if [[ -n "$NAT_IDS" && "$NAT_IDS" != "None" ]]; then
        # Read the NAT gateway's Elastic IPs before deleting it
        NAT_ALLOCATION_IDS=$(aws ec2 describe-nat-gateways --nat-gateway-ids $NAT_IDS \
            --query 'NatGateways[].NatGatewayAddresses[].AllocationId' --output text)
        for NAT in $NAT_IDS; do
            attempt "NAT gateway $NAT" aws ec2 delete-nat-gateway --nat-gateway-id "$NAT"
        done
        echo "  waiting for the NAT gateway to be deleted..."
        attempt "wait for NAT gateway deletion" aws ec2 wait nat-gateway-deleted --nat-gateway-ids $NAT_IDS
    fi
fi

# Release only this sample's Elastic IPs: the NAT gateway's addresses plus
# unassociated addresses tagged Project=intelligent-rollback. Untagged EIPs
# are never released.
TAGGED_ALLOCATION_IDS=$(aws ec2 describe-addresses \
    --filters "Name=tag:Project,Values=${PROJECT_NAME}" \
    --query 'Addresses[?AssociationId==null].AllocationId' --output text)
for ALLOC in $(printf '%s\n' $NAT_ALLOCATION_IDS $TAGGED_ALLOCATION_IDS | sort -u); do
    [[ "$ALLOC" == "None" ]] && continue
    if exists aws ec2 describe-addresses --allocation-ids "$ALLOC"; then
        attempt_until_free "Elastic IP $ALLOC" aws ec2 release-address --allocation-id "$ALLOC"
    fi
done

if [[ -n "$VPC_ID" && "$VPC_ID" != "None" ]]; then
    # Security groups: the ECS group references the ALB group, so it goes first
    for SG_NAME in "${PROJECT_NAME}-ecs-sg" "${PROJECT_NAME}-alb-sg"; do
        for SG in $(aws ec2 describe-security-groups \
                --filters "Name=vpc-id,Values=$VPC_ID" "Name=group-name,Values=$SG_NAME" \
                --query 'SecurityGroups[].GroupId' --output text); do
            attempt_until_free "security group $SG_NAME ($SG)" aws ec2 delete-security-group --group-id "$SG"
        done
    done

    for SUBNET in $(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" \
            --query 'Subnets[].SubnetId' --output text); do
        attempt_until_free "subnet $SUBNET" aws ec2 delete-subnet --subnet-id "$SUBNET"
    done

    # Route tables other than the main one
    for RT in $(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" \
            --query 'RouteTables[?Associations[0].Main!=`true`].RouteTableId' --output text); do
        for ASSOC in $(aws ec2 describe-route-tables --route-table-ids "$RT" \
                --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text); do
            attempt "route table association $ASSOC" aws ec2 disassociate-route-table --association-id "$ASSOC"
        done
        attempt "route table $RT" aws ec2 delete-route-table --route-table-id "$RT"
    done

    for IGW in $(aws ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
            --query 'InternetGateways[].InternetGatewayId' --output text); do
        attempt "detach internet gateway $IGW" \
            aws ec2 detach-internet-gateway --internet-gateway-id "$IGW" --vpc-id "$VPC_ID"
        attempt "internet gateway $IGW" aws ec2 delete-internet-gateway --internet-gateway-id "$IGW"
    done

    attempt_until_free "VPC $VPC_ID" aws ec2 delete-vpc --vpc-id "$VPC_ID"
fi

# ---------------------------------------------------------------------------
# Step 11: IAM roles
# ---------------------------------------------------------------------------
echo "[11/12] Deleting IAM roles..."
# ${PROJECT_NAME}-lambda-role is the shared role used by older versions.
# ${ELEVATED_ROLE_NAME} exists only if you created the contingency role
# from devops-agent/README.md; deregister it from the Agent Space first.
for ROLE_NAME in "${PROJECT_NAME}-ecs-execution-role" "${PROJECT_NAME}-ecs-task-role" \
        "${PROJECT_NAME}-codedeploy-role" "${PROJECT_NAME}-mcp-rollback-tool-role" \
        "${PROJECT_NAME}-webhook-forwarder-role" "${PROJECT_NAME}-devops-agent-mcp-role" \
        "${PROJECT_NAME}-lambda-role" "$ELEVATED_ROLE_NAME"; do
    exists aws iam get-role --role-name "$ROLE_NAME" || continue
    for POLICY in $(aws iam list-attached-role-policies --role-name "$ROLE_NAME" \
            --query 'AttachedPolicies[].PolicyArn' --output text); do
        attempt "detach $POLICY from $ROLE_NAME" \
            aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY"
    done
    for POLICY in $(aws iam list-role-policies --role-name "$ROLE_NAME" \
            --query 'PolicyNames[]' --output text); do
        attempt "inline policy $POLICY of $ROLE_NAME" \
            aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY"
    done
    attempt "IAM role $ROLE_NAME" aws iam delete-role --role-name "$ROLE_NAME"
done

# ---------------------------------------------------------------------------
# Step 12: Log groups
# ---------------------------------------------------------------------------
echo "[12/12] Deleting log groups..."
for LOG_GROUP in /ecs/sample-app /aws/lambda/mcp-rollback-tool /aws/lambda/webhook-forwarder; do
    if present aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
            --query "logGroups[?logGroupName=='${LOG_GROUP}'].logGroupName" --output text; then
        attempt "log group $LOG_GROUP" aws logs delete-log-group --log-group-name "$LOG_GROUP"
    fi
done

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo ""
echo "============================================="
EXIT_CODE=0
if (( FAILURE_COUNT > 0 )); then
    echo " Cleanup finished with ${FAILURE_COUNT} failure(s):"
    printf '%s' "$FAILURES"
    echo ""
    echo "Some resources remain. Re-run ./deploy/cleanup.sh or delete them in the console."
    EXIT_CODE=1
else
    echo "All resources created by deploy.sh were deleted."
fi
echo "============================================="
echo ""
echo "Remove these manually in the AWS DevOps Agent console:"
echo "  - The MCP server, from your Agent Space (Capabilities > MCP Servers)"
echo "  - The MCP server registration (Capability Providers > MCP Server)"
echo "  - The generic webhook (Capabilities > Webhook)"
echo "  - The deployment-rollback-decision Skill (Operator Web App > Knowledge > Skills)"
echo "  - The elevated role registration on the AWS account association, if you registered one"
echo "  - The Agent Space, if you created it only for this sample"
exit "$EXIT_CODE"
