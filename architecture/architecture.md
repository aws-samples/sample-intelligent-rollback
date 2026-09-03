# Architecture

![Architecture Diagram](architecture.png)

This document describes the components of the Intelligent Rollback system and how they interact.

## Overview

The system implements an automated, AI-driven rollback flow. A deployment that
degrades service health triggers a CloudWatch alarm, which notifies the Amazon Q
Developer Agent for DevOps through a signed webhook. The agent evaluates the
situation and, when appropriate, invokes a custom MCP (Model Context Protocol)
tool to roll the deployment back through AWS CodeDeploy.

## Components

### Sample Application (`sample-app/`)
A Flask microservice deployed to ECS Fargate via CodeDeploy blue/green. It
exposes `/health`, `/`, and `/info` endpoints and publishes custom CloudWatch
metrics (`RequestCount`, `ErrorCount`, `Latency`) under the
`IntelligentRollback` namespace. Failure behavior is controlled by the
`ERROR_RATE` and `LATENCY_MS` environment variables, so a "bad" version can be
deployed by changing the ECS task definition without changing code.

### CloudWatch Alarm
Watches the custom error-rate / latency metrics emitted by the sample app. When
the metric breaches its threshold, the alarm transitions to `ALARM` and invokes
the Webhook Forwarder Lambda.

### Webhook Forwarder (`webhook-forwarder/`)
An AWS Lambda function that transforms the CloudWatch alarm event into the
schema expected by the AWS DevOps Agent, enriches it with current CodeDeploy
deployment context, signs the payload with HMAC-SHA256
(`X-Hub-Signature-256` header), and POSTs it to the agent's webhook endpoint.
It only forwards `ALARM` state changes.

### Amazon Q Developer Agent for DevOps
Receives the signed webhook, verifies the signature, and autonomously decides
whether a rollback is warranted. When it decides to act, it invokes the MCP
Rollback Tool.

### MCP Rollback Tool (`mcp-rollback-tool/`)
An AWS Lambda function exposed as an MCP tool. It supports rollback, stop,
status, and list actions against CodeDeploy. Rollback is executed via
`StopDeployment` with auto-rollback enabled on the deployment group.

### SNS Notifications
Operators are notified of rollback actions through an SNS topic so humans stay
informed even though the flow is automated.

## End-to-End Flow

1. Sample app is deployed via CodeDeploy blue/green to ECS Fargate.
2. CloudWatch alarm detects an anomalous error rate or latency.
3. The alarm invokes the Webhook Forwarder Lambda.
4. The Webhook Forwarder signs and posts the event to the AWS DevOps Agent webhook endpoint.
5. The AWS DevOps Agent evaluates the situation and invokes the MCP Rollback Tool.
6. The MCP Rollback Tool calls CodeDeploy `StopDeployment` with auto-rollback enabled.
7. An SNS notification is sent to operators.

## Security Notes

- Webhook payloads are authenticated with an HMAC-SHA256 signature derived from a
  shared secret (`WEBHOOK_SECRET`), so the agent can verify requests originate
  from this Lambda.
- IAM policies follow least privilege where the API supports resource-level
  permissions. `codedeploy:StopDeployment`, `GetDeployment`,
  `BatchGetDeployments`, and `ListDeployments` are scoped to this sample's
  application and deployment group; `cloudwatch:PutMetricData` does not support
  resource-level permissions and is constrained by a namespace condition.
- The demo Application Load Balancer serves plain HTTP on port 80 for simplicity.
  Production deployments should add an HTTPS listener with an ACM certificate and
  terminate TLS at the ALB.
