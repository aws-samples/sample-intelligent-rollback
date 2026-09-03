#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# cleanup.sh — Tear down all resources created by deploy.sh
#
# Deletes resources in reverse dependency order to avoid conflicts.
# Safe to run multiple times (idempotent — errors from missing resources
# are suppressed).
#
# Usage:
#   export AWS_REGION=us-east-1
#   ./deploy/cleanup.sh
# ---------------------------------------------------------------------------

set -uo pipefail
# Note: NOT using -e because we want to continue even if some deletes fail
# (resource may already be gone from a partial cleanup)

PROJECT_NAME="intelligent-rollback"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "============================================="
echo " Intelligent Rollback — Cleanup"
echo " Region:  ${AWS_REGION}"
echo " Account: ${AWS_ACCOUNT_ID}"
echo "============================================="
echo ""
echo "WARNING: This will delete ALL resources for this sample."
echo "Press Ctrl+C within 10 seconds to abort..."
sleep 10

# ---------------------------------------------------------------------------
# Step 1: CloudWatch Alarm
# ---------------------------------------------------------------------------
echo "[1/9] Deleting CloudWatch alarm..."
aws cloudwatch delete-alarms --alarm-names "${PROJECT_NAME}-error-rate" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 2: Lambda Functions
# ---------------------------------------------------------------------------
echo "[2/9] Deleting Lambda functions..."
aws lambda delete-function --function-name "mcp-rollback-tool" 2>/dev/null || true
aws lambda delete-function --function-name "webhook-forwarder" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 3: SNS Topic
# ---------------------------------------------------------------------------
echo "[3/9] Deleting SNS topic..."
SNS_TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${PROJECT_NAME}-notifications"
# Remove all subscriptions first
for SUB_ARN in $(aws sns list-subscriptions-by-topic --topic-arn "$SNS_TOPIC_ARN" \
    --query 'Subscriptions[].SubscriptionArn' --output text 2>/dev/null); do
    aws sns unsubscribe --subscription-arn "$SUB_ARN" 2>/dev/null || true
done
aws sns delete-topic --topic-arn "$SNS_TOPIC_ARN" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 4: CodeDeploy
# ---------------------------------------------------------------------------
echo "[4/9] Deleting CodeDeploy application..."
aws deploy delete-deployment-group \
    --application-name "sample-app" \
    --deployment-group-name "sample-app-dg" 2>/dev/null || true
aws deploy delete-application --application-name "sample-app" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 5: ECS Service and Cluster
# ---------------------------------------------------------------------------
echo "[5/9] Deleting ECS service and cluster..."

# Scale down and delete service
aws ecs update-service --cluster "${PROJECT_NAME}-cluster" \
    --service "sample-app-service" --desired-count 0 2>/dev/null || true
aws ecs delete-service --cluster "${PROJECT_NAME}-cluster" \
    --service "sample-app-service" --force 2>/dev/null || true

# Wait for tasks to drain
echo "  Waiting for tasks to stop..."
sleep 30

aws ecs delete-cluster --cluster "${PROJECT_NAME}-cluster" 2>/dev/null || true

# Deregister task definitions
for TD in $(aws ecs list-task-definitions --family-prefix "sample-app" \
    --query 'taskDefinitionArns[]' --output text 2>/dev/null); do
    aws ecs deregister-task-definition --task-definition "$TD" 2>/dev/null || true
done

# ---------------------------------------------------------------------------
# Step 6: Load Balancer and Target Groups
# ---------------------------------------------------------------------------
echo "[6/9] Deleting ALB and target groups..."

ALB_ARN=$(aws elbv2 describe-load-balancers --names "${PROJECT_NAME}-alb" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo "")

if [[ -n "$ALB_ARN" && "$ALB_ARN" != "None" ]]; then
    # Delete listeners first
    for LISTENER in $(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
        --query 'Listeners[].ListenerArn' --output text 2>/dev/null); do
        aws elbv2 delete-listener --listener-arn "$LISTENER" 2>/dev/null || true
    done
    aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" 2>/dev/null || true
    echo "  Waiting for ALB deletion..."
    sleep 30
fi

# Delete target groups
for TG_NAME in "${PROJECT_NAME}-blue" "${PROJECT_NAME}-green"; do
    TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
        --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")
    if [[ -n "$TG_ARN" && "$TG_ARN" != "None" ]]; then
        aws elbv2 delete-target-group --target-group-arn "$TG_ARN" 2>/dev/null || true
    fi
done

# ---------------------------------------------------------------------------
# Step 7: ECR Repository
# ---------------------------------------------------------------------------
echo "[7/9] Deleting ECR repository..."
aws ecr delete-repository --repository-name "${PROJECT_NAME}-sample-app" \
    --force 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 8: VPC and Networking
# ---------------------------------------------------------------------------
echo "[8/9] Deleting VPC and networking..."

VPC_ID=$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=${PROJECT_NAME}-vpc" \
    --query 'Vpcs[0].VpcId' --output text 2>/dev/null || echo "")

if [[ -n "$VPC_ID" && "$VPC_ID" != "None" ]]; then
    # Delete NAT Gateways
    for NAT in $(aws ec2 describe-nat-gateways --filter "Name=vpc-id,Values=$VPC_ID" \
        --query 'NatGateways[?State!=`deleted`].NatGatewayId' --output text 2>/dev/null); do
        aws ec2 delete-nat-gateway --nat-gateway-id "$NAT" 2>/dev/null || true
    done
    echo "  Waiting for NAT Gateway deletion..."
    sleep 60

    # Release Elastic IPs
    for EIP in $(aws ec2 describe-addresses --filters "Name=domain,Values=vpc" \
        --query 'Addresses[?AssociationId==null].AllocationId' --output text 2>/dev/null); do
        aws ec2 release-address --allocation-id "$EIP" 2>/dev/null || true
    done

    # Delete security groups (non-default)
    for SG in $(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" \
        --query 'SecurityGroups[?GroupName!=`default`].GroupId' --output text 2>/dev/null); do
        aws ec2 delete-security-group --group-id "$SG" 2>/dev/null || true
    done

    # Delete subnets
    for SUBNET in $(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" \
        --query 'Subnets[].SubnetId' --output text 2>/dev/null); do
        aws ec2 delete-subnet --subnet-id "$SUBNET" 2>/dev/null || true
    done

    # Delete route tables (non-main)
    for RT in $(aws ec2 describe-route-tables --filters "Name=vpc-id,Values=$VPC_ID" \
        --query 'RouteTables[?Associations[0].Main!=`true`].RouteTableId' --output text 2>/dev/null); do
        # Disassociate first
        for ASSOC in $(aws ec2 describe-route-tables --route-table-id "$RT" \
            --query 'RouteTables[0].Associations[?!Main].RouteTableAssociationId' --output text 2>/dev/null); do
            aws ec2 disassociate-route-table --association-id "$ASSOC" 2>/dev/null || true
        done
        aws ec2 delete-route-table --route-table-id "$RT" 2>/dev/null || true
    done

    # Detach and delete Internet Gateway
    for IGW in $(aws ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=$VPC_ID" \
        --query 'InternetGateways[].InternetGatewayId' --output text 2>/dev/null); do
        aws ec2 detach-internet-gateway --internet-gateway-id "$IGW" --vpc-id "$VPC_ID" 2>/dev/null || true
        aws ec2 delete-internet-gateway --internet-gateway-id "$IGW" 2>/dev/null || true
    done

    # Delete VPC
    aws ec2 delete-vpc --vpc-id "$VPC_ID" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Step 9: IAM Roles
# ---------------------------------------------------------------------------
echo "[9/9] Deleting IAM roles..."

for ROLE_NAME in "${PROJECT_NAME}-ecs-execution-role" "${PROJECT_NAME}-ecs-task-role" \
    "${PROJECT_NAME}-codedeploy-role" "${PROJECT_NAME}-lambda-role"; do
    # Detach managed policies
    for POLICY in $(aws iam list-attached-role-policies --role-name "$ROLE_NAME" \
        --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
        aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY" 2>/dev/null || true
    done
    # Delete inline policies
    for POLICY in $(aws iam list-role-policies --role-name "$ROLE_NAME" \
        --query 'PolicyNames[]' --output text 2>/dev/null); do
        aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$POLICY" 2>/dev/null || true
    done
    aws iam delete-role --role-name "$ROLE_NAME" 2>/dev/null || true
done

echo ""
echo "============================================="
echo " Cleanup Complete!"
echo "============================================="
echo ""
echo " All resources have been deleted."
echo " Note: Some resources (NAT Gateway, ALB) may"
echo " take a few minutes to fully terminate."
echo "============================================="
