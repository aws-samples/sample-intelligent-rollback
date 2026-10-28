# Context-aware deployment rollback with AWS DevOps Agent and MCP

This sample shows how AWS DevOps Agent can investigate a failing AWS CodeDeploy blue/green deployment and recommend whether to roll it back, while an operator keeps the final decision.

When a deployment drives up the error rate, an Amazon CloudWatch alarm sends a signed incident to AWS DevOps Agent through a generic webhook. The agent investigates with read-only tools and its own telemetry, then recommends **ROLL BACK**, **HOLD**, or **ESCALATE** with the exact tool call it proposes. An operator opens the investigation, asks the agent in chat to carry out the recommendation, and approves the action. Only then does the custom Model Context Protocol (MCP) tool stop the deployment with rollback or notify the team.

## Architecture

![An AWS CodeDeploy blue/green deployment to Amazon ECS on AWS Fargate trips an Amazon CloudWatch alarm, and the webhook-forwarder AWS Lambda function sends a signed incident to AWS DevOps Agent, which investigates with read-only tools through a SigV4-protected Amazon API Gateway MCP endpoint and the mcp-rollback-tool Lambda function. The agent recommends roll back, hold, or escalate, an operator approves the proposed action in chat before the tool changes anything, and Amazon SNS emails operators at each step.](architecture/architecture.png)

See [architecture/architecture.md](architecture/architecture.md) for a detailed description.

### Flow

1. A trigger script starts an AWS CodeDeploy blue/green deployment of the sample app on Amazon Elastic Container Service (Amazon ECS) on AWS Fargate.
2. The CloudWatch error-rate alarm reaches `ALARM` and invokes the `webhook-forwarder` AWS Lambda function.
3. The forwarder builds the documented incident payload (deployment ID, timing, threshold, datapoints), signs it with HMAC-SHA256, and posts it to the AWS DevOps Agent generic webhook. It then emails `[INVESTIGATING]` through Amazon Simple Notification Service (Amazon SNS).
4. AWS DevOps Agent runs a webhook-triggered investigation. It follows the `deployment-rollback-decision` Skill, calls only the read-only tools `get_deployment_status` and `list_deployments`, and writes a recommendation with the proposed tool call.
5. An operator opens the investigation in the Operator Web App and asks the agent in chat to carry out the recommendation.
6. The agent proposes the mutating tool call (for example `rollback_deployment`). The operator approves, narrows, or rejects it.
7. On approval, the tool validates the deployment, calls CodeDeploy `StopDeployment` with auto-rollback enabled, and emails `[ROLLBACK]` (or `[FAILED]`). For HOLD or ESCALATE, an approved `notify_operators` call emails `[HOLD]` or `[ESCALATE]`.

### What needs approval

| Category | Items |
|---|---|
| Runs without approval (read-only) | AWS DevOps Agent's own read-only investigation actions, `get_deployment_status`, `list_deployments` |
| Requires directed actions and per-action approval in chat | `rollback_deployment`, `stop_deployment`, `notify_operators` |
| Never runs from an autonomous investigation | Any mutating tool. AWS DevOps Agent shows approval requests only in chat, so a mutating call during a webhook-triggered investigation fails. |
| Amazon SNS emails | `[INVESTIGATING]` from the forwarder for every forwarded alarm; `[ROLLBACK]`, `[FAILED]`, `[STOPPED]`, `[HOLD]`, `[ESCALATE]` from the tool after an approved call |

If nobody approves before the rollback window closes, the deployment reaches `Succeeded`, `rollback_deployment` returns `DeploymentNotActive`, and the remedy is a new deployment of the previous revision.

## Prerequisites

- An AWS account and credentials that can create the resources listed in [deploy/deploy.sh](deploy/deploy.sh)
- AWS Command Line Interface (AWS CLI) v2
- Docker, running locally (builds the sample image)
- `curl` and `python3` (used by the deploy scripts)
- Python 3.12, only for the local unit tests and `deploy/mcp_smoke_test.py`
- Access to AWS DevOps Agent in a [supported Region](https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-supported-regions.html), with an Agent Space you can configure. Deploy the sample in the Agent Space Region.
- Your public IPv4 address for `ALLOWED_CIDR` (find it with `curl -s https://checkip.amazonaws.com` and add `/32`). The Application Load Balancer accepts HTTP only from this range. IPv6 is not supported by this sample.

**Cost.** This sample creates billable resources. The NAT gateway and the Application Load Balancer are the main hourly costs, plus two small Fargate tasks. Amazon API Gateway, Lambda, Amazon SNS, Amazon Simple Queue Service (Amazon SQS), CloudWatch, and AWS Systems Manager Parameter Store costs are minor at demo traffic. AWS DevOps Agent usage is billed separately; see its pricing page. Check the pricing pages for your Region, and run the clean-up script when you finish.

**Time.** The deployment takes about 10-15 minutes (the NAT gateway wait is about 5 minutes). Running Scenario A and then Scenario B takes about 1 hour if you end Scenario B's rollback window with `continue-deployment`.

## Deployment

### Step 1: Create the generic webhook

1. In the **AWS DevOps Agent console**, select your Agent Space, choose the **Capabilities** tab, then **Webhook**, **Configure**, and **Add webhook**.
2. Choose **HMAC**. AWS DevOps Agent generates the signing secret.
3. Copy the webhook endpoint URL and store the secret securely. You can't retrieve the secret again; if you lose it, rotate the webhook and download the new `.csv` file.

### Step 2: Deploy the sample

```bash
git clone https://github.com/aws-samples/sample-intelligent-rollback.git
cd sample-intelligent-rollback

export AWS_REGION=us-east-1                      # the Agent Space Region
export NOTIFICATION_EMAIL="your-email@example.com"
export ALLOWED_CIDR="203.0.113.7/32"             # your IPv4 address, /32
export DEVOPS_AGENT_WEBHOOK_URL="https://..."    # from the webhook dialog
export ROLLBACK_WINDOW_MINUTES=60                # optional, 5..120, default 60
export DEVOPS_AGENT_REGION=us-east-1             # optional, default AWS_REGION

./deploy/deploy.sh
```

`deploy.sh` reads the webhook signing secret only from standard input. On a terminal it prompts without echo; paste the secret. For a non-interactive run, use `./deploy/deploy.sh < secret-file`, where `secret-file` contains only the secret value on one line. Don't use the downloaded `.csv` file (it also contains the URL), and don't put the secret in an environment variable: `deploy.sh` exits if `WEBHOOK_SECRET` is set. The script stores the secret as a Parameter Store SecureString right after reading it.

`deploy.sh` creates the VPC, Amazon Elastic Container Registry (Amazon ECR) repository and image, ECS cluster and service, Application Load Balancer, CodeDeploy application and deployment group, SNS topic, both Lambda functions with an SQS dead-letter queue, the API Gateway MCP endpoint and the IAM role AWS DevOps Agent uses to call it, and the CloudWatch alarm. Before it finishes, it confirms that every `/mcp` method requires SigV4 and that an unsigned request gets HTTP 403. Then it prints the MCP registration values.

Confirm the SNS subscription email.

Run `deploy.sh` once. To deploy again, run `./deploy/cleanup.sh` first; `deploy.sh` exits early if it finds this sample's VPC or REST API.

### Step 3: Connect the MCP server and add the Skill

Follow [devops-agent/README.md](devops-agent/README.md). In short:

1. Register the MCP server on the **Capability Providers** page with the values `deploy.sh` printed:

   ```
   Register in AWS DevOps Agent console > Capability Providers > MCP Server > Register
     Name:              intelligent-rollback
     Endpoint URL:      https://<api-id>.execute-api.<AWS_REGION>.amazonaws.com/v1/mcp
     Authorization:     AWS SigV4
     IAM role:          Use an existing role -> arn:aws:iam::<account>:role/intelligent-rollback-devops-agent-mcp-role
     AWS Region:        <AWS_REGION>          (Region of the API, NOT the Agent Space Region)
     Service Name:      execute-api
     Custom Headers:    none
     Dynamic Client Registration / private connection: leave unchecked
   ```

2. Add the server to your Agent Space with all five tools, and classify `get_deployment_status` and `list_deployments` as `READ_ONLY` and `rollback_deployment`, `stop_deployment`, and `notify_operators` as `MUTATIVE`.
3. Enable directed actions on the Agent Space.
4. Add the [deployment-rollback-decision Skill](devops-agent/skills/deployment-rollback-decision/SKILL.md) in the Operator Web App (**Knowledge**, **Skills**), with agent type **Generic**.

### Read-only alternative

The AWS DevOps Agent MCP documentation recommends allowlisting only read-only MCP tools. If you prefer that model, add only `get_deployment_status` and `list_deployments` to the Agent Space and leave directed actions off. The agent still investigates and recommends. To act on a ROLL BACK recommendation, run the rollback yourself with your own credentials:

```bash
aws deploy stop-deployment --deployment-id <id> --auto-rollback-enabled
```

## Usage

Run Scenario A first, then Scenario B. CodeDeploy runs one deployment at a time per deployment group, and a deployment you keep stays in flight during the rollback window. The trigger scripts refuse to start while a deployment is in flight and print the commands that end it.

The deployment group keeps the original task set for `ROLLBACK_WINDOW_MINUTES` (60 by default) after traffic shifting, which takes about 10 minutes. A rollback works only while the deployment is active, so approve inside that window. While you decide, the new revision keeps serving traffic. For production, consider CodeDeploy alarm-based automatic rollback at a high, unambiguous threshold as a backstop that runs outside the agent.

### Scenario A: a bad deployment (expected: ROLL BACK)

1. In one terminal, start traffic. The alarm divides errors by requests, so it needs steady traffic.

   ```bash
   ./deploy/generate-traffic.sh            # 5 requests/second for 30 minutes
   ```

2. In another terminal, start release 2.0.0. The sample simulates a code defect with `ERROR_RATE=0.5` (no code change), and the deployment description names the simulated changed handler.

   ```bash
   ./deploy/trigger-bad-deployment.sh
   ```

3. The alarm fires during traffic shifting, usually 4-6 minutes in. You receive the `[INVESTIGATING]` email.
4. Open the investigation in the Operator Web App. Review the ROLL BACK recommendation and its evidence.
5. In chat, ask the agent to carry it out, for example "Roll back deployment d-ABCDEF123 as recommended". Approve the `rollback_deployment` request.
6. Validate: the CodeDeploy console shows the deployment stopped with rollback, and the `[ROLLBACK]` email arrives.

If you rejected the rollback, end the deployment before Scenario B:

```bash
aws deploy stop-deployment --deployment-id <id> --auto-rollback-enabled
```

### Scenario B: a dependency outage during a good deployment (expected: HOLD)

1. Keep `./deploy/generate-traffic.sh` running. Wait until the alarm is `OK`, and confirm that no deployment is in flight (the second command prints nothing):

   ```bash
   aws cloudwatch describe-alarms --alarm-names intelligent-rollback-error-rate \
     --query 'MetricAlarms[0].StateValue' --output text
   aws deploy list-deployments --application-name sample-app --deployment-group-name sample-app-dg \
     --include-only-statuses Created Queued InProgress Baking Ready --output text
   ```

   If the second command prints an ID, end that deployment with the `continue-deployment` or `stop-deployment` command the trigger script prints.

2. Start release 2.1.0 (description: logging change only). The script prints the deployment ID.

   ```bash
   ./deploy/trigger-good-deployment.sh
   ```

3. Right away, simulate a payments dependency outage on both task sets:

   ```bash
   aws ssm put-parameter --name /intelligent-rollback/dependency-failure-rate \
     --type String --value 0.3 --overwrite
   ```

   Within 15 seconds (the app's cache interval), both versions fail about 30 percent of requests with `ConnectTimeout to payments.example.com`.

4. The alarm fires about 3-4 minutes later, while the deployment is still `InProgress`, so the incident says "Deployment in flight: yes" with the 2.1.0 deployment ID.
5. The agent finds that the per-version error rate is about the same on `Version=1.0.0` and `Version=2.1.0`, that most errors are on the old version 1.0.0 (which still carries most traffic), that `DependencyHealth` is 0 for `Dependency=payments`, and that the deployment description says logging change only. It recommends HOLD: the errors are on the old version 1.0.0 and the dependency, not on the new revision.
6. Optionally, ask the agent in chat to notify the team, and approve `notify_operators` with `decision: hold`.
7. Reset the dependency. The alarm returns to `OK`.

   ```bash
   aws ssm put-parameter --name /intelligent-rollback/dependency-failure-rate \
     --type String --value 0.0 --overwrite
   ```

   Traffic shifting finishes in about 10 minutes, and then CodeDeploy keeps the original task set for the rollback window. To finish now:

   ```bash
   aws deploy continue-deployment --deployment-id <id> --deployment-wait-type TERMINATION_WAIT
   ```

Validate: no rollback happens, the deployment reaches `Succeeded` after `continue-deployment` (or after the rollback window), and, if you approved the call, the `[HOLD]` email arrives with the `continue-deployment` command.

**Why the order matters.** The alarm has to change state while the deployment is in flight. If you set the parameter first, the alarm fires before the deployment, the forwarder reports no deployment in flight, and the deployment that follows never triggers a new alarm transition.

### Inspect failed webhook deliveries

The forwarder retries delivery three times per invocation, and Lambda retries a failed invocation twice. Events that still fail go to the `intelligent-rollback-webhook-dlq` SQS queue, kept for 14 days:

```bash
QUEUE_URL=$(aws sqs get-queue-url --queue-name intelligent-rollback-webhook-dlq --query QueueUrl --output text)
aws sqs receive-message --queue-url "$QUEUE_URL" --max-number-of-messages 10 --attribute-names All
```

Each message holds the original alarm event (`requestPayload`) and the error (`responsePayload`). Check `/aws/lambda/webhook-forwarder` in CloudWatch Logs for the HTTP status of each attempt.

### Run the unit tests

The tests mock boto3 and make no AWS calls. Run them from the repository root:

```bash
python3 -m venv /tmp/ir-venv
/tmp/ir-venv/bin/pip install -r tests/requirements-dev.txt
/tmp/ir-venv/bin/python -m unittest discover -s tests -v
```

## Configuration

`deploy.sh` inputs:

| Variable | Description | Default |
|---|---|---|
| `AWS_REGION` | Region for every resource. Use the Agent Space Region. | (required) |
| `NOTIFICATION_EMAIL` | Email address subscribed to the SNS topic | (required) |
| `ALLOWED_CIDR` | IPv4 range allowed to reach the Application Load Balancer on port 80; `/0` is rejected | (required) |
| `DEVOPS_AGENT_WEBHOOK_URL` | Generic webhook endpoint URL (https only) | (required) |
| `ROLLBACK_WINDOW_MINUTES` | CodeDeploy termination wait after traffic shifting (5-120) | `60` |
| `DEVOPS_AGENT_REGION` | Agent Space Region, used in the MCP caller role's trust policy | `AWS_REGION` |

Lambda function environment, set by `deploy.sh`:

| Variable | Function | Description |
|---|---|---|
| `CONFIG_PARAMETER_NAME` | mcp-rollback-tool | Parameter with `application_name`, `deployment_group`, `sns_topic_arn` (JSON): `/intelligent-rollback/config` |
| `CODEDEPLOY_APP_NAME`, `CODEDEPLOY_DG_NAME` | both | CodeDeploy application and deployment group: `sample-app`, `sample-app-dg` |
| `SNS_TOPIC_ARN` | both | Topic for operator emails |
| `WEBHOOK_URL` | webhook-forwarder | Generic webhook endpoint (https only) |
| `WEBHOOK_SECRET_PARAMETER` | webhook-forwarder | SecureString parameter with the signing secret: `/intelligent-rollback/webhook-secret` |
| `ROLLBACK_WINDOW_MINUTES` | webhook-forwarder | Window quoted in the `[INVESTIGATING]` email |

Sample app environment (`deploy/taskdef.json` and the trigger scripts): `ERROR_RATE`, `DEPENDENCY_FAILURE_RATE`, `DEPENDENCY_FAILURE_PARAMETER`, `LATENCY_MS`, `APP_VERSION`. See the docstring in [sample-app/app.py](sample-app/app.py).

## Clean up

```bash
./deploy/cleanup.sh
```

The script stops any in-flight deployment, then deletes the alarm, the Lambda functions and REST API, the dead-letter queue, the SNS topic, the parameters, the CodeDeploy application, the ECS service, cluster, and task definitions, the load balancer and target groups, the ECR repository, the NAT gateway, this sample's Elastic IP addresses and the VPC, the IAM roles, and the log groups. It reports anything it could not delete and exits non-zero in that case.

Remove these manually in the AWS DevOps Agent console:

- The MCP server, from your Agent Space (**Capabilities**, **MCP Servers**)
- The MCP server registration (**Capability Providers**, **MCP Server**)
- The generic webhook (**Capabilities**, **Webhook**)
- The `deployment-rollback-decision` Skill (Operator Web App, **Knowledge**, **Skills**)
- The elevated role registration on the AWS account association, if you registered one
- The Agent Space, if you created it only for this sample

## Security

- **MCP endpoint.** API Gateway uses `AWS_IAM` (SigV4) authorization on every `/mcp` method. Only `intelligent-rollback-devops-agent-mcp-role` can call it (`execute-api:Invoke` on `/v1/*/mcp`), and that role trusts only `aidevops.amazonaws.com` with `aws:SourceAccount` and `aws:SourceArn` conditions. As a second check, the tool refuses any request without a caller identity. Stage throttling is 5 requests per second with a burst of 10.
- **Direct invocation.** The function's resource policy lets only API Gateway, scoped to this API's `/mcp` resource, invoke `mcp-rollback-tool`. Principals in your account that hold `lambda:InvokeFunction` on the function (for example administrators) can still invoke it directly with a hand-built event, which bypasses AWS DevOps Agent approval. Restrict `lambda:InvokeFunction` on this function with IAM. The caller ARN the tool logs is trustworthy only for requests that come through API Gateway.
- **Approval.** `rollback_deployment`, `stop_deployment`, and `notify_operators` run only after directed actions are enabled and an operator approves each call in chat. Approvals are attributable to the approving operator in AWS CloudTrail. Tools registered programmatically without `toolDetails` default to `READ_ONLY` and run without approval, so register the server in the console and confirm the classifications afterwards.
- **Tool-side limits.** Before any CodeDeploy call, the tool checks the deployment ID format, that the deployment belongs to this sample's deployment group, and that it is still active. Its IAM role can call `StopDeployment` only on that deployment group.
- **Prompt injection.** Deployment descriptions and alarm reasons are text that whoever creates a deployment or edits an alarm controls, and that text reaches the agent. The forwarder sanitizes both fields and labels them untrusted, the Skill tells the agent to treat them as data, the tool limits every call to this sample's deployment group, and every mutating call needs operator approval. Restrict who can create deployments (`codedeploy:CreateDeployment`) and edit alarms. See [AWS DevOps Agent security](https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security.html).
- **Webhook signing.** The forwarder signs each request per the generic webhook documentation: `x-amzn-event-timestamp` and `x-amzn-event-signature` (base64 HMAC-SHA256 over `<timestamp>:<body>`). The secret lives only in Parameter Store as a SecureString; it is never in a Lambda environment variable, a command line, or a log.
- **Network.** The Application Load Balancer accepts port 80 from `ALLOWED_CIDR` only. The 8080 test listener is not reachable. Tasks run in private subnets and accept traffic only from the load balancer.
- **IAM.** Each Lambda function and the ECS task have their own least-privilege role (see [deploy/iam/](deploy/iam/)). The sample creates no elevated role for AWS DevOps Agent.

### Production hardening

- **TLS.** The demo load balancer serves plain HTTP. Add an HTTPS listener with an AWS Certificate Manager (ACM) certificate before you send real traffic.
- **API Gateway resource policy (optional).** Add a resource policy that allows `execute-api:Invoke` only for the AWS DevOps Agent role (and the operator role you use for smoke tests). It also blocks smoke tests from any other principal.
- **Metrics.** The sample app calls `PutMetricData` synchronously on every request, which is fine at demo traffic. For production traffic, use the CloudWatch embedded metric format (EMF) or batch the metrics.
- **Infrastructure as code.** `deploy.sh` is a linear AWS CLI script meant to run once. For anything long-lived, move the resources to AWS CloudFormation, AWS SAM, or AWS CDK.

To report a security issue, see [SECURITY.md](SECURITY.md) and [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications).

## Repository structure

```
.
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── SECURITY.md
├── architecture/
│   ├── architecture.md                  # Components, flow, and security notes
│   └── architecture.png
├── deploy/
│   ├── appspec.yaml                     # CodeDeploy ECS AppSpec template
│   ├── cleanup.sh                       # Deletes every resource deploy.sh created
│   ├── cloudwatch-alarm.json            # Error-rate alarm (5 percent for 3 minutes)
│   ├── deploy.sh                        # Deploys the sample infrastructure
│   ├── generate-traffic.sh              # Steady HTTP traffic for the demo
│   ├── iam/
│   │   ├── devops-agent-elevated-permissions-policy.json  # Contingency template, not applied
│   │   ├── devops-agent-elevated-trust-policy.json        # Contingency template, not applied
│   │   ├── devops-agent-mcp-invoke-policy.json            # execute-api:Invoke on /v1/*/mcp
│   │   ├── devops-agent-mcp-trust-policy.json             # Trust for aidevops.amazonaws.com
│   │   ├── ecs-task-role-policy.json
│   │   ├── lambda-trust-policy.json
│   │   ├── mcp-rollback-tool-policy.json
│   │   └── webhook-forwarder-policy.json
│   ├── lib/
│   │   ├── create-deployment.sh         # Shared logic for the trigger scripts
│   │   ├── taskdef_transform.py         # Builds the new task definition revision
│   │   └── validate.py                  # Input validators for the scripts
│   ├── mcp_smoke_test.py                # SigV4 smoke test (read-only tools)
│   ├── taskdef.json                     # ECS task definition template
│   ├── trigger-bad-deployment.sh        # Scenario A
│   └── trigger-good-deployment.sh       # Scenario B
├── devops-agent/
│   ├── README.md                        # MCP registration, approval, and Skill setup
│   ├── deployment-metadata-schema.json  # JSON Schema of the webhook payload
│   ├── mcp-tool-definition.json         # Tools, schemas, and classifications
│   └── skills/
│       └── deployment-rollback-decision/
│           └── SKILL.md                 # Four-dimension decision framework
├── mcp-rollback-tool/
│   ├── lambda_function.py               # Tool actions: status, list, rollback, stop, notify
│   ├── mcp_server.py                    # MCP Streamable HTTP layer
│   └── requirements.txt
├── sample-app/
│   ├── Dockerfile
│   ├── app.py                           # Flask app with failure injection
│   └── requirements.txt
├── tests/
│   ├── __init__.py
│   ├── _fixtures.py
│   ├── _loader.py
│   ├── requirements-dev.txt
│   ├── test_contract_schemas.py
│   ├── test_deploy_assets.py
│   ├── test_mcp_rollback_tool.py
│   ├── test_mcp_server.py
│   ├── test_sample_app.py
│   └── test_webhook_forwarder.py
└── webhook-forwarder/
    ├── lambda_function.py               # CloudWatch alarm to AWS DevOps Agent webhook
    └── requirements.txt
```

## License

This library is licensed under the Apache-2.0 License. See the [LICENSE](LICENSE) file.
