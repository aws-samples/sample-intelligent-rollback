# Rollback decision runbook

This runbook is the decision framework the AWS DevOps Agent follows when it receives a deployment health alert from the webhook forwarder. Load it into the DevOps Agent Space as a context document. It works through four dimensions, in order, and maps the outcome to one call of the `intelligent_rollback` tool (see `mcp-tool-definition.json`).

## Inputs

- The webhook payload (see `deployment-metadata-schema.json`). Key fields:
  - `alert.state_timestamp`: when the alarm entered ALARM
  - `alert.recent_datapoints` and `alert.threshold`: how far over the line the metric is, and its trend
  - `deployment.deployment_id`, `deployment.status`, `deployment.create_time`, `deployment.complete_time`: the deployment the alarm may relate to
  - `deployment.description`: changed code paths or commit details, if your pipeline sets them
- Tool calls for more context: `status` (with `deployment_id`) and `list` (recent history, for example repeated failures).
- Your own telemetry: application logs, dependency health checks, and CloudWatch metrics for upstream and downstream services.

If `deployment.active_deployment` is `false`, no deployment is in flight: do not roll back. Call `notify` with `decision: "escalate"` only if the impact is severe. If it is `null`, the lookup failed: call `status` to retry the lookup before deciding.

## Dimension 1: Temporal correlation

Does the anomaly line up with the deployment timeline?

- Compare when the degradation began (`alert.state_timestamp` minus the evaluation window, and the earliest breaching value in `alert.recent_datapoints`) with `deployment.create_time`.
- Degradation that began within the expected window after traffic started shifting points toward the deployment.
- Degradation that began before the deployment, or well after the deployment window closed, points to an external cause.
- Check canary progression. Errors that appeared in the canary (new task set) first and then grew with each traffic shift point toward the deployment. Errors that appeared everywhere at once point away from it.

## Dimension 2: Error type classification

What kind of errors are showing up?

- Points toward the deployment: null pointer or attribute errors, new exception types not seen before the deployment, errors concentrated in endpoints that map to changed code paths (`deployment.description`).
- Points away from the deployment: timeouts, connection refused, DNS failures, TLS or certificate errors, and circuit breaker trips that occur on both old and new task sets.

## Dimension 3: Dependency health

Are the services the application depends on healthy?

- Check health checks and CloudWatch metrics for upstream and downstream dependencies over the same window.
- A dependency that was already degraded before the alarm fired points away from the deployment. The errors in your service are likely a symptom.
- Healthy dependencies strengthen the case that the deployment is the cause.

## Dimension 4: Blast radius assessment

How widespread is the impact, and is it growing?

- Does the issue affect all users or a subset? Do the affected instances line up with the newly deployed task set?
- Is the impact growing (rising `alert.recent_datapoints`, more endpoints affected) or flat?
- Concurrent alarms that carry the same `deployment.deployment_id` (for example, error rate and latency together) are signals about one event. Treat them as a wider blast radius and a stronger case for action, not as separate decisions.
- A rapidly expanding blast radius calls for fast action regardless of root cause.

## Decision

| Signals | Decision | Tool call |
|---|---|---|
| Temporal correlation and error type point toward the deployment, dependencies are healthy | Roll back | `{"action": "rollback", "deployment_id": "<deployment.deployment_id>", "reason": "<evidence summary>"}` |
| Blast radius is expanding quickly and the deployment is a plausible cause | Roll back | same as above |
| Most signals point away from the deployment (for example a degraded dependency, errors that predate the deployment) and impact is narrow or flat | Hold and monitor | `{"action": "notify", "decision": "hold", "deployment_id": "<id>", "reason": "<evidence summary>"}` |
| Signals conflict, data is missing, or impact is severe but the cause is unclear | Escalate to a human | `{"action": "notify", "decision": "escalate", "deployment_id": "<id>", "reason": "<what is known and what is missing>"}` |

Rules:

- Always pass a `reason` that names the signals for and against the deployment. Operators read it in the SNS notification.
- A rollback runs `StopDeployment` with auto-rollback enabled, which reverts traffic to the previous task set. It is reversible, so act when the evidence points to the deployment.
- Never call `stop` (stop without rollback) unless a human operator asks for it.
- After a hold, keep monitoring. If later alerts for the same `deployment_id` change the picture, run the framework again.

## Handling tool responses

- `statusCode` 200: done. Report the result.
- 400 (`error_type: "validation"`): fix the input. Do not retry the same call. For example, `DeploymentNotActive` means the deployment already finished, so call `status` to see its outcome.
- 503 (`error_type: "transient"`, `retryable: true`): retry after a short wait, at most twice.
- 500 (`error_type: "permanent"`): escalate. For a failed rollback the tool has already sent a `[FAILED]` SNS notification.

## Worked example: the opening scenario

A deployment goes out at 9:00 AM. At 9:03 AM the 5xx error rate alarm fires.

1. Temporal correlation: the payment processor outage started at 8:58 AM, two minutes before the deployment. Points away.
2. Error type: the errors are connection timeouts to a third-party payment endpoint, not exceptions in the new code. Points away.
3. Dependency health: the payment service health check was already red before the alarm fired. Points away.
4. Blast radius: errors affect both the old and new task sets at a flat rate. Not decisive.

Three of four signals point away from the deployment. The agent holds:

```json
{
    "action": "notify",
    "decision": "hold",
    "deployment_id": "d-ABCDEF123",
    "reason": "Errors began 2 minutes before the deployment, are connection timeouts to the payment processor, and the payment service health check was red before the alarm. Impact is flat across old and new task sets. Holding and monitoring."
}
```

No rollback happens, and the on-call engineer sees the reasoning in the `[HOLD]` notification.
