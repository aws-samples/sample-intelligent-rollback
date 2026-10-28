# Architecture

![An AWS CodeDeploy blue/green deployment to Amazon ECS on AWS Fargate trips an Amazon CloudWatch alarm, and the webhook-forwarder AWS Lambda function sends a signed incident to AWS DevOps Agent, which investigates with read-only tools through a SigV4-protected Amazon API Gateway MCP endpoint and the mcp-rollback-tool Lambda function. The agent recommends roll back, hold, or escalate, an operator approves the proposed action in chat before the tool changes anything, and Amazon SNS emails operators at each step.](architecture.png)

*Figure 1: Architecture of the context-aware rollback flow with operator approval*

This document describes the components of the sample and how they interact.

## Overview

A deployment that degrades service health trips an Amazon CloudWatch alarm. The alarm's AWS Lambda function sends a signed incident to AWS DevOps Agent, which investigates with read-only tools and recommends roll back, hold, or escalate. The agent changes nothing on its own: an operator asks the agent in chat to carry out the recommendation and approves the specific tool call. Only then does the custom Model Context Protocol (MCP) tool stop the deployment with rollback through AWS CodeDeploy, or notify the team through Amazon Simple Notification Service (Amazon SNS).

## End-to-end flow

The numbers match the diagram.

1. A trigger script (`deploy/trigger-bad-deployment.sh` or `deploy/trigger-good-deployment.sh`) starts a CodeDeploy blue/green deployment of the sample app on Amazon Elastic Container Service (Amazon ECS) on AWS Fargate.
2. The CloudWatch error-rate alarm reaches `ALARM` and invokes `webhook-forwarder` asynchronously.
3. The forwarder builds the incident payload, signs it, and posts it to the AWS DevOps Agent generic webhook. Then it publishes `[INVESTIGATING]` to Amazon SNS. Delivery failures raise, so Lambda retries apply, and the event finally goes to an Amazon Simple Queue Service (Amazon SQS) dead-letter queue.
4. AWS DevOps Agent runs a webhook-triggered investigation. It loads the `deployment-rollback-decision` Skill, calls only the read-only tools `get_deployment_status` and `list_deployments` plus its own AWS telemetry, and writes a recommendation (ROLL BACK, HOLD, or ESCALATE) with the evidence for each of the four dimensions and the exact proposed tool call.
5. The operator opens the investigation in the Operator Web App and, in chat, asks the agent to carry out the recommendation.
6. The agent proposes the mutating tool call, for example `rollback_deployment`. The approval request shows the tool and its arguments. The operator approves, narrows, or rejects it.
7. On approval, the tool validates the deployment and calls CodeDeploy `StopDeployment` with `autoRollbackEnabled=True`, then publishes `[ROLLBACK]` (or `[FAILED]`). For HOLD or ESCALATE, an approved `notify_operators` call publishes `[HOLD]` or `[ESCALATE]`.

## Components

### Sample application (`sample-app/`)

A Flask service that CodeDeploy deploys blue/green to ECS on Fargate. It exposes `/`, `/health`, and `/info`, and publishes `RequestCount`, `ErrorCount`, and `Latency` to the `IntelligentRollback` namespace twice per request: with the `ServiceName` dimension (the alarm uses these) and with `ServiceName` and `Version`. It also publishes `DependencyHealth` with `Dependency=payments`.

Two failure modes drive the demo:

- `ERROR_RATE` simulates a code defect in the new revision: HTTP 500 and an `AttributeError` log line.
- `DEPENDENCY_FAILURE_RATE`, overridden by the Parameter Store parameter `/intelligent-rollback/dependency-failure-rate`, simulates a payments dependency outage on every version at once: HTTP 503 and a `ConnectTimeout to payments.example.com` log line. No network call is made.

### CloudWatch alarm (`deploy/cloudwatch-alarm.json`)

A metric math alarm that computes `100 * ErrorCount / RequestCount`. When the error rate stays above 5 percent for three consecutive 60-second periods, the alarm enters `ALARM` and invokes `webhook-forwarder`. No alarm is attached to the deployment group, so CodeDeploy never rolls back on the alarm by itself.

### Webhook forwarder (`webhook-forwarder/`)

A Lambda function that handles only `ALARM` transitions. It looks up the in-flight deployment for the deployment group and builds the generic webhook payload described by `devops-agent/deployment-metadata-schema.json`:

- Fields `eventType`, `incidentId`, `action`, `priority`, `title`, `description`, `timestamp`, and `service`. The `description` carries fixed `Key: value` lines (alarm, threshold, recent datapoints, evaluation period, deployment ID, status, and times) that the Skill reads by name.
- `incidentId` is deterministic per alarm transition, so AWS DevOps Agent deduplicates Lambda retries of the same event.
- The alarm reason and the deployment description are sanitized and labeled untrusted.

It signs each attempt with the headers the generic webhook documentation defines: `Content-Type: application/json`, `x-amzn-event-timestamp` (`%Y-%m-%dT%H:%M:%S.mmmZ`), and `x-amzn-event-signature`, the base64 HMAC-SHA256 of `<timestamp>:<body>` with the secret from the Parameter Store SecureString `/intelligent-rollback/webhook-secret`. Three attempts per invocation, then Lambda retries twice, then the SQS dead-letter queue `intelligent-rollback-webhook-dlq`.

### AWS DevOps Agent

Receives the incident and starts an investigation. It follows the Skill, gathers telemetry, and calls the read-only MCP tools. A mutating MCP call during this investigation fails, because AWS DevOps Agent shows approval requests only in chat. The operator approves mutating calls in the investigation chat.

### MCP endpoint (Amazon API Gateway)

The REST API `intelligent-rollback-mcp` exposes `https://<api-id>.execute-api.<region>.amazonaws.com/v1/mcp`. `POST`, `GET`, and `DELETE` on `/mcp` all use `AWS_IAM` authorization with a Lambda proxy integration, and the stage throttles at 5 requests per second (burst 10). AWS DevOps Agent signs requests with SigV4 (service `execute-api`) using the role `intelligent-rollback-devops-agent-mcp-role`, which trusts `aidevops.amazonaws.com` and may call only this endpoint.

### MCP rollback tool (`mcp-rollback-tool/`)

A Lambda function that serves the stateless subset of MCP Streamable HTTP (`initialize`, `ping`, `tools/list`, `tools/call`, JSON responses, no sessions). It offers five tools:

| Tool | AWS DevOps Agent classification | What it does |
|---|---|---|
| `get_deployment_status` | `READ_ONLY` | Describes one deployment, or the newest one |
| `list_deployments` | `READ_ONLY` | Lists recent deployments of the deployment group |
| `rollback_deployment` | `MUTATIVE` | `StopDeployment` with auto-rollback; requires `deployment_id` and `reason` |
| `stop_deployment` | `MUTATIVE` | `StopDeployment` without rollback; requires `deployment_id` and `reason` |
| `notify_operators` | `MUTATIVE` | Publishes a hold or escalate decision to Amazon SNS |

Before any CodeDeploy call, the tool checks the deployment ID format, that the deployment belongs to the configured deployment group, and that it is still active. Errors come back as MCP tool errors (`isError: true`) with `error_type` set to validation, transient (retryable), or permanent. The tool reads its configuration from the Parameter Store parameter `/intelligent-rollback/config` and logs the tool name, argument names (never values), and the caller ARN.

### Operator approval

| Category | Items |
|---|---|
| Runs without approval (read-only) | AWS DevOps Agent's read-only investigation actions, `get_deployment_status`, `list_deployments` |
| Requires directed actions and per-action approval in chat | `rollback_deployment`, `stop_deployment`, `notify_operators` |
| Never runs from an autonomous investigation | Any mutating tool (the call fails) |
| Amazon SNS emails | `[INVESTIGATING]` (forwarder); `[ROLLBACK]`, `[FAILED]`, `[STOPPED]`, `[HOLD]`, `[ESCALATE]` (tool, after an approved call) |

Each approval covers one tool call on one resource and can't be reused. Approvals are attributable to the approving operator in AWS CloudTrail, and the investigation journal records the agent's reasoning.

### Rollback window

The deployment group uses `CodeDeployDefault.ECSLinear10PercentEvery1Minutes` (about 10 minutes of traffic shifting) and keeps the original task set for `ROLLBACK_WINDOW_MINUTES` (60 by default) afterwards. `StopDeployment` with auto-rollback works only while the deployment is active, so the operator must approve inside that window. If nobody approves in time, the deployment reaches `Succeeded` and the remedy is a new deployment of the previous revision. To keep a deployment and end the window early, the operator runs `aws deploy continue-deployment --deployment-id <id> --deployment-wait-type TERMINATION_WAIT`. CodeDeploy allows one in-flight deployment per deployment group, so the trigger scripts refuse to start while one is active.

### DevOps Agent configuration (`devops-agent/`)

- `mcp-tool-definition.json`: the five tools exactly as `tools/list` returns them, plus their classifications
- `deployment-metadata-schema.json`: JSON Schema of the webhook payload and the signing headers
- `skills/deployment-rollback-decision/SKILL.md`: the decision framework and the proposed tool call for each outcome
- `README.md`: registration, classification, directed actions, and Skill setup

## Security notes

- The MCP endpoint accepts only SigV4-signed requests. `deploy.sh` checks that every `/mcp` method uses `AWS_IAM` and that an unsigned request gets HTTP 403 before it prints the registration values. The tool also refuses any request without a caller identity.
- The webhook secret is generated by AWS DevOps Agent, read by `deploy.sh` from standard input only, and stored as a Parameter Store SecureString. It never appears in a Lambda environment variable, on a command line, or in a log.
- Each Lambda function and the ECS task have their own IAM role (`deploy/iam/`). Only the tool's role can call `StopDeployment`, and only on this sample's deployment group. `cloudwatch:PutMetricData` does not support resource-level permissions and is constrained by a namespace condition.
- The Application Load Balancer serves plain HTTP on port 80 to `ALLOWED_CIDR` only. Add an HTTPS listener with an AWS Certificate Manager (ACM) certificate for production.
