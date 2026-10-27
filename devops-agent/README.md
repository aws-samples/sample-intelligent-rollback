# DevOps Agent Space configuration

These files are the three pieces Step 3 of the blog post configures in the AWS DevOps Agent Space.

| File | Purpose |
|---|---|
| `mcp-tool-definition.json` | How the agent calls the `mcp-rollback-tool` Lambda function: actions, inputs, and the response body. |
| `deployment-metadata-schema.json` | JSON Schema (draft 2020-12) of the payload the `webhook-forwarder` Lambda function sends, including the deployment ID used for correlation. |
| `rollback-decision-runbook.md` | The four-dimension decision framework (temporal correlation, error type classification, dependency health, blast radius) and the tool call for each outcome: roll back, hold and monitor, or escalate. |

## Notes

- `mcp-tool-definition.json` uses the generic MCP tool shape from the Model Context Protocol specification (`name`, `title`, `description`, `inputSchema`, `outputSchema`). Product-specific registration fields for AWS DevOps Agent are not included because they could not be verified for this sample. Map these fields to whatever the DevOps Agent console or API asks for when you register the tool.
- The Lambda function accepts `action` case-insensitively. The schema lists lowercase values only, so any input that passes the schema is accepted by the function, and the function rejects unknown fields the same way the schema does.
- Both files are checked against the Lambda code by `tests/test_contract_schemas.py`.

## Access

Grant the agent's IAM role `lambda:InvokeFunction` on the `mcp-rollback-tool` function ARN only. Do not add a Lambda function URL or a resource policy with a public principal; the tool is reachable through IAM-authenticated invocation only.
