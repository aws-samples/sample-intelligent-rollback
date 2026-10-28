# AWS DevOps Agent configuration

This folder holds what you configure in AWS DevOps Agent after `./deploy/deploy.sh` finishes: the MCP server registration, the tool classifications, directed actions, and the rollback decision Skill. Step 3 of the blog post walks through the same steps.

| File | Purpose |
|---|---|
| `mcp-tool-definition.json` | The five tools exactly as the MCP server returns them from `tools/list`, and the `toolDetails` classification for each. A sample-specific descriptor, not an AWS DevOps Agent API request. |
| `deployment-metadata-schema.json` | JSON Schema (draft 2020-12) of the incident payload `webhook-forwarder` sends, and the signing headers. |
| `skills/deployment-rollback-decision/SKILL.md` | The Skill: four-dimension decision framework (temporal correlation, error type, dependency health, blast radius) and the proposed tool call for ROLL BACK, HOLD, and ESCALATE. |

`tests/test_contract_schemas.py` checks all three against the Lambda code.

The console names below follow the AWS DevOps Agent documentation: the **AWS DevOps Agent console** holds Agent Spaces, **Capabilities**, and **Capability Providers**; the **Operator Web App** holds investigations, chat approvals, and **Knowledge**, **Skills**.

## 1. Register the MCP server

Registration is per AWS account. In the AWS DevOps Agent console, open **Capability Providers**, choose **MCP Server** under **Available providers**, and choose **Register**. Enter the values `deploy.sh` printed:

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

**AWS Region** is the SigV4 signing Region, so it must be the Region of the API (`AWS_REGION`). The role's trust policy uses the Agent Space Region (`DEVOPS_AGENT_REGION`) in `aws:SourceArn`. Deploy the sample in the Agent Space Region and both values are the same.

Review and submit. AWS DevOps Agent validates the connection by calling `initialize` and `tools/list`. The endpoint URL appears in AWS CloudTrail.

## 2. Add the tools to the Agent Space and classify them

1. Select your Agent Space, open the **Capabilities** tab, go to **MCP Servers**, and choose **Add**.
2. Select `intelligent-rollback`. Choose **Select specific tools** and select all five (or only the two read-only tools, see "Read-only alternative" below). Choose **Add**.
3. When the console asks you to classify each tool, use:

   | Tool | Classification |
   |---|---|
   | `get_deployment_status` | `READ_ONLY` |
   | `list_deployments` | `READ_ONLY` |
   | `rollback_deployment` | `MUTATIVE` |
   | `stop_deployment` | `MUTATIVE` |
   | `notify_operators` | `MUTATIVE` |

   `MUTATIVE` tools need directed actions and per-action operator approval in chat. Never classify a tool as `DESTRUCTIVE` here; the agent never calls those.

Register through the console. Tools that are registered programmatically without `toolDetails` default to `READ_ONLY`, and the agent runs them without asking for approval. If you register programmatically, set `toolDetails` from `mcp-tool-definition.json`, and confirm the classifications in the console afterwards.

## 3. Enable directed actions

Directed actions are off by default. Turn them on in the Agent Space settings in the AWS DevOps Agent console, or with the AWS CLI:

```bash
aws devops-agent update-agent-space --agent-space-id <agent-space-id> \
  --preferences elevatedActionsEnabled=true
```

Caution: `--preferences` replaces the Agent Space's full set of preferences. If you have set other preferences, include them in the same call, or use the console.

With directed actions on, the agent still never runs a `MUTATIVE` tool without approval. During a webhook-triggered (autonomous investigation) run, a mutating call fails instead of asking for approval. The flow is therefore: the investigation recommends, then you open the investigation chat, ask the agent to carry out the recommendation, and approve the request. Each approval covers one tool call on one resource. You can narrow the arguments or reject the call.

## 4. Add the Skill

Add `skills/deployment-rollback-decision/SKILL.md` in the Operator Web App: open **Knowledge**, the **Skills** tab, and choose **Add skill**. Use one of these paths:

- **Create skill**: enter the `name` and `description` from the frontmatter, set the status to active, keep agent type **Generic**, and paste the rest of the file as the instructions.
- **Upload skill**: zip the Skill directory and upload it (at most 6 MB; it must contain `SKILL.md` and no `scripts/` directory):

  ```bash
  cd devops-agent/skills && zip -r /tmp/deployment-rollback-decision.zip deployment-rollback-decision
  ```

- **Import from repository**: enter the GitHub URL of the `devops-agent/skills/deployment-rollback-decision` directory. This needs a GitHub account associated with the Agent Space.

Keep the agent type at **Generic**, which makes the Skill available to every agent type. The Skill is needed both in the webhook-triggered investigation and in the follow-up chat where you approve the action. To narrow context, you can instead select Incident Triage, Incident RCA, Incident Mitigation, and On-demand.

## 5. Validate

- **Tools.** In the AWS DevOps Agent console, **Capabilities**, **MCP Servers** lists the five tools with their classifications.
- **Skill.** In the Operator Web App, **Knowledge**, **Skills** lists `deployment-rollback-decision`.
- **Anonymous denial.** An unsigned request returns HTTP 403 with `Missing Authentication Token`:

  ```bash
  curl -s -w '\n%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{}' \
    https://<api-id>.execute-api.<AWS_REGION>.amazonaws.com/v1/mcp
  ```

- **Signed smoke test.** `deploy/mcp_smoke_test.py` signs requests with SigV4 and sends `initialize`, `notifications/initialized`, and `tools/list`; `--status` also calls the read-only `get_deployment_status`. It never calls a mutating tool. Run it from the tests virtual environment (it needs botocore) with your own operator credentials that allow `execute-api:Invoke` on `.../v1/POST/mcp`; the DevOps Agent role trusts only `aidevops.amazonaws.com`.

  ```bash
  /tmp/ir-venv/bin/python deploy/mcp_smoke_test.py \
    --endpoint https://<api-id>.execute-api.<AWS_REGION>.amazonaws.com/v1/mcp \
    --region <AWS_REGION> --status
  ```

## Read-only alternative

The MCP server and security pages of the AWS DevOps Agent documentation recommend allowlisting only read-only MCP tools. To follow that model, add only `get_deployment_status` and `list_deployments` in step 2 and skip step 3. The agent still investigates and recommends, and you run the rollback with your own credentials:

```bash
aws deploy stop-deployment --deployment-id <id> --auto-rollback-enabled
```

## If approved actions are refused

The sample creates no elevated role. The directed-actions documentation says `MUTATIVE` MCP tools need directed actions and per-action approval. The same page also lists a per-account elevated IAM role as one of the opt-in layers. If you approve a `rollback_deployment` call and AWS DevOps Agent refuses it because no elevated role is registered, register a least-privilege role:

1. Render the two templates in `deploy/iam/` (from the repository root, with the same `AWS_REGION` and `DEVOPS_AGENT_REGION` you deployed with):

   ```bash
   ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
   for f in devops-agent-elevated-trust-policy devops-agent-elevated-permissions-policy; do
     sed -e "s/<AWS_ACCOUNT_ID>/${ACCOUNT_ID}/g" \
         -e "s/<AWS_REGION>/${AWS_REGION}/g" \
         -e "s/<DEVOPS_AGENT_REGION>/${DEVOPS_AGENT_REGION:-$AWS_REGION}/g" \
         "deploy/iam/${f}.json" > "/tmp/${f}.json"
   done
   ```

   - `devops-agent-elevated-trust-policy.json` trusts `aidevops.amazonaws.com` with all three required actions (`sts:AssumeRole`, `sts:SetSourceIdentity`, `sts:TagSession`) and the `aws:SourceAccount` and `aws:SourceArn` (`.../agentspace/*`) conditions.
   - `devops-agent-elevated-permissions-policy.json` allows only `codedeploy:StopDeployment` on `deploymentgroup:sample-app/sample-app-dg`. Don't attach `AIDevOpsAgentActionsPolicy` or any policy with `*` actions or resources.

2. Create the role with the `DevOpsAgent-ElevatedAction-*` naming convention:

   ```bash
   aws iam create-role --role-name DevOpsAgent-ElevatedAction-intelligent-rollback \
     --assume-role-policy-document file:///tmp/devops-agent-elevated-trust-policy.json
   aws iam put-role-policy --role-name DevOpsAgent-ElevatedAction-intelligent-rollback \
     --policy-name stop-sample-deployment \
     --policy-document file:///tmp/devops-agent-elevated-permissions-policy.json
   ```

   If you use the IAM console instead, choose **Custom trust policy**, not **AWS service**; the AWS service type omits `sts:SetSourceIdentity` and `sts:TagSession`, and directed actions then fail at credential time.

3. With directed actions enabled, register the role ARN as the elevated role (`agentElevatedRoleArn`) on the Agent Space's AWS account association. The operator who registers it needs `iam:PassRole` on the role with the condition `iam:PassedToService` = `aidevops.amazonaws.com`.

`./deploy/cleanup.sh` deletes `DevOpsAgent-ElevatedAction-intelligent-rollback` if it exists. Deregister it from the account association first.

## Troubleshooting

- **Registration validation fails.** Check the `/aws/lambda/mcp-rollback-tool` log group. Each request logs a line `MCP request method=... tool=... caller=...`. No line means the request never reached the function: check the endpoint URL, **Service Name** `execute-api`, **AWS Region**, and the role. The server supports MCP protocol versions `2025-11-25`, `2025-06-18`, and `2025-03-26` over stateless JSON responses, with no sessions and no SSE. If the log shows a method other than `initialize`, `ping`, `tools/list`, or `tools/call`, the client expects a part of MCP this server does not implement.
- **Access error and a different Agent Space Region.** If registration validation fails with an access error and the Agent Space Region differs from `AWS_REGION`, redeploy in the Agent Space Region (simplest), or check the Region in the role's trust policy `aws:SourceArn`.
- **No approval request appears.** Ask for the action in the investigation chat, not in the webhook-triggered run. Confirm that directed actions are on and that the tool is classified `MUTATIVE`.
- **`DeploymentNotActive`.** The rollback window closed and the deployment already finished. Deploy the previous revision again to recover.
- **No investigation starts.** Check `/aws/lambda/webhook-forwarder` for the delivery status and the `intelligent-rollback-webhook-dlq` queue for failed events. A 4xx response means a signature or header problem; check that the webhook secret in Parameter Store matches the webhook.
