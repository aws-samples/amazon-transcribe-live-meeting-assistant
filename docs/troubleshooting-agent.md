---
title: "AI Troubleshooting Agent"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# AI Troubleshooting Agent

> **Status: foundation release.** The agent, its tools and its prompt are
> deployed, but there is **no entry point in the LMA web UI yet**. Invoke it
> with the AWS CLI or SDK as shown below. The Admin-only troubleshooting page
> and the per-resource "Diagnose" button are a later change.

An agent that answers "why did this fail?" about a deployed LMA stack by
reading that stack's own CloudWatch logs, its Virtual Participant records and
its CloudFormation parameters, and reporting a cause together with the evidence
it rests on. It is built on the Strands Agents SDK and Amazon Bedrock, the same
stack the Meeting Assistant uses.

It reads and does not change anything it diagnoses: no permission to restart,
delete or redeploy, and no ability to write to any table or bucket. The grants
that are not reads do not touch the deployment either: publishing a progress
step to the UI through a single GraphQL mutation, cancelling a log query it
started, writing its own log stream, and sending its own X-Ray traces.

## Why it exists

Diagnosing LMA means reading several CloudWatch log groups in the right order.
A meeting that produced no transcript looks identical whether the WebSocket
upgrade was refused, the speech engine declined the stream, the Kinesis
consumer threw, or the Virtual Participant never got into the meeting — and the
evidence that distinguishes those lives in four different log groups whose names
cannot be derived from the stack name you typed.

The agent does that reading. It does not replace
[Troubleshooting](troubleshooting.md) or
[Monitoring & Alerting](monitoring.md); it is the thing to reach for once an
alarm or a user report has told you something is wrong and you need to know
what.

## Using it

The function's ARN is a stack output, `TroubleshootingAgentFunctionArn`:

```bash
FUNCTION=$(aws cloudformation describe-stacks --stack-name <your-lma-stack> \
  --query "Stacks[0].Outputs[?OutputKey=='TroubleshootingAgentFunctionArn'].OutputValue" \
  --output text)

aws lambda invoke \
  --function-name "$FUNCTION" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"query": "why did virtual participant vp-abc123 fail to join?", "vpId": "vp-abc123"}' \
  diagnosis.json >/dev/null

jq -r '.body | fromjson | .answer' diagnosis.json
```

The function returns `{"statusCode": ..., "body": "<json>"}`, so the diagnosis
is inside the encoded `body`. Writing to a file rather than `/dev/stdout`
keeps the CLI's own response metadata out of the way. Read the whole body —
the token usage and the `truncated` flag as well as the answer — with
`jq -r '.body | fromjson' diagnosis.json`.

Accepted fields, all optional except that a `query` is strongly advised:

| Field | Meaning |
|---|---|
| `query` | The question. Defaults to "diagnose the most recent failure". |
| `vpId` | A Virtual Participant id to scope the investigation to. |
| `callId` | A meeting id to scope to. Also the channel thinking steps stream on. |
| `messageId` | When present, each tool call is streamed to the UI's thinking-step panel over the existing `onAddChatToken` subscription. |

Because there is no UI entry point, the only way to invoke it is with an IAM
principal that holds `lambda:InvokeFunction` on the function. **The stack grants
that to no principal**, so unless you already have it through a broad
account-level policy such as an administrator role, you have to grant it
deliberately.

## What it answers with

Three sections, always in this order:

```
## Root Cause
**Confidence:** HIGH | MEDIUM | LOW
...one or two sentences, or "Not determined"...

## Recommendations
...at most three, most useful first...

<details><summary><strong>Evidence</strong></summary>
...the log groups, streams, lines, records and parameters the cause rests on...
</details>
```

`Not determined`, with the missing evidence named, is a deliberate and
acceptable answer. The prompt forbids offering likely causes: a diagnosis you
cannot check is worse than none, because you act on it.

## What it reads

Four tools, each read-only:

| Tool | Reads | Why |
|---|---|---|
| `get_stack_parameters` | `cloudformation:DescribeStacks` on your LMA stack | Which components this deployment actually runs, and therefore where evidence can exist at all. |
| `discover_log_groups` | `logs:DescribeLogGroups`, plus `cloudformation:ListStackResources` to resolve this deployment's own nested stacks | The log group names, their owning component, and how many bytes each holds. |
| `search_logs` | CloudWatch Logs Insights (`logs:StartQuery`) | Every "why", timeline, count and cross-component correlation. |
| `get_vp_record` | `dynamodb:GetItem` on the VirtualParticipant table | One Virtual Participant's current status and its structured exit code. |

`get_stack_parameters` returns parameter values, because deployment
configuration is what the agent is reading them for — but any parameter whose
name suggests a credential, an email address or other personal data is reported
by name only. That is defence in depth rather than the control: this solution's
five third-party credential parameters are all declared `NoEcho`, so
CloudFormation already masks them in `DescribeStacks` output.

`get_vp_record` returns a fixed allowlist of diagnostic attributes. The meeting
passcode, the VNC password, and the fields identifying who owns or shares the
Virtual Participant are never returned — their names are reported so the agent
knows the record was filtered and does not treat the absence as a finding, but
their values do not reach the model or the thinking-step stream. A field added
to the row in a future release is withheld until it is added to that list
deliberately.

Two design points worth knowing, because they are what make the answers
trustworthy:

**Log group names are discovered, never constructed.** An LMA deployment's log
groups sit in four different namespaces: most are named after the *generated*
nested-stack name, a few after the parent stack, the MicroVM groups live under
`/aws/lambda-microvms/`, and AppSync's group is named after the API id. The
agent refuses to query a name that `discover_log_groups` did not return, because
a guessed name produces an empty result that reads exactly like an absence of
evidence.

**`VPLaunchType` is a first-class input.** Under the default `MICROVM`, the
Virtual Participant's Fargate log group exists and is permanently empty and no
ECS task-stopped event is ever emitted. An agent that did not know this would
report "no evidence the Virtual Participant ran" on a perfectly healthy
deployment. It reads the parameter first and routes accordingly.

It also reports, for every log group, whether that group holds any data at all —
so "this path is unused in this deployment" and "this component failed silently"
stay distinguishable.

**Discovery is scoped to this deployment, and says so when it cannot be.** The
agent resolves your stack's own nested stacks by name before looking for log
groups, so an account holding both `LMA` and `LMA-dev` never gets the second
one's log lines reported as the first one's evidence. If that lookup is not
possible it falls back to matching on the stack-name prefix and tells the agent
that a neighbouring deployment's groups may be included, rather than degrading
silently. Likewise, if the `TROUBLESHOOTING_MAX_LOG_GROUPS` cap cuts the list
short, the agent is told the list is incomplete so a group it cannot see is not
read as a group that does not exist.

## Tuning the prompt

The agent's judgement is its system prompt, and the prompt is meant to be
edited. It lives in DynamoDB, in the same table as the summarization prompt
templates, as a Default/Custom pair:

| Item | Behaviour |
|---|---|
| `DefaultTroubleshootingAgentPrompt` | Shipped with the stack and **rewritten whenever a stack update ships a changed prompt**. Edits to it are lost. |
| `CustomTroubleshootingAgentPrompt` | Created empty if it is absent, on create *and* on update, so it is there on an upgraded deployment as well as a new one. Once it exists it is never written again, so your prompt survives every later upgrade. |

To override it, set a `systemPrompt` attribute on
`CustomTroubleshootingAgentPrompt`. The change applies to the **next
diagnosis** — no redeployment, no Lambda update. Removing the attribute, or
setting it to an empty string or `NONE`, reverts to the shipped default. The
stack output `TroubleshootingAgentCustomPrompt` is a direct link to that item in
the DynamoDB console; `<br>` is accepted as a line break there, since the
console has no good way to type one.

A very short value is treated as a truncated paste rather than a prompt and the
default is used instead. If neither item holds a usable prompt the agent
refuses to run rather than improvising one — an agent that has not been told
what LMA does not persist reads an empty log group as evidence.

The shipped prompt's error-pattern catalogue is seeded from this project's own
CHANGELOG history, in rough order of how often each class has actually
occurred: meeting-platform UI drift, end-of-meeting misdetection, audio-pipeline
silence or duplication, speech-session lifecycle on long meetings, voice
assistant session management, fresh-deploy permission and key gaps, URL-unsafe
characters in meeting identifiers, and capacity or quota limits.

## Cost, and the limits that bound it

Every run is bounded in four independent ways, and all four are enforced in
code rather than requested in the prompt. There is deliberately **no whole-run
retry**: a failed run is reported once, because restarting a diagnosis from the
original question re-pays for every tool call it already made.

When a limit is reached, the agent is first asked to write its answer from the
evidence it has — the cooperative path, and the one that produces a real
diagnosis. If it calls another tool anyway, the run is ended rather than paying
for a further model turn, and you get a diagnosis in the usual three sections
whose Root Cause is "Not determined" and which names the limit that was
reached.

> **Known limitation.** That forced ending works by raising out of the agent's
> event loop, and whether the Strands Agents SDK passes the signal through its
> own asynchronous and threaded code unchanged has not been verified against a
> deployed stack — only against a stand-in for an SDK's tool executor. If it
> did not get through, every subsequent tool call would refuse (the deadline is
> checked inside each tool), and the invocation would finally be ended by the
> Lambda `Timeout` rather than by the agent. Confirming this is the first item
> on the entry-point change that follows.

The response reports this in a `truncated` field, inside the JSON-encoded
`body`. It is `true` whenever a limit was reached at all, on the cooperative
path as well as the forced one. So read it as "this diagnosis did not get to
search as widely as it wanted" rather than "the agent was cut off
mid-sentence".

| Environment variable | Default | Maximum | Bounds |
|---|---|---|---|
| `TROUBLESHOOTING_MAX_ITERATIONS` | 8 | 20 | Tool calls per diagnosis |
| `TROUBLESHOOTING_TOKEN_BUDGET` | 250,000 | 1,000,000 | Input + output tokens per diagnosis. Checked between tool calls, so a run can overshoot it by roughly one model turn before it stops |
| `TROUBLESHOOTING_WALL_CLOCK_SECONDS` | 240 | 870 | Seconds per diagnosis — **requested**; see below |
| `TROUBLESHOOTING_ANSWER_RESERVE_SECONDS` | 30 | 120 | Time held back for the model to write its answer |
| `TROUBLESHOOTING_MAX_OUTPUT_TOKENS` | 4,096 | 16,384 | Output tokens per model turn |
| `TROUBLESHOOTING_MAX_LOG_GROUPS` | 120 | 400 | Log groups returned by discovery |
| `TROUBLESHOOTING_MAX_LOG_EVENTS` | 100 | 1,000 | Rows per log query |
| `TROUBLESHOOTING_MAX_LOG_MESSAGE_LENGTH` | 2,000 | 20,000 | Characters per log line |
| `TROUBLESHOOTING_MAX_TOOL_RESULT_CHARS` | 24,000 | 120,000 | Characters per tool result |
| `TROUBLESHOOTING_DEFAULT_LOOKBACK_HOURS` | 4 | 72 | Search window when none is asked for |
| `TROUBLESHOOTING_MAX_LOOKBACK_HOURS` | 72 | 336 | Largest search window allowed |
| `TROUBLESHOOTING_QUERY_TIMEOUT_SECONDS` | 45 | 120 | Seconds to wait for one log query |
| `TROUBLESHOOTING_QUERY_POLL_SECONDS` | 1 | 10 | Seconds between polls of a running query |

Set these on the `STRANDS-<stack>-Troubleshooting` Lambda function. They take
effect on the next invocation. A value above its maximum is clamped rather than
refused, and an unreadable value falls back to the default, so a typo costs
money or precision rather than stopping a diagnosis from starting — and every
clamp or fallback is logged, so a limit never quietly becomes a different one.

⚠️ **These are not carried in the CloudFormation template, so a value you set
by hand does not survive a stack update** — CloudFormation restores the
function's environment to what the template declares, which is none of these.
That is deliberate: they are a dozen cost knobs for an operator looking at a
specific run, not deployment configuration, and adding a dozen stack parameters
for them would be worse. Treat a change here as temporary. The one setting that
*is* meant to persist is the system prompt, which lives in DynamoDB precisely
so that it does.

**The wall clock is a request, not a guarantee of how long a run may take.**
The effective budget is the smaller of `TROUBLESHOOTING_WALL_CLOCK_SECONDS` and
the time the invocation actually has left, less
`TROUBLESHOOTING_ANSWER_RESERVE_SECONDS` for the final model turn. So raising
the wall clock above the function's `Timeout` does not extend a run — it is
simply ignored, and the agent still reports that it ran out of time rather than
being killed mid-answer. To genuinely allow longer diagnoses, raise the
function's `Timeout` as well. Each run logs the effective budget it used and
whether the invocation was what capped it.

`TROUBLESHOOTING_MAX_LOOKBACK_HOURS` is the costliest of these: CloudWatch Logs
Insights bills by bytes scanned, and the Virtual Participant's container log
group is the highest-volume stream in the solution.

The agent uses the same Bedrock model as the Meeting Assistant
(`MeetingAssistServiceBedrockModelID`), so selecting a stronger model there
gives a stronger diagnosis. Its token usage is logged at the end of every run.

## Guardrails and what it will not say

If `BedrockGuardrailId` and `BedrockGuardrailVersion` are both set, the
configured Amazon Bedrock Guardrail is applied to this agent as it is to the
Meeting Assistant. With either one unset, no guardrail is applied.

The prompt also instructs the agent, in its output: to describe a defect as the
behaviour observed in neutral engineering terms rather than characterising it in
security terms; to redact credentials, tokens and signed URLs from the evidence
it quotes; and to quote meeting content only where the content itself is the
evidence. This matters because a diagnosis is the kind of text people paste into
a public issue tracker. If what the agent finds needs reporting through a
security channel, it is told to say so and stop rather than write it up.

Those are instructions to a model, so the controls that do not depend on it
sit in the tools: `get_vp_record`'s field allowlist, the parameter redaction
described above, and the fact that the thinking-step stream publishes only a
tool result's **shape** — "2 log groups, 42 rows, 90000 bytes scanned" —
never its content. That
last one matters most, because that stream reaches the UI without the model in
the loop, and a log line can contain whatever the component that wrote it put
there.

## Related

- [Troubleshooting](troubleshooting.md) — the manual procedures, and which log
  group holds what
- [Monitoring & Alerting](monitoring.md) — the alarms and dashboard that tell
  you to start looking
- [Meeting Assistant](meeting-assistant.md) — the other Strands agent in this
  solution, and the streaming channel this one reuses
