#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh: deploy all infrastructure for the Intelligent Rollback sample.
#
# This script creates (every taggable resource gets Project=intelligent-rollback):
#   1. Parameter Store parameters (webhook secret, tool config, dependency failure rate)
#   2. VPC with public and private subnets and a NAT gateway
#   3. Amazon ECR repository (scan on push, immutable tags) and the sample image
#   4. IAM roles for Amazon ECS, AWS CodeDeploy, and the two AWS Lambda functions
#   5. Amazon ECS cluster and an Application Load Balancer (blue/green target groups)
#   6. ECS task definition and service (CodeDeploy deployment controller)
#   7. CodeDeploy application and deployment group (ECS blue/green)
#   8. Amazon SNS topic for notifications
#   9. Lambda functions mcp-rollback-tool and webhook-forwarder, with an
#      Amazon SQS dead-letter queue for failed webhook deliveries
#  10. Amazon API Gateway REST API /mcp (AWS_IAM, SigV4) in front of
#      mcp-rollback-tool, and the IAM role AWS DevOps Agent uses to call it
#  11. Amazon CloudWatch alarm that invokes webhook-forwarder
#
# Configuration files used (all resolved relative to this script):
#   taskdef.json                              ECS task definition template
#   cloudwatch-alarm.json                     Error-rate alarm definition
#   iam/lambda-trust-policy.json              Lambda execution role trust policy
#   iam/mcp-rollback-tool-policy.json         MCP tool permissions
#   iam/webhook-forwarder-policy.json         Forwarder permissions
#   iam/ecs-task-role-policy.json             Sample app task role permissions
#   iam/devops-agent-mcp-trust-policy.json    DevOps Agent SigV4 caller role trust
#   iam/devops-agent-mcp-invoke-policy.json   execute-api:Invoke on /v1/*/mcp only
#   lib/validate.py                           Input validators
#
# Not applied by this script (contingency templates, see devops-agent/README.md):
#   iam/devops-agent-elevated-trust-policy.json
#   iam/devops-agent-elevated-permissions-policy.json
#
# Prerequisites:
#   - AWS CLI v2 with credentials that can create the resources above
#   - Docker running locally, python3, curl
#   - A generic webhook (HMAC) in your AWS DevOps Agent Agent Space
#
# Usage:
#   export AWS_REGION=us-east-1                      # use the Agent Space Region
#   export NOTIFICATION_EMAIL="your-email@example.com"
#   export ALLOWED_CIDR="203.0.113.7/32"             # your IPv4 address, /32
#   export DEVOPS_AGENT_WEBHOOK_URL="https://..."    # from the webhook dialog
#   export ROLLBACK_WINDOW_MINUTES=60                # optional, 5..120
#   export DEVOPS_AGENT_REGION=us-east-1             # optional, Agent Space Region
#   ./deploy/deploy.sh                               # prompts for the secret
#   ./deploy/deploy.sh < secret-file                 # file with only the secret, one line
#
# The webhook signing secret is read only from stdin, never from an
# environment variable or a command-line argument.
#
# Run once. To deploy again, run ./deploy/cleanup.sh first.
# ---------------------------------------------------------------------------

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# BEGIN tmp-cleanup
SECRET_INPUT_FILE=""
TMP_BODY=""
cleanup_tmp() { rm -f -- ${SECRET_INPUT_FILE:+"$SECRET_INPUT_FILE"} ${TMP_BODY:+"$TMP_BODY"}; }
trap cleanup_tmp EXIT
# END tmp-cleanup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_NAME="intelligent-rollback"
PROJECT_TAG_KEY="Project"
PROJECT_TAG_VALUE="${PROJECT_NAME}"
AWS_REGION="${AWS_REGION:-}"
NOTIFICATION_EMAIL="${NOTIFICATION_EMAIL:-}"
ALLOWED_CIDR="${ALLOWED_CIDR:-}"
DEVOPS_AGENT_WEBHOOK_URL="${DEVOPS_AGENT_WEBHOOK_URL:-}"
ROLLBACK_WINDOW_MINUTES="${ROLLBACK_WINDOW_MINUTES:-60}"
DEVOPS_AGENT_REGION="${DEVOPS_AGENT_REGION:-${AWS_REGION}}"
ECR_REPO_NAME="${PROJECT_NAME}-sample-app"
ECS_CLUSTER_NAME="${PROJECT_NAME}-cluster"
ECS_SERVICE_NAME="sample-app-service"
CODEDEPLOY_APP_NAME="sample-app"
CODEDEPLOY_DG_NAME="sample-app-dg"
SNS_TOPIC_NAME="${PROJECT_NAME}-notifications"
ALARM_NAME="${PROJECT_NAME}-error-rate"
DLQ_NAME="${PROJECT_NAME}-webhook-dlq"
REST_API_NAME="${PROJECT_NAME}-mcp"
MCP_STAGE="v1"
DEVOPS_AGENT_MCP_ROLE="${PROJECT_NAME}-devops-agent-mcp-role"
CONFIG_PARAMETER_NAME="/${PROJECT_NAME}/config"
WEBHOOK_SECRET_PARAMETER="/${PROJECT_NAME}/webhook-secret"
DEPENDENCY_FAILURE_PARAMETER="/${PROJECT_NAME}/dependency-failure-rate"
LOG_RETENTION_DAYS=30
# AWS DevOps Agent supported Regions (docs: about-aws-devops-agent-supported-regions)
DEVOPS_AGENT_REGIONS="us-east-1 us-east-2 us-west-2 ca-central-1 sa-east-1 ap-south-1 ap-southeast-1 ap-southeast-2 ap-northeast-1 ap-northeast-2 eu-central-1 eu-west-1 eu-west-2 eu-west-3 eu-north-1"

# ---------------------------------------------------------------------------
# Step 1: Validate the environment (no AWS calls)
# ---------------------------------------------------------------------------

for var in AWS_REGION NOTIFICATION_EMAIL ALLOWED_CIDR DEVOPS_AGENT_WEBHOOK_URL; do
    if [[ -z "${!var}" ]]; then
        echo "ERROR: ${var} must be set (see the usage notes at the top of deploy/deploy.sh)" >&2
        exit 1
    fi
done
if [[ -n "${WEBHOOK_SECRET+x}" ]]; then
    echo "ERROR: Do not pass the webhook secret in an environment variable; deploy.sh reads it from stdin." >&2
    echo "       Run 'unset WEBHOOK_SECRET', then ./deploy/deploy.sh (prompt) or ./deploy/deploy.sh < secret-file" >&2
    exit 1
fi
if [[ "$DEVOPS_AGENT_WEBHOOK_URL" != https://* ]]; then
    echo "ERROR: DEVOPS_AGENT_WEBHOOK_URL must be an https:// URL" >&2
    exit 1
fi
python3 "$SCRIPT_DIR/lib/validate.py" cidr "$ALLOWED_CIDR" || exit 1
python3 "$SCRIPT_DIR/lib/validate.py" int-range 5 120 "$ROLLBACK_WINDOW_MINUTES" || {
    echo "ERROR: ROLLBACK_WINDOW_MINUTES must be an integer from 5 to 120" >&2
    exit 1
}
if [[ " ${DEVOPS_AGENT_REGIONS} " != *" ${DEVOPS_AGENT_REGION} "* ]]; then
    echo "WARNING: DEVOPS_AGENT_REGION=${DEVOPS_AGENT_REGION} is not in the AWS DevOps Agent supported Regions list."
    echo "         Set DEVOPS_AGENT_REGION to the Region of your Agent Space."
fi

# All AWS CLI calls below use this Region.
export AWS_DEFAULT_REGION="$AWS_REGION"

# ---------------------------------------------------------------------------
# Step 2: Re-run guard (read-only)
# ---------------------------------------------------------------------------

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

EXISTING_VPC=$(aws ec2 describe-vpcs \
    --filters "Name=tag:Name,Values=${PROJECT_NAME}-vpc" \
    --query 'Vpcs[].VpcId' --output text)
EXISTING_API=$(aws apigateway get-rest-apis \
    --query "items[?name=='${REST_API_NAME}'].id" --output text)
if [[ -n "$EXISTING_VPC" && "$EXISTING_VPC" != "None" ]] || \
   [[ -n "$EXISTING_API" && "$EXISTING_API" != "None" ]]; then
    echo "ERROR: resources from an earlier run exist (VPC: ${EXISTING_VPC:-none}, REST API: ${EXISTING_API:-none})." >&2
    echo "       Run ./deploy/cleanup.sh first." >&2
    exit 1
fi

echo "============================================="
echo " Intelligent Rollback: full deployment"
echo " Region:          ${AWS_REGION}"
echo " Account:         ${AWS_ACCOUNT_ID}"
echo " Agent Space:     ${DEVOPS_AGENT_REGION}"
echo " ALB ingress:     ${ALLOWED_CIDR} (port 80)"
echo " Rollback window: ${ROLLBACK_WINDOW_MINUTES} minutes"
echo "============================================="

# ---------------------------------------------------------------------------
# Step 3: Read the webhook signing secret from stdin
# ---------------------------------------------------------------------------

# BEGIN secret-read
webhook_secret=""
if [[ -t 0 ]]; then
  IFS= read -r -s -p "Webhook signing secret: " webhook_secret || true
  echo
else
  IFS= read -r webhook_secret || true
fi
webhook_secret="${webhook_secret%$'\r'}"
[[ -n "$webhook_secret" ]] || { echo "ERROR: empty webhook secret on stdin" >&2; exit 1; }
# END secret-read

# ---------------------------------------------------------------------------
# Step 4: Store the secret as a SecureString right away
# ---------------------------------------------------------------------------

echo ""
echo "[1/11] Writing Parameter Store parameters..."

# The secret goes through a pipe into a 0600 temp file, never through a
# child environment or a command line. cleanup_tmp removes the file on exit.
SECRET_INPUT_FILE=$(umask 077 && mktemp /tmp/webhook-secret-XXXXXX)
printf '%s' "$webhook_secret" | WEBHOOK_SECRET_PARAMETER="$WEBHOOK_SECRET_PARAMETER" python3 -c 'import json,os,sys; json.dump({"Name": os.environ["WEBHOOK_SECRET_PARAMETER"], "Type": "SecureString", "Overwrite": True, "Value": sys.stdin.read()}, sys.stdout)' > "$SECRET_INPUT_FILE"
aws ssm put-parameter --cli-input-json "file://${SECRET_INPUT_FILE}" > /dev/null
rm -f -- "$SECRET_INPUT_FILE"
SECRET_INPUT_FILE=""
unset webhook_secret

tag_parameter() {
    aws ssm add-tags-to-resource --resource-type Parameter --resource-id "$1" \
        --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}"
}
tag_parameter "$WEBHOOK_SECRET_PARAMETER"

# Dependency failure rate read by the sample app (Scenario B raises it)
aws ssm put-parameter --name "$DEPENDENCY_FAILURE_PARAMETER" --type String \
    --value "0.0" --overwrite > /dev/null
tag_parameter "$DEPENDENCY_FAILURE_PARAMETER"

echo "  Parameters: ${WEBHOOK_SECRET_PARAMETER}, ${DEPENDENCY_FAILURE_PARAMETER}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# render_template <template> <output>: fill in the placeholders of a policy
# or definition template.
render_template() {
    sed -e "s|<AWS_REGION>|${AWS_REGION}|g" \
        -e "s|<AWS_ACCOUNT_ID>|${AWS_ACCOUNT_ID}|g" \
        -e "s|<DEVOPS_AGENT_REGION>|${DEVOPS_AGENT_REGION}|g" \
        -e "s|<CODEDEPLOY_APP_NAME>|${CODEDEPLOY_APP_NAME}|g" \
        -e "s|<CODEDEPLOY_DG_NAME>|${CODEDEPLOY_DG_NAME}|g" \
        -e "s|<SNS_TOPIC_NAME>|${SNS_TOPIC_NAME}|g" \
        -e "s|<DLQ_NAME>|${DLQ_NAME}|g" \
        -e "s|<PROJECT_NAME>|${PROJECT_NAME}|g" \
        -e "s|<API_ID>|${API_ID:-API_ID_NOT_SET}|g" \
        "$1" > "$2"
}

# ensure_role <name> <trust-policy-file>: create the role (or reuse it) and
# wait until IAM reports it. Prints the role ARN. Errors return non-zero
# explicitly, because bash clears `set -e` inside command substitution.
ensure_role() {
    local name="$1" trust_file="$2" arn
    if ! arn=$(aws iam get-role --role-name "$name" --query 'Role.Arn' --output text 2> /dev/null); then
        arn=$(aws iam create-role --role-name "$name" \
            --assume-role-policy-document "file://${trust_file}" \
            --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}" \
            --query 'Role.Arn' --output text) || return 1
    else
        # Left over from a partial run: reset its trust policy to the template
        aws iam update-assume-role-policy --role-name "$name" \
            --policy-document "file://${trust_file}" || return 1
    fi
    aws iam wait role-exists --role-name "$name" || return 1
    echo "$arn"
}

# ensure_log_group <name>: pre-create a log group with 30-day retention, so
# no runtime principal needs logs:CreateLogGroup.
ensure_log_group() {
    local name="$1" output
    if ! output=$(aws logs create-log-group --log-group-name "$name" \
            --tags "${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE}" 2>&1); then
        if [[ "$output" != *ResourceAlreadyExistsException* ]]; then
            echo "ERROR: could not create log group ${name}: ${output}" >&2
            exit 1
        fi
    fi
    aws logs put-retention-policy --log-group-name "$name" \
        --retention-in-days "$LOG_RETENTION_DAYS"
}

# add_lambda_permission <function> <statement-id> <principal> <source-arn>:
# grant an AWS service permission to invoke a function. An existing
# statement with the same ID is accepted; any other error fails.
add_lambda_permission() {
    local function_name="$1" statement_id="$2" principal="$3" source_arn="$4" output
    if ! output=$(aws lambda add-permission \
            --function-name "$function_name" \
            --statement-id "$statement_id" \
            --action "lambda:InvokeFunction" \
            --principal "$principal" \
            --source-arn "$source_arn" \
            --source-account "$AWS_ACCOUNT_ID" 2>&1); then
        if [[ "$output" != *ResourceConflictException* ]]; then
            echo "ERROR: add-permission ${statement_id} on ${function_name} failed: ${output}" >&2
            exit 1
        fi
    fi
}

# ---------------------------------------------------------------------------
# VPC and networking
# ---------------------------------------------------------------------------

echo ""
echo "[2/11] Creating VPC and networking..."

tags_for() {
    # tags_for <resource-type> <name>: a --tag-specifications value
    echo "ResourceType=$1,Tags=[{Key=Name,Value=$2},{Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}}]"
}

VPC_ID=$(aws ec2 create-vpc \
    --cidr-block 10.0.0.0/16 \
    --tag-specifications "$(tags_for vpc "${PROJECT_NAME}-vpc")" \
    --query 'Vpc.VpcId' --output text)

# Enable DNS support for ECS service discovery
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}'
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'

# Internet Gateway
IGW_ID=$(aws ec2 create-internet-gateway \
    --tag-specifications "$(tags_for internet-gateway "${PROJECT_NAME}-igw")" \
    --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"

# Public subnets (for the ALB)
PUBLIC_SUBNET_1=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" --cidr-block 10.0.1.0/24 --availability-zone "${AWS_REGION}a" \
    --tag-specifications "$(tags_for subnet "${PROJECT_NAME}-public-1")" \
    --query 'Subnet.SubnetId' --output text)

PUBLIC_SUBNET_2=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" --cidr-block 10.0.2.0/24 --availability-zone "${AWS_REGION}b" \
    --tag-specifications "$(tags_for subnet "${PROJECT_NAME}-public-2")" \
    --query 'Subnet.SubnetId' --output text)

# Private subnets (for ECS tasks, no public IPs)
PRIVATE_SUBNET_1=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" --cidr-block 10.0.10.0/24 --availability-zone "${AWS_REGION}a" \
    --tag-specifications "$(tags_for subnet "${PROJECT_NAME}-private-1")" \
    --query 'Subnet.SubnetId' --output text)

PRIVATE_SUBNET_2=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" --cidr-block 10.0.11.0/24 --availability-zone "${AWS_REGION}b" \
    --tag-specifications "$(tags_for subnet "${PROJECT_NAME}-private-2")" \
    --query 'Subnet.SubnetId' --output text)

# Route table for public subnets
PUBLIC_RT=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
    --tag-specifications "$(tags_for route-table "${PROJECT_NAME}-public-rt")" \
    --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id "$PUBLIC_RT" --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW_ID" > /dev/null
aws ec2 associate-route-table --route-table-id "$PUBLIC_RT" --subnet-id "$PUBLIC_SUBNET_1" > /dev/null
aws ec2 associate-route-table --route-table-id "$PUBLIC_RT" --subnet-id "$PUBLIC_SUBNET_2" > /dev/null

# NAT gateway for private subnets (ECS tasks need outbound access to ECR,
# CloudWatch, and Parameter Store). The EIP is tagged so cleanup.sh releases
# only this sample's address.
EIP_ALLOC=$(aws ec2 allocate-address --domain vpc \
    --tag-specifications "$(tags_for elastic-ip "${PROJECT_NAME}-nat-eip")" \
    --query 'AllocationId' --output text)
NAT_GW=$(aws ec2 create-nat-gateway --subnet-id "$PUBLIC_SUBNET_1" --allocation-id "$EIP_ALLOC" \
    --tag-specifications "$(tags_for natgateway "${PROJECT_NAME}-nat")" \
    --query 'NatGateway.NatGatewayId' --output text)

echo "  Waiting for NAT gateway (about 5 minutes)..."
aws ec2 wait nat-gateway-available --nat-gateway-ids "$NAT_GW"

PRIVATE_RT=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
    --tag-specifications "$(tags_for route-table "${PROJECT_NAME}-private-rt")" \
    --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id "$PRIVATE_RT" --destination-cidr-block 0.0.0.0/0 --nat-gateway-id "$NAT_GW" > /dev/null
aws ec2 associate-route-table --route-table-id "$PRIVATE_RT" --subnet-id "$PRIVATE_SUBNET_1" > /dev/null
aws ec2 associate-route-table --route-table-id "$PRIVATE_RT" --subnet-id "$PRIVATE_SUBNET_2" > /dev/null

echo "  VPC: $VPC_ID"

# ---------------------------------------------------------------------------
# ECR repository and Docker image
# ---------------------------------------------------------------------------

echo ""
echo "[3/11] Creating ECR repository and building the Docker image..."

if aws ecr describe-repositories --repository-names "$ECR_REPO_NAME" > /dev/null 2>&1; then
    aws ecr put-image-scanning-configuration --repository-name "$ECR_REPO_NAME" \
        --image-scanning-configuration scanOnPush=true > /dev/null
    aws ecr put-image-tag-mutability --repository-name "$ECR_REPO_NAME" \
        --image-tag-mutability IMMUTABLE > /dev/null
else
    aws ecr create-repository --repository-name "$ECR_REPO_NAME" \
        --image-scanning-configuration scanOnPush=true \
        --image-tag-mutability IMMUTABLE \
        --encryption-configuration encryptionType=AES256 \
        --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}" > /dev/null
fi

# Authenticate Docker to ECR
aws ecr get-login-password | \
    docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Unique tag per build (tags are immutable)
IMAGE_TAG="v1-$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:${IMAGE_TAG}"
docker build -t "$IMAGE_URI" "$REPO_DIR/sample-app"
docker push "$IMAGE_URI"

echo "  Image pushed: $IMAGE_URI"

# ---------------------------------------------------------------------------
# IAM roles
# ---------------------------------------------------------------------------

echo ""
echo "[4/11] Creating IAM roles..."

cat > /tmp/ecs-trust-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}
EOF

cat > /tmp/codedeploy-trust-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "codedeploy.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}
EOF

# ECS task execution role (pull the image, write container logs)
EXEC_ROLE_ARN=$(ensure_role "${PROJECT_NAME}-ecs-execution-role" /tmp/ecs-trust-policy.json)
aws iam attach-role-policy --role-name "${PROJECT_NAME}-ecs-execution-role" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# ECS task role: PutMetricData in the sample namespace and read the
# dependency failure rate parameter (nothing else)
TASK_ROLE_ARN=$(ensure_role "${PROJECT_NAME}-ecs-task-role" /tmp/ecs-trust-policy.json)
render_template "$SCRIPT_DIR/iam/ecs-task-role-policy.json" /tmp/ecs-task-role-policy-rendered.json
aws iam put-role-policy --role-name "${PROJECT_NAME}-ecs-task-role" \
    --policy-name sample-app-least-privilege \
    --policy-document file:///tmp/ecs-task-role-policy-rendered.json

# CodeDeploy service role
CODEDEPLOY_ROLE_ARN=$(ensure_role "${PROJECT_NAME}-codedeploy-role" /tmp/codedeploy-trust-policy.json)
aws iam attach-role-policy --role-name "${PROJECT_NAME}-codedeploy-role" \
    --policy-arn arn:aws:iam::aws:policy/AWSCodeDeployRoleForECS

# Lambda execution roles: one per function, each with its own
# least-privilege inline policy from deploy/iam/. All CodeDeploy actions used
# (StopDeployment, GetDeployment, BatchGetDeployments, ListDeployments)
# support resource-level permissions and are scoped to this sample's
# application and deployment group.
for FUNCTION_NAME in mcp-rollback-tool webhook-forwarder; do
    ROLE_NAME="${PROJECT_NAME}-${FUNCTION_NAME}-role"
    ensure_role "$ROLE_NAME" "$SCRIPT_DIR/iam/lambda-trust-policy.json" > /dev/null
    render_template "$SCRIPT_DIR/iam/${FUNCTION_NAME}-policy.json" "/tmp/${FUNCTION_NAME}-policy-rendered.json"
    aws iam put-role-policy --role-name "$ROLE_NAME" \
        --policy-name "${FUNCTION_NAME}-least-privilege" \
        --policy-document "file:///tmp/${FUNCTION_NAME}-policy-rendered.json"
done

MCP_ROLE_ARN=$(aws iam get-role --role-name "${PROJECT_NAME}-mcp-rollback-tool-role" \
    --query 'Role.Arn' --output text)
FORWARDER_ROLE_ARN=$(aws iam get-role --role-name "${PROJECT_NAME}-webhook-forwarder-role" \
    --query 'Role.Arn' --output text)

# Log groups are created here with 30-day retention, before the task
# definition and functions that write to them.
for LOG_GROUP in /ecs/sample-app /aws/lambda/mcp-rollback-tool /aws/lambda/webhook-forwarder; do
    ensure_log_group "$LOG_GROUP"
done

# ---------------------------------------------------------------------------
# ECS cluster and ALB
# ---------------------------------------------------------------------------

echo ""
echo "[5/11] Creating ECS cluster and Application Load Balancer..."

aws ecs create-cluster --cluster-name "$ECS_CLUSTER_NAME" \
    --tags "key=${PROJECT_TAG_KEY},value=${PROJECT_TAG_VALUE}" > /dev/null

# Security groups
ALB_SG=$(aws ec2 create-security-group \
    --group-name "${PROJECT_NAME}-alb-sg" \
    --description "ALB security group" \
    --vpc-id "$VPC_ID" \
    --tag-specifications "$(tags_for security-group "${PROJECT_NAME}-alb-sg")" \
    --query 'GroupId' --output text)

# HTTP port 80 from ALLOWED_CIDR only (validated above; /0 is rejected)
aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" \
    --protocol tcp --port 80 --cidr "$ALLOWED_CIDR" > /dev/null

ECS_SG=$(aws ec2 create-security-group \
    --group-name "${PROJECT_NAME}-ecs-sg" \
    --description "ECS tasks security group" \
    --vpc-id "$VPC_ID" \
    --tag-specifications "$(tags_for security-group "${PROJECT_NAME}-ecs-sg")" \
    --query 'GroupId' --output text)

# Tasks accept 8080 only from the ALB security group
aws ec2 authorize-security-group-ingress --group-id "$ECS_SG" \
    --protocol tcp --port 8080 --source-group "$ALB_SG" > /dev/null

# Application Load Balancer
ALB_ARN=$(aws elbv2 create-load-balancer \
    --name "${PROJECT_NAME}-alb" \
    --subnets "$PUBLIC_SUBNET_1" "$PUBLIC_SUBNET_2" \
    --security-groups "$ALB_SG" \
    --scheme internet-facing \
    --type application \
    --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# Two target groups for blue/green deployment
create_target_group() {
    aws elbv2 create-target-group \
        --name "$1" \
        --protocol HTTP --port 8080 \
        --vpc-id "$VPC_ID" \
        --target-type ip \
        --health-check-path /health \
        --health-check-interval-seconds 15 \
        --healthy-threshold-count 2 \
        --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}" \
        --query 'TargetGroups[0].TargetGroupArn' --output text
}
TG_BLUE_ARN=$(create_target_group "${PROJECT_NAME}-blue")
TG_GREEN_ARN=$(create_target_group "${PROJECT_NAME}-green")

# Production listener (port 80)
LISTENER_ARN=$(aws elbv2 create-listener \
    --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=${TG_BLUE_ARN}" \
    --query 'Listeners[0].ListenerArn' --output text)

# CodeDeploy test traffic route. Not reachable: the ALB security group opens only port 80.
TEST_LISTENER_ARN=$(aws elbv2 create-listener \
    --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP --port 8080 \
    --default-actions "Type=forward,TargetGroupArn=${TG_GREEN_ARN}" \
    --query 'Listeners[0].ListenerArn' --output text)

echo "  ALB created: $ALB_ARN"

# ---------------------------------------------------------------------------
# ECS task definition and service
# ---------------------------------------------------------------------------

echo ""
echo "[6/11] Registering ECS task definition and creating service..."

sed -e "s|<IMAGE_URI>|${IMAGE_URI}|g" \
    -e "s|<EXEC_ROLE_ARN>|${EXEC_ROLE_ARN}|g" \
    -e "s|<TASK_ROLE_ARN>|${TASK_ROLE_ARN}|g" \
    -e "s|<AWS_REGION>|${AWS_REGION}|g" \
    "$SCRIPT_DIR/taskdef.json" > /tmp/taskdef-rendered.json

TASK_DEF_ARN=$(aws ecs register-task-definition \
    --cli-input-json file:///tmp/taskdef-rendered.json \
    --tags "key=${PROJECT_TAG_KEY},value=${PROJECT_TAG_VALUE}" \
    --query 'taskDefinition.taskDefinitionArn' --output text)

aws ecs create-service \
    --cluster "$ECS_CLUSTER_NAME" \
    --service-name "$ECS_SERVICE_NAME" \
    --task-definition "$TASK_DEF_ARN" \
    --desired-count 2 \
    --launch-type FARGATE \
    --deployment-controller type=CODE_DEPLOY \
    --network-configuration "awsvpcConfiguration={subnets=[$PRIVATE_SUBNET_1,$PRIVATE_SUBNET_2],securityGroups=[$ECS_SG],assignPublicIp=DISABLED}" \
    --load-balancers "targetGroupArn=${TG_BLUE_ARN},containerName=sample-app,containerPort=8080" \
    --tags "key=${PROJECT_TAG_KEY},value=${PROJECT_TAG_VALUE}" > /dev/null

echo "  ECS service created with the CodeDeploy deployment controller"

# ---------------------------------------------------------------------------
# CodeDeploy application and deployment group
# ---------------------------------------------------------------------------

echo ""
echo "[7/11] Setting up CodeDeploy blue/green deployment..."

aws deploy create-application \
    --application-name "$CODEDEPLOY_APP_NAME" \
    --compute-platform ECS \
    --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}" > /dev/null

# terminationWaitTimeInMinutes is the rollback window: CodeDeploy keeps the
# original task set this long after traffic shifting, so an operator can
# approve a rollback. Auto-rollback stays on for DEPLOYMENT_FAILURE only; no
# alarm is attached to the group, so only an approved action rolls back.
aws deploy create-deployment-group \
    --application-name "$CODEDEPLOY_APP_NAME" \
    --deployment-group-name "$CODEDEPLOY_DG_NAME" \
    --service-role-arn "$CODEDEPLOY_ROLE_ARN" \
    --deployment-config-name CodeDeployDefault.ECSLinear10PercentEvery1Minutes \
    --deployment-style "deploymentType=BLUE_GREEN,deploymentOption=WITH_TRAFFIC_CONTROL" \
    --ecs-services "serviceName=${ECS_SERVICE_NAME},clusterName=${ECS_CLUSTER_NAME}" \
    --load-balancer-info "targetGroupPairInfoList=[{targetGroups=[{name=${PROJECT_NAME}-blue},{name=${PROJECT_NAME}-green}],prodTrafficRoute={listenerArns=[${LISTENER_ARN}]},testTrafficRoute={listenerArns=[${TEST_LISTENER_ARN}]}}]" \
    --blue-green-deployment-configuration "{
        \"terminateBlueInstancesOnDeploymentSuccess\": {
            \"action\": \"TERMINATE\",
            \"terminationWaitTimeInMinutes\": ${ROLLBACK_WINDOW_MINUTES}
        },
        \"deploymentReadyOption\": {
            \"actionOnTimeout\": \"CONTINUE_DEPLOYMENT\",
            \"waitTimeInMinutes\": 0
        }
    }" \
    --auto-rollback-configuration "enabled=true,events=DEPLOYMENT_FAILURE" \
    --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}" > /dev/null

echo "  CodeDeploy deployment group: $CODEDEPLOY_DG_NAME (rollback window ${ROLLBACK_WINDOW_MINUTES} minutes)"

# ---------------------------------------------------------------------------
# SNS topic and tool configuration
# ---------------------------------------------------------------------------

echo ""
echo "[8/11] Creating SNS notification topic..."

SNS_TOPIC_ARN=$(aws sns create-topic --name "$SNS_TOPIC_NAME" \
    --attributes KmsMasterKeyId=alias/aws/sns \
    --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}" \
    --query 'TopicArn' --output text)

aws sns subscribe --topic-arn "$SNS_TOPIC_ARN" \
    --protocol email --notification-endpoint "$NOTIFICATION_EMAIL" > /dev/null
echo "  Subscription created. Check your email and confirm it."
echo "  SNS topic: $SNS_TOPIC_ARN"

# Tool configuration in Parameter Store (read by the MCP rollback tool)
aws ssm put-parameter \
    --name "$CONFIG_PARAMETER_NAME" \
    --type String \
    --overwrite \
    --value "{\"application_name\":\"${CODEDEPLOY_APP_NAME}\",\"deployment_group\":\"${CODEDEPLOY_DG_NAME}\",\"sns_topic_arn\":\"${SNS_TOPIC_ARN}\"}" \
    > /dev/null
tag_parameter "$CONFIG_PARAMETER_NAME"

echo "  Parameter: $CONFIG_PARAMETER_NAME"

# ---------------------------------------------------------------------------
# Lambda functions and dead-letter queue
# ---------------------------------------------------------------------------

echo ""
echo "[9/11] Deploying Lambda functions..."

# create_function_with_retry <create-function args...>: a new IAM role can
# take a few seconds before Lambda can assume it. Retry only that error.
create_function_with_retry() {
    local attempt output
    for attempt in 1 2 3 4 5 6; do
        if output=$(aws lambda create-function "$@" 2>&1); then
            return 0
        fi
        if [[ "$output" == *InvalidParameterValueException* && "$output" == *"cannot be assumed"* && "$attempt" -lt 6 ]]; then
            echo "  IAM role not assumable yet (attempt ${attempt}/6); retrying in 10 seconds..."
            sleep 10
            continue
        fi
        echo "ERROR: create-function failed: ${output}" >&2
        exit 1
    done
}

# create_or_update_function <name> <role-arn> <zip> <timeout> <environment>
# Creates the function, or on reruns updates its code and configuration.
# Neither function gets a function URL. mcp-rollback-tool is reachable only
# through the SigV4-protected API Gateway endpoint created below.
create_or_update_function() {
    local name="$1" role_arn="$2" zip_file="$3" timeout="$4" environment="$5"
    if aws lambda get-function --function-name "$name" > /dev/null 2>&1; then
        aws lambda update-function-code \
            --function-name "$name" \
            --zip-file "fileb://${zip_file}" > /dev/null
        aws lambda wait function-updated --function-name "$name"
        aws lambda update-function-configuration \
            --function-name "$name" \
            --role "$role_arn" \
            --timeout "$timeout" \
            --environment "$environment" > /dev/null
        aws lambda wait function-updated --function-name "$name"
    else
        create_function_with_retry \
            --function-name "$name" \
            --runtime python3.12 \
            --handler lambda_function.lambda_handler \
            --role "$role_arn" \
            --zip-file "fileb://${zip_file}" \
            --timeout "$timeout" \
            --memory-size 256 \
            --environment "$environment" \
            --tags "${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE}"
    fi
    aws lambda wait function-active-v2 --function-name "$name"
}

# Package and deploy the MCP rollback tool
rm -f /tmp/mcp-rollback-tool.zip /tmp/webhook-forwarder.zip
(cd "$REPO_DIR/mcp-rollback-tool" && zip -r /tmp/mcp-rollback-tool.zip lambda_function.py mcp_server.py)

create_or_update_function "mcp-rollback-tool" "$MCP_ROLE_ARN" /tmp/mcp-rollback-tool.zip 30 \
    "Variables={CONFIG_PARAMETER_NAME=${CONFIG_PARAMETER_NAME},CODEDEPLOY_APP_NAME=${CODEDEPLOY_APP_NAME},CODEDEPLOY_DG_NAME=${CODEDEPLOY_DG_NAME},SNS_TOPIC_ARN=${SNS_TOPIC_ARN}}"

# Package and deploy the webhook forwarder. It reads its HMAC secret from
# Parameter Store at runtime; the secret is not in its environment. Timeout
# 60 seconds covers three delivery attempts, the CodeDeploy lookup, and SNS.
(cd "$REPO_DIR/webhook-forwarder" && zip -r /tmp/webhook-forwarder.zip lambda_function.py)

create_or_update_function "webhook-forwarder" "$FORWARDER_ROLE_ARN" /tmp/webhook-forwarder.zip 60 \
    "Variables={WEBHOOK_URL=${DEVOPS_AGENT_WEBHOOK_URL},WEBHOOK_SECRET_PARAMETER=${WEBHOOK_SECRET_PARAMETER},CODEDEPLOY_APP_NAME=${CODEDEPLOY_APP_NAME},CODEDEPLOY_DG_NAME=${CODEDEPLOY_DG_NAME},SNS_TOPIC_ARN=${SNS_TOPIC_ARN},ROLLBACK_WINDOW_MINUTES=${ROLLBACK_WINDOW_MINUTES}}"

# Dead-letter queue for alarm events the forwarder could not deliver after
# Lambda's asynchronous retries (SSE with SQS managed keys, 14-day retention)
DLQ_URL=$(aws sqs create-queue --queue-name "$DLQ_NAME" \
    --attributes SqsManagedSseEnabled=true,MessageRetentionPeriod=1209600 \
    --tags "${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE}" \
    --query 'QueueUrl' --output text)
DLQ_ARN=$(aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
    --attribute-names QueueArn --query 'Attributes.QueueArn' --output text)

aws lambda put-function-event-invoke-config \
    --function-name webhook-forwarder \
    --maximum-retry-attempts 2 \
    --maximum-event-age-in-seconds 3600 \
    --destination-config "{\"OnFailure\":{\"Destination\":\"${DLQ_ARN}\"}}" > /dev/null

echo "  Lambda functions deployed; dead-letter queue: $DLQ_NAME"

# ---------------------------------------------------------------------------
# MCP endpoint: API Gateway REST API with AWS_IAM (SigV4) authorization
# ---------------------------------------------------------------------------

echo ""
echo "[10/11] Creating the SigV4-protected MCP endpoint..."

MCP_FUNCTION_ARN=$(aws lambda get-function --function-name mcp-rollback-tool \
    --query 'Configuration.FunctionArn' --output text)

# 1. REST API and the /mcp resource
API_ID=$(aws apigateway create-rest-api --region "$AWS_REGION" \
    --name "$REST_API_NAME" \
    --endpoint-configuration types=REGIONAL \
    --tags "${PROJECT_TAG_KEY}=${PROJECT_TAG_VALUE}" \
    --query 'id' --output text)
ROOT_RESOURCE_ID=$(aws apigateway get-resources --region "$AWS_REGION" \
    --rest-api-id "$API_ID" --query "items[?path=='/'].id" --output text)
MCP_RESOURCE_ID=$(aws apigateway create-resource --region "$AWS_REGION" \
    --rest-api-id "$API_ID" --parent-id "$ROOT_RESOURCE_ID" --path-part mcp \
    --query 'id' --output text)

# 2. POST, GET, DELETE: every method requires SigV4 (AWS_IAM). Lambda proxy
# integrations always call Lambda with POST, whatever the client method.
for METHOD in POST GET DELETE; do
    aws apigateway put-method --region "$AWS_REGION" \
        --rest-api-id "$API_ID" --resource-id "$MCP_RESOURCE_ID" \
        --http-method "$METHOD" --authorization-type AWS_IAM > /dev/null
    aws apigateway put-integration --region "$AWS_REGION" \
        --rest-api-id "$API_ID" --resource-id "$MCP_RESOURCE_ID" \
        --http-method "$METHOD" --type AWS_PROXY --integration-http-method POST \
        --uri "arn:aws:apigateway:${AWS_REGION}:lambda:path/2015-03-31/functions/${MCP_FUNCTION_ARN}/invocations" \
        > /dev/null
done

# 3. Allow only this API's /mcp resource to invoke the function
add_lambda_permission mcp-rollback-tool apigateway-mcp apigateway.amazonaws.com \
    "arn:aws:execute-api:${AWS_REGION}:${AWS_ACCOUNT_ID}:${API_ID}/*/*/mcp"

# 4. Deploy stage v1
aws apigateway create-deployment --region "$AWS_REGION" \
    --rest-api-id "$API_ID" --stage-name "$MCP_STAGE" > /dev/null

# 5. Stage throttling: 5 requests per second, burst 10
aws apigateway update-stage --region "$AWS_REGION" \
    --rest-api-id "$API_ID" --stage-name "$MCP_STAGE" \
    --patch-operations \
        "op=replace,path=/*/*/throttling/rateLimit,value=5" \
        "op=replace,path=/*/*/throttling/burstLimit,value=10" > /dev/null

MCP_ENDPOINT="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com/${MCP_STAGE}/mcp"

# 6a. Fail-closed configuration gate. API Gateway returns the same 403 for an
# unsigned request and for a missing stage or path, so assert the stage and
# every method's authorization type first.
aws apigateway get-stage --region "$AWS_REGION" --rest-api-id "$API_ID" --stage-name "$MCP_STAGE" > /dev/null \
  || { echo "ERROR: stage ${MCP_STAGE} not found on $API_ID" >&2; exit 1; }
for m in POST GET DELETE; do
  t=$(aws apigateway get-method --region "$AWS_REGION" --rest-api-id "$API_ID" --resource-id "$MCP_RESOURCE_ID" \
        --http-method "$m" --query authorizationType --output text)
  [[ "$t" == "AWS_IAM" ]] || { echo "ERROR: $m /mcp authorizationType=$t, expected AWS_IAM" >&2; exit 1; }
done

# 6b. Anonymous-denial check: an unsigned request must get 403 "Missing
# Authentication Token". Retry only while the endpoint is not reachable yet.
TMP_BODY=$(mktemp /tmp/mcp-anon-check-XXXXXX)
ANON_STATUS="000"
for attempt in 1 2 3 4 5 6; do
    ANON_STATUS=$(curl -s -o "$TMP_BODY" -w '%{http_code}' -X POST \
        -H 'Content-Type: application/json' -d '{}' "$MCP_ENDPOINT" || true)
    [[ "$ANON_STATUS" == "000" ]] || break
    echo "  Endpoint not reachable yet (attempt ${attempt}/6); retrying in 5 seconds..."
    sleep 5
done
if [[ "$ANON_STATUS" != "403" ]] || ! grep -q "Missing Authentication Token" "$TMP_BODY"; then
    echo "ERROR: an unsigned request to ${MCP_ENDPOINT} returned HTTP ${ANON_STATUS}, expected 403 Missing Authentication Token." >&2
    echo "       The MCP endpoint may be reachable without SigV4. Do not register it with AWS DevOps Agent." >&2
    echo "       Check that every /mcp method uses AWS_IAM authorization, then run ./deploy/cleanup.sh and deploy again." >&2
    exit 1
fi
echo "  Verified: unsigned requests are denied (HTTP 403 Missing Authentication Token)"

# 7. SigV4 caller role for AWS DevOps Agent: trusted by aidevops.amazonaws.com
# (confused-deputy conditions), allowed execute-api:Invoke on /v1/*/mcp only
render_template "$SCRIPT_DIR/iam/devops-agent-mcp-trust-policy.json" /tmp/devops-agent-mcp-trust-rendered.json
render_template "$SCRIPT_DIR/iam/devops-agent-mcp-invoke-policy.json" /tmp/devops-agent-mcp-invoke-rendered.json
DEVOPS_AGENT_MCP_ROLE_ARN=$(ensure_role "$DEVOPS_AGENT_MCP_ROLE" /tmp/devops-agent-mcp-trust-rendered.json)
aws iam put-role-policy --role-name "$DEVOPS_AGENT_MCP_ROLE" \
    --policy-name invoke-mcp-endpoint \
    --policy-document file:///tmp/devops-agent-mcp-invoke-rendered.json

echo "  MCP endpoint: $MCP_ENDPOINT"

# ---------------------------------------------------------------------------
# CloudWatch alarm
# ---------------------------------------------------------------------------

echo ""
echo "[11/11] Creating CloudWatch alarm..."

FORWARDER_ARN=$(aws lambda get-function --function-name "webhook-forwarder" \
    --query 'Configuration.FunctionArn' --output text)

# Allow only this alarm to invoke the forwarder
add_lambda_permission webhook-forwarder cloudwatch-alarm-invoke lambda.alarms.cloudwatch.amazonaws.com \
    "arn:aws:cloudwatch:${AWS_REGION}:${AWS_ACCOUNT_ID}:alarm:${ALARM_NAME}"

# Render the alarm from deploy/cloudwatch-alarm.json:
# 100 * ErrorCount / RequestCount > 5.0 for 3 consecutive 60-second periods
sed -e "s|<ALARM_NAME>|${ALARM_NAME}|g" \
    -e "s|<FORWARDER_ARN>|${FORWARDER_ARN}|g" \
    -e "s|<CLOUDWATCH_NAMESPACE>|IntelligentRollback|g" \
    -e "s|<SERVICE_NAME>|sample-app|g" \
    "$SCRIPT_DIR/cloudwatch-alarm.json" > /tmp/cloudwatch-alarm-rendered.json

aws cloudwatch put-metric-alarm --cli-input-json file:///tmp/cloudwatch-alarm-rendered.json \
    --tags "Key=${PROJECT_TAG_KEY},Value=${PROJECT_TAG_VALUE}"

echo "  CloudWatch alarm: $ALARM_NAME (threshold: 5% error rate for 3 minutes)"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
    --query 'LoadBalancers[0].DNSName' --output text)

echo ""
echo "============================================="
echo " Deployment complete"
echo "============================================="
echo ""
echo " Application URL:  http://${ALB_DNS} (reachable from ${ALLOWED_CIDR} only)"
echo " ECS cluster:      ${ECS_CLUSTER_NAME}"
echo " CodeDeploy app:   ${CODEDEPLOY_APP_NAME} / ${CODEDEPLOY_DG_NAME}"
echo " SNS topic:        ${SNS_TOPIC_ARN}"
echo " Alarm:            ${ALARM_NAME}"
echo " Dead-letter queue: ${DLQ_NAME}"
echo " MCP endpoint:     ${MCP_ENDPOINT}"
echo ""
echo "Register in AWS DevOps Agent console > Capability Providers > MCP Server > Register"
echo "  Name:              intelligent-rollback"
echo "  Endpoint URL:      ${MCP_ENDPOINT}"
echo "  Authorization:     AWS SigV4"
echo "  IAM role:          Use an existing role -> ${DEVOPS_AGENT_MCP_ROLE_ARN}"
echo "  AWS Region:        ${AWS_REGION}          (Region of the API, NOT the Agent Space Region)"
echo "  Service Name:      execute-api"
echo "  Custom Headers:    none"
echo "  Dynamic Client Registration / private connection: leave unchecked"
echo ""
echo " Next steps (see devops-agent/README.md):"
echo "   1. Confirm the SNS email subscription"
echo "   2. Register the MCP server (above), add it to your Agent Space, and classify"
echo "      get_deployment_status and list_deployments as READ_ONLY and"
echo "      rollback_deployment, stop_deployment, notify_operators as MUTATIVE"
echo "   3. Enable directed actions on the Agent Space and add the Skill"
echo "   4. Run ./deploy/generate-traffic.sh, then Scenario A (./deploy/trigger-bad-deployment.sh)"
echo "      and afterwards Scenario B (./deploy/trigger-good-deployment.sh)"
echo "============================================="
