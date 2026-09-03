#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# deploy.sh — Deploy all infrastructure for the Intelligent Rollback sample.
#
# This script creates:
#   1. VPC with public/private subnets
#   2. ECR repository and Docker image push
#   3. ECS cluster, task definition, and service
#   4. Application Load Balancer with two target groups (blue/green)
#   5. CodeDeploy application and deployment group (ECS blue/green)
#   6. MCP Rollback Tool Lambda
#   7. Webhook Forwarder Lambda
#   8. SNS topic for notifications
#   9. CloudWatch alarm
#
# Prerequisites:
#   - AWS CLI v2 configured with admin-level credentials
#   - Docker running locally
#   - Environment variables set (see below)
#
# Usage:
#   export AWS_REGION=us-east-1
#   export NOTIFICATION_EMAIL="your-email@example.com"
#   export DEVOPS_AGENT_WEBHOOK_URL="https://..."
#   export WEBHOOK_SECRET="your-shared-secret"
#   ./deploy/deploy.sh
# ---------------------------------------------------------------------------

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_NAME="intelligent-rollback"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO_NAME="${PROJECT_NAME}-sample-app"
ECS_CLUSTER_NAME="${PROJECT_NAME}-cluster"
ECS_SERVICE_NAME="sample-app-service"
CODEDEPLOY_APP_NAME="sample-app"
CODEDEPLOY_DG_NAME="sample-app-dg"
SNS_TOPIC_NAME="${PROJECT_NAME}-notifications"
ALARM_NAME="${PROJECT_NAME}-error-rate"
NOTIFICATION_EMAIL="${NOTIFICATION_EMAIL:-}"
DEVOPS_AGENT_WEBHOOK_URL="${DEVOPS_AGENT_WEBHOOK_URL:-}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"

# Validate required environment variables
if [[ -z "$DEVOPS_AGENT_WEBHOOK_URL" ]]; then
    echo "ERROR: DEVOPS_AGENT_WEBHOOK_URL must be set"
    exit 1
fi
if [[ -z "$WEBHOOK_SECRET" ]]; then
    echo "ERROR: WEBHOOK_SECRET must be set"
    exit 1
fi

echo "============================================="
echo " Intelligent Rollback — Full Deployment"
echo " Region:  ${AWS_REGION}"
echo " Account: ${AWS_ACCOUNT_ID}"
echo "============================================="

# ---------------------------------------------------------------------------
# Step 1: VPC and Networking
# ---------------------------------------------------------------------------

echo ""
echo "[1/9] Creating VPC and networking..."

VPC_ID=$(aws ec2 create-vpc \
    --cidr-block 10.0.0.0/16 \
    --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=${PROJECT_NAME}-vpc}]" \
    --query 'Vpc.VpcId' --output text)

# Enable DNS support for ECS service discovery
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}'
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'

# Internet Gateway
IGW_ID=$(aws ec2 create-internet-gateway \
    --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=${PROJECT_NAME}-igw}]" \
    --query 'InternetGateway.InternetGatewayId' --output text)
aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"

# Public subnets (for ALB)
PUBLIC_SUBNET_1=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" --cidr-block 10.0.1.0/24 --availability-zone "${AWS_REGION}a" \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${PROJECT_NAME}-public-1}]" \
    --query 'Subnet.SubnetId' --output text)

PUBLIC_SUBNET_2=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" --cidr-block 10.0.2.0/24 --availability-zone "${AWS_REGION}b" \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${PROJECT_NAME}-public-2}]" \
    --query 'Subnet.SubnetId' --output text)

# Private subnets (for ECS tasks)
PRIVATE_SUBNET_1=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" --cidr-block 10.0.10.0/24 --availability-zone "${AWS_REGION}a" \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${PROJECT_NAME}-private-1}]" \
    --query 'Subnet.SubnetId' --output text)

PRIVATE_SUBNET_2=$(aws ec2 create-subnet \
    --vpc-id "$VPC_ID" --cidr-block 10.0.11.0/24 --availability-zone "${AWS_REGION}b" \
    --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=${PROJECT_NAME}-private-2}]" \
    --query 'Subnet.SubnetId' --output text)

# Route table for public subnets
PUBLIC_RT=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
    --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${PROJECT_NAME}-public-rt}]" \
    --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id "$PUBLIC_RT" --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW_ID"
aws ec2 associate-route-table --route-table-id "$PUBLIC_RT" --subnet-id "$PUBLIC_SUBNET_1"
aws ec2 associate-route-table --route-table-id "$PUBLIC_RT" --subnet-id "$PUBLIC_SUBNET_2"

# NAT Gateway for private subnets (ECS tasks need outbound for ECR/CloudWatch)
EIP_ALLOC=$(aws ec2 allocate-address --domain vpc --query 'AllocationId' --output text)
NAT_GW=$(aws ec2 create-nat-gateway --subnet-id "$PUBLIC_SUBNET_1" --allocation-id "$EIP_ALLOC" \
    --tag-specifications "ResourceType=natgateway,Tags=[{Key=Name,Value=${PROJECT_NAME}-nat}]" \
    --query 'NatGateway.NatGatewayId' --output text)

echo "  Waiting for NAT Gateway..."
aws ec2 wait nat-gateway-available --nat-gateway-ids "$NAT_GW"

PRIVATE_RT=$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
    --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=${PROJECT_NAME}-private-rt}]" \
    --query 'RouteTable.RouteTableId' --output text)
aws ec2 create-route --route-table-id "$PRIVATE_RT" --destination-cidr-block 0.0.0.0/0 --nat-gateway-id "$NAT_GW"
aws ec2 associate-route-table --route-table-id "$PRIVATE_RT" --subnet-id "$PRIVATE_SUBNET_1"
aws ec2 associate-route-table --route-table-id "$PRIVATE_RT" --subnet-id "$PRIVATE_SUBNET_2"

echo "  VPC: $VPC_ID"

# ---------------------------------------------------------------------------
# Step 2: ECR Repository and Docker Image
# ---------------------------------------------------------------------------

echo ""
echo "[2/9] Creating ECR repository and building Docker image..."

aws ecr create-repository --repository-name "$ECR_REPO_NAME" --region "$AWS_REGION" 2>/dev/null || true

# Authenticate Docker to ECR
aws ecr get-login-password --region "$AWS_REGION" | \
    docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Build and push the sample app image
IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}:latest"
docker build -t "$ECR_REPO_NAME" ./sample-app/
docker tag "$ECR_REPO_NAME:latest" "$IMAGE_URI"
docker push "$IMAGE_URI"

echo "  Image pushed: $IMAGE_URI"

# ---------------------------------------------------------------------------
# Step 3: IAM Roles
# ---------------------------------------------------------------------------

echo ""
echo "[3/9] Creating IAM roles..."

# ECS Task Execution Role
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

EXEC_ROLE_ARN=$(aws iam create-role \
    --role-name "${PROJECT_NAME}-ecs-execution-role" \
    --assume-role-policy-document file:///tmp/ecs-trust-policy.json \
    --query 'Role.Arn' --output text 2>/dev/null || \
    aws iam get-role --role-name "${PROJECT_NAME}-ecs-execution-role" --query 'Role.Arn' --output text)

aws iam attach-role-policy --role-name "${PROJECT_NAME}-ecs-execution-role" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

# ECS Task Role (for CloudWatch metrics publishing)
TASK_ROLE_ARN=$(aws iam create-role \
    --role-name "${PROJECT_NAME}-ecs-task-role" \
    --assume-role-policy-document file:///tmp/ecs-trust-policy.json \
    --query 'Role.Arn' --output text 2>/dev/null || \
    aws iam get-role --role-name "${PROJECT_NAME}-ecs-task-role" --query 'Role.Arn' --output text)

aws iam put-role-policy --role-name "${PROJECT_NAME}-ecs-task-role" \
    --policy-name cloudwatch-metrics \
    --policy-document '{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["cloudwatch:PutMetricData"],
            "Resource": "*",
            "Condition": {"StringEquals": {"cloudwatch:namespace": "IntelligentRollback"}}
        }]
    }'

# CodeDeploy Service Role
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

CODEDEPLOY_ROLE_ARN=$(aws iam create-role \
    --role-name "${PROJECT_NAME}-codedeploy-role" \
    --assume-role-policy-document file:///tmp/codedeploy-trust-policy.json \
    --query 'Role.Arn' --output text 2>/dev/null || \
    aws iam get-role --role-name "${PROJECT_NAME}-codedeploy-role" --query 'Role.Arn' --output text)

aws iam attach-role-policy --role-name "${PROJECT_NAME}-codedeploy-role" \
    --policy-arn arn:aws:iam::aws:policy/AWSCodeDeployRoleForECS

# Lambda Execution Role
cat > /tmp/lambda-trust-policy.json << 'EOF'
{
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"
    }]
}
EOF

LAMBDA_ROLE_ARN=$(aws iam create-role \
    --role-name "${PROJECT_NAME}-lambda-role" \
    --assume-role-policy-document file:///tmp/lambda-trust-policy.json \
    --query 'Role.Arn' --output text 2>/dev/null || \
    aws iam get-role --role-name "${PROJECT_NAME}-lambda-role" --query 'Role.Arn' --output text)

aws iam attach-role-policy --role-name "${PROJECT_NAME}-lambda-role" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Note: all CodeDeploy actions used here (StopDeployment, GetDeployment,
# BatchGetDeployments, ListDeployments) support resource-level permissions and
# are scoped to this sample's application and deployment group.
aws iam put-role-policy --role-name "${PROJECT_NAME}-lambda-role" \
    --policy-name codedeploy-and-sns \
    --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
            {
                \"Sid\": \"CodeDeployDeploymentGroupScoped\",
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"codedeploy:StopDeployment\",
                    \"codedeploy:GetDeployment\",
                    \"codedeploy:BatchGetDeployments\",
                    \"codedeploy:ListDeployments\"
                ],
                \"Resource\": [
                    \"arn:aws:codedeploy:${AWS_REGION}:${AWS_ACCOUNT_ID}:deploymentgroup:${CODEDEPLOY_APP_NAME}/${CODEDEPLOY_DG_NAME}\",
                    \"arn:aws:codedeploy:${AWS_REGION}:${AWS_ACCOUNT_ID}:application:${CODEDEPLOY_APP_NAME}\"
                ]
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": \"sns:Publish\",
                \"Resource\": \"arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SNS_TOPIC_NAME}\"
            }
        ]
    }"

# Wait for IAM propagation
echo "  Waiting for IAM role propagation..."
sleep 10

# ---------------------------------------------------------------------------
# Step 4: ECS Cluster and ALB
# ---------------------------------------------------------------------------

echo ""
echo "[4/9] Creating ECS cluster and Application Load Balancer..."

aws ecs create-cluster --cluster-name "$ECS_CLUSTER_NAME" --region "$AWS_REGION"

# Security Groups
ALB_SG=$(aws ec2 create-security-group \
    --group-name "${PROJECT_NAME}-alb-sg" \
    --description "ALB security group" \
    --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0

ECS_SG=$(aws ec2 create-security-group \
    --group-name "${PROJECT_NAME}-ecs-sg" \
    --description "ECS tasks security group" \
    --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text)

aws ec2 authorize-security-group-ingress --group-id "$ECS_SG" \
    --protocol tcp --port 8080 --source-group "$ALB_SG"

# Application Load Balancer
ALB_ARN=$(aws elbv2 create-load-balancer \
    --name "${PROJECT_NAME}-alb" \
    --subnets "$PUBLIC_SUBNET_1" "$PUBLIC_SUBNET_2" \
    --security-groups "$ALB_SG" \
    --scheme internet-facing \
    --type application \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# Two target groups for blue/green deployment
TG_BLUE_ARN=$(aws elbv2 create-target-group \
    --name "${PROJECT_NAME}-blue" \
    --protocol HTTP --port 8080 \
    --vpc-id "$VPC_ID" \
    --target-type ip \
    --health-check-path /health \
    --health-check-interval-seconds 15 \
    --healthy-threshold-count 2 \
    --query 'TargetGroups[0].TargetGroupArn' --output text)

TG_GREEN_ARN=$(aws elbv2 create-target-group \
    --name "${PROJECT_NAME}-green" \
    --protocol HTTP --port 8080 \
    --vpc-id "$VPC_ID" \
    --target-type ip \
    --health-check-path /health \
    --health-check-interval-seconds 15 \
    --healthy-threshold-count 2 \
    --query 'TargetGroups[0].TargetGroupArn' --output text)

# Production listener (port 80)
LISTENER_ARN=$(aws elbv2 create-listener \
    --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=${TG_BLUE_ARN}" \
    --query 'Listeners[0].ListenerArn' --output text)

# Test listener (port 8080) for CodeDeploy validation
TEST_LISTENER_ARN=$(aws elbv2 create-listener \
    --load-balancer-arn "$ALB_ARN" \
    --protocol HTTP --port 8080 \
    --default-actions "Type=forward,TargetGroupArn=${TG_GREEN_ARN}" \
    --query 'Listeners[0].ListenerArn' --output text)

echo "  ALB created: $ALB_ARN"

# ---------------------------------------------------------------------------
# Step 5: ECS Task Definition and Service
# ---------------------------------------------------------------------------

echo ""
echo "[5/9] Registering ECS task definition and creating service..."

# Generate task definition from template
sed -e "s|<IMAGE_URI>|${IMAGE_URI}|g" \
    -e "s|<EXEC_ROLE_ARN>|${EXEC_ROLE_ARN}|g" \
    -e "s|<TASK_ROLE_ARN>|${TASK_ROLE_ARN}|g" \
    -e "s|<AWS_REGION>|${AWS_REGION}|g" \
    deploy/taskdef.json > /tmp/taskdef-rendered.json

TASK_DEF_ARN=$(aws ecs register-task-definition \
    --cli-input-json file:///tmp/taskdef-rendered.json \
    --query 'taskDefinition.taskDefinitionArn' --output text)

# Create ECS service
aws ecs create-service \
    --cluster "$ECS_CLUSTER_NAME" \
    --service-name "$ECS_SERVICE_NAME" \
    --task-definition "$TASK_DEF_ARN" \
    --desired-count 2 \
    --launch-type FARGATE \
    --deployment-controller type=CODE_DEPLOY \
    --network-configuration "awsvpcConfiguration={subnets=[$PRIVATE_SUBNET_1,$PRIVATE_SUBNET_2],securityGroups=[$ECS_SG],assignPublicIp=DISABLED}" \
    --load-balancers "targetGroupArn=${TG_BLUE_ARN},containerName=sample-app,containerPort=8080"

echo "  ECS service created with CodeDeploy controller"

# ---------------------------------------------------------------------------
# Step 6: CodeDeploy Application and Deployment Group
# ---------------------------------------------------------------------------

echo ""
echo "[6/9] Setting up CodeDeploy blue/green deployment..."

aws deploy create-application \
    --application-name "$CODEDEPLOY_APP_NAME" \
    --compute-platform ECS

aws deploy create-deployment-group \
    --application-name "$CODEDEPLOY_APP_NAME" \
    --deployment-group-name "$CODEDEPLOY_DG_NAME" \
    --service-role-arn "$CODEDEPLOY_ROLE_ARN" \
    --deployment-config-name CodeDeployDefault.ECSLinear10PercentEvery1Minutes \
    --ecs-services "serviceName=${ECS_SERVICE_NAME},clusterName=${ECS_CLUSTER_NAME}" \
    --load-balancer-info "targetGroupPairInfoList=[{targetGroups=[{name=${PROJECT_NAME}-blue},{name=${PROJECT_NAME}-green}],prodTrafficRoute={listenerArns=[${LISTENER_ARN}]},testTrafficRoute={listenerArns=[${TEST_LISTENER_ARN}]}}]" \
    --blue-green-deployment-configuration '{
        "terminateBlueInstancesOnDeploymentSuccess": {
            "action": "TERMINATE",
            "terminationWaitTimeInMinutes": 5
        },
        "deploymentReadyOption": {
            "actionOnTimeout": "CONTINUE_DEPLOYMENT",
            "waitTimeInMinutes": 0
        }
    }' \
    --auto-rollback-configuration "enabled=true,events=DEPLOYMENT_FAILURE"

echo "  CodeDeploy deployment group: $CODEDEPLOY_DG_NAME"

# ---------------------------------------------------------------------------
# Step 7: SNS Topic
# ---------------------------------------------------------------------------

echo ""
echo "[7/9] Creating SNS notification topic..."

SNS_TOPIC_ARN=$(aws sns create-topic --name "$SNS_TOPIC_NAME" \
    --attributes KmsMasterKeyId=alias/aws/sns \
    --query 'TopicArn' --output text)

if [[ -n "$NOTIFICATION_EMAIL" ]]; then
    aws sns subscribe --topic-arn "$SNS_TOPIC_ARN" \
        --protocol email --notification-endpoint "$NOTIFICATION_EMAIL"
    echo "  Subscription created — check email to confirm"
fi

echo "  SNS Topic: $SNS_TOPIC_ARN"

# ---------------------------------------------------------------------------
# Step 8: Lambda Functions
# ---------------------------------------------------------------------------

echo ""
echo "[8/9] Deploying Lambda functions..."

# Package and deploy MCP Rollback Tool
cd mcp-rollback-tool
zip -r /tmp/mcp-rollback-tool.zip lambda_function.py
cd ..

aws lambda create-function \
    --function-name "mcp-rollback-tool" \
    --runtime python3.12 \
    --handler lambda_function.lambda_handler \
    --role "$LAMBDA_ROLE_ARN" \
    --zip-file fileb:///tmp/mcp-rollback-tool.zip \
    --timeout 30 \
    --memory-size 256 \
    --environment "Variables={CODEDEPLOY_APP_NAME=${CODEDEPLOY_APP_NAME},CODEDEPLOY_DG_NAME=${CODEDEPLOY_DG_NAME},SNS_TOPIC_ARN=${SNS_TOPIC_ARN}}" \
    2>/dev/null || \
aws lambda update-function-code \
    --function-name "mcp-rollback-tool" \
    --zip-file fileb:///tmp/mcp-rollback-tool.zip

# Package and deploy Webhook Forwarder
cd webhook-forwarder
zip -r /tmp/webhook-forwarder.zip lambda_function.py
cd ..

aws lambda create-function \
    --function-name "webhook-forwarder" \
    --runtime python3.12 \
    --handler lambda_function.lambda_handler \
    --role "$LAMBDA_ROLE_ARN" \
    --zip-file fileb:///tmp/webhook-forwarder.zip \
    --timeout 30 \
    --memory-size 256 \
    --environment "Variables={WEBHOOK_URL=${DEVOPS_AGENT_WEBHOOK_URL},WEBHOOK_SECRET=${WEBHOOK_SECRET},CODEDEPLOY_APP_NAME=${CODEDEPLOY_APP_NAME},CODEDEPLOY_DG_NAME=${CODEDEPLOY_DG_NAME}}" \
    2>/dev/null || \
aws lambda update-function-code \
    --function-name "webhook-forwarder" \
    --zip-file fileb:///tmp/webhook-forwarder.zip

echo "  Lambda functions deployed"

# ---------------------------------------------------------------------------
# Step 9: CloudWatch Alarm
# ---------------------------------------------------------------------------

echo ""
echo "[9/9] Creating CloudWatch alarm..."

# Get the Webhook Forwarder Lambda ARN for alarm action
FORWARDER_ARN=$(aws lambda get-function --function-name "webhook-forwarder" \
    --query 'Configuration.FunctionArn' --output text)

# Grant CloudWatch permission to invoke the Lambda
aws lambda add-permission \
    --function-name "webhook-forwarder" \
    --statement-id "cloudwatch-alarm-invoke" \
    --action "lambda:InvokeFunction" \
    --principal "lambda.alarms.cloudwatch.amazonaws.com" \
    --source-arn "arn:aws:cloudwatch:${AWS_REGION}:${AWS_ACCOUNT_ID}:alarm:${ALARM_NAME}" \
    2>/dev/null || true

# Create a math expression alarm: ErrorCount / RequestCount > 0.05
aws cloudwatch put-metric-alarm \
    --alarm-name "$ALARM_NAME" \
    --alarm-description "Triggers when sample-app error rate exceeds 5% over 1 minute" \
    --metrics '[
        {"Id":"errors","MetricStat":{"Metric":{"Namespace":"IntelligentRollback","MetricName":"ErrorCount","Dimensions":[{"Name":"ServiceName","Value":"sample-app"}]},"Period":60,"Stat":"Sum"},"ReturnData":false},
        {"Id":"requests","MetricStat":{"Metric":{"Namespace":"IntelligentRollback","MetricName":"RequestCount","Dimensions":[{"Name":"ServiceName","Value":"sample-app"}]},"Period":60,"Stat":"Sum"},"ReturnData":false},
        {"Id":"error_rate","Expression":"IF(requests>0, errors/requests, 0)","Label":"ErrorRate","ReturnData":true}
    ]' \
    --threshold 0.05 \
    --comparison-operator GreaterThanThreshold \
    --evaluation-periods 1 \
    --datapoints-to-alarm 1 \
    --treat-missing-data notBreaching \
    --alarm-actions "$FORWARDER_ARN"

echo "  CloudWatch alarm: $ALARM_NAME (threshold: 5% error rate)"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
    --query 'LoadBalancers[0].DNSName' --output text)

echo ""
echo "============================================="
echo " Deployment Complete!"
echo "============================================="
echo ""
echo " Application URL:  http://${ALB_DNS}"
echo " ECS Cluster:      ${ECS_CLUSTER_NAME}"
echo " CodeDeploy App:   ${CODEDEPLOY_APP_NAME}"
echo " SNS Topic:        ${SNS_TOPIC_ARN}"
echo " Alarm:            ${ALARM_NAME}"
echo ""
echo " Next steps:"
echo "   1. Confirm the SNS email subscription"
echo "   2. Register the MCP Rollback Tool with DevOps Agent"
echo "   3. Configure the AWS DevOps Agent webhook URL"
echo "   4. Deploy a bad version to test rollback"
echo ""
echo " To test rollback, deploy with high error rate:"
echo "   See README.md for instructions"
echo "============================================="
