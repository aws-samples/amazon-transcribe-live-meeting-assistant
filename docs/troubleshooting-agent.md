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
| `messageId` | When present together with `callId`, each tool call is streamed to the UI's thinking-step panel over the existing `onAddChatToken` subscription. |

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
| `get_vp_record` | `dynamodb:GetItem` on the VirtualParticipant table | One Virtual Participant's current status, its structured exit code, and — when it was ended from outside — when and why (`endedAt`, `endReason`). |

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

The record also comes with an `endAssessment`: a reading of the row the way the
code that writes it does. A Virtual Participant ends in one of four ways, and
each leaves a different shape. Ended through the end operation (by a user, the
SDK or CLI), it has `endedAt` and `endReason` on its row, and its
log has `Received SIGTERM` (and, on a MicroVM,
`[terminate] signalling app to exit`) rather than a `VP exit:` line; the
assessment presents that recorded reason as the answer to why it ended. Marked
by the VP task reaper after its ECS task stopped (`FARGATE` and `EC2` launch
types), it is `FAILED` with `endedAt` but no `endReason`, and `errorMessage`
carries the ECS stop code and reason; the assessment points at that instead.
Leaving through its own exit path, it is `COMPLETED` or `FAILED` with an
`endTrigger`, and its log has a `VP exit:` line. Stopped by a signal that did
not come through the end operation, it is `COMPLETED` with none of these. A row
whose status is not terminal but which has `endedAt` set describes a
participant that has ended — a progress update it wrote while shutting down
landed after the end — and the assessment says the status is stale. A scheduled
participant cancelled before it launched never ran, and the assessment says
there is no participant log to search. Every other assessment ends with the one
log search that corroborates it: the participant's
id together with the markers for every way it could have ended, in one call,
with a `lookback_hours` reaching back to the row's `createdAt`, because the
default search window is only a few hours.

A search that returns nothing says which window it covered, that its terms are
ORed so alternatives belong in the same call, and — when it was looking for
`VP exit:` — which lines a participant stopped from outside writes instead.
These hints are in the tool results rather than only in the prompt, so a
deployment that has replaced the prompt still gets them.

Two design points worth knowing, because they are what make the answers
trustworthy:

**Log group names are discovered, never constructed.** An LMA deployment's log
groups sit in four different namespaces: most are named after the *generated*
nested-stack name, a few after the parent stack, the MicroVM groups live under
`/aws/lambda-microvms/`, and AppSync's group is named after the API id. The
agent refuses to query a name that `discover_log_groups` did not return, because
a guessed name produces an empty result that reads exactly like an absence of
evidence. On a stack upgraded from a release before v0.3.11 it also finds the
MicroVM log groups that release used, `/aws/lambda-microvms/<stack>-vp` and
`-asr`, which the upgrade leaves in place: a Virtual Participant that ran on the
pre-upgrade image — including one launched before the image rebuild finished —
logged only there. Each MicroVM group is labelled `current` or `legacy`, and
when a legacy group exists the agent is told to search it before concluding
there is no evidence (see the [v0.3.11 migration
notes](stack-updates-and-upgrades.md#v0311)).

**`VPLaunchType` is a first-class input.** Under the default `MICROVM`, the
Virtual Participant's Fargate log group exists and is permanently empty and no
ECS task-stopped event is ever emitted. An agent that did not know this would
report "no evidence the Virtual Participant ran" on a perfectly healthy
deployment. It reads the parameter first and routes accordingly.

It also reports, for every log group, whether CloudWatch currently stores any
bytes for it. That is a hint about where to look first rather than proof that a
path is unused: a group reports no stored bytes when everything in it has aged
past the retention period too, so a participant idle for a fortnight and
failing today would report none on the very group holding the failure. The
agent is told as much, and told that "does this deployment use that path" is a
question for the stack parameters rather than for a byte count.

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
diagnosis. Every tool call the model made in that same turn gets the same
request, because a model can ask for several tools at once and none of them
can have ignored an instruction it had not yet seen. If the model calls a tool
in a later turn anyway, the run is ended rather than paying for a further model
turn, and you get a diagnosis in the usual three sections whose Root Cause is
"Not determined" and which names the limit that was reached.

The run is ended through the Strands Agents SDK's own stop request, which takes
effect after every tool call in the turn has been given a result, so the
conversation the model would see stays complete. This behaviour is tested
against the real Strands event loop with a scripted model in place of Bedrock
(`lma-meetingassist-setup-stack/test/test_troubleshooting_real_strands.py`).

The response's JSON-encoded `body` reports both outcomes. `truncated` is `true`
whenever a limit was reached at all, on the cooperative path as well as the
forced one, so read it as "this diagnosis did not get to search as widely as it
wanted" rather than "the agent was cut off mid-sentence". `usage.endedBecause`
is set only on the forced path, and names the limit that ended the run.

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
described above, and the thinking-step stream's summaries.

That stream matters most, because it reaches the UI without the model in the
loop, so nothing the prompt says about what to quote applies to it. The rule
it follows is: publish a shape, a count, or text written by this codebase —
never text the model wrote, and never an AWS error message.

In practice a progress line reads "2 log groups, 42 rows, 90000 bytes scanned"
or "3 log groups in virtual-participant, transcript-pipeline". A
`query_string`, `search_terms` or `components` the agent wrote appears only as
a length, because it writes those from text it has just read out of a log line.
Log group names are counted and reported by which component they belong to
rather than quoted, because a name on this stream has not been checked against
discovery yet. A failed call publishes the exception type, or a short label
written at the point the error is raised — not the message, which may carry an
account-specific ARN. The agent itself still receives all of it; this is about
what leaves the function.

The same applies to an error response: a 400 or a 500 carries a fixed message
and an `errorType`, and the full text goes to the function's log. That matters
more once the UI entry points forward this body to a browser.

## Related

- [Troubleshooting](troubleshooting.md) — the manual procedures, and which log
  group holds what
- [Monitoring & Alerting](monitoring.md) — the alarms and dashboard that tell
  you to start looking
- [Meeting Assistant](meeting-assistant.md) — the other Strands agent in this
  solution, and the streaming channel this one reuses
