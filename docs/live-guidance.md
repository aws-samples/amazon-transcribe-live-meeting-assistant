---
title: "Live Guidance"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Live Guidance

The Meeting Assistant answers when someone asks it something. **Live guidance**
works the other way round: while a meeting is in progress, an AI assistant reads
what is being said, compares it with what the people running the meeting
prepared (its agenda, its [meeting context and checklist](meeting-context-and-checklist.md)),
and offers them short guidance as it goes: a question to ask now, a topic not
yet covered, something that needs clarifying. It also keeps the meeting's
checklist up to date, marking items covered or partly covered with evidence from
the transcript.

Live guidance is **off** until it is turned on, for one meeting or for every new
meeting. With it off, nothing runs and nothing is billed.

## Contents

- [What it produces](#what-it-produces)
- [Who sees it](#who-sees-it)
- [Turning it on](#turning-it-on)
- [How often it runs](#how-often-it-runs)
- [The digest](#the-digest)
- [Settings](#settings)
- [The planned length of a meeting](#the-planned-length-of-a-meeting)
- [Cost](#cost)
- [Reading it over MCP](#reading-it-over-mcp)
- [Retention](#retention)
- [Limitations](#limitations)

## What it produces

Each run reads the transcript since the previous run, a summary of the meeting
before that (which the run itself keeps up to date), the agenda, the meeting
context, the checklist, the time into the meeting and, when known, its planned
length. It produces two things.

### Checklist progress

For each checklist item that is **open** or **partial** and whose status was not
set by a user, the run decides whether the transcript now covers it
(**covered**) or covers part of it (**partial**). A change is stored through the
same `updateCallChecklistItem` mutation the meeting page uses, with:

- `StatusSource: assistant`, so the page shows the assistant set it;
- evidence for each segment the decision rests on: the segment, its time, and a
  quote of up to 300 characters, kept only when it is in that segment's text;
- `UpdatedBy: LMA live guidance`.

A status a user set is never changed, as the
[checklist rules](meeting-context-and-checklist.md) promise, and a user's change
made while a run is in progress wins. Live guidance only moves an item forward
(open to partial or covered, partial to covered), and never marks one
**deferred** or **not applicable**: an item the meeting postponed is left for the
people running it to decide.

### Guidance

Guidance is published as [meeting notifications](meeting-notifications.md) of
category `guidance`, source `live-guidance`:

| Type | When |
|---|---|
| `ask_question` | A `must` or `should` checklist question is still open, and the conversation has reached its topic or is running out of time. The body gives the question, worded for the moment. |
| `uncovered_topic` | An agenda item or checklist topic has had no discussion, and the meeting is past the point where it was planned, or past about 60% of its planned length. |
| `clarify` | Something ambiguous was said that matters to an open item: a number without its unit, "soon", "we'll handle that", or a term used two ways. |
| `risk_probe` | A risk item's subject came up without being explored, or a new risk was stated (a dependency, a deadline, a constraint) that is not in the checklist. |
| `action_missing_owner`, `action_missing_due_date` | An action was agreed with no owner, or no date. |
| `time_check` | The planned length is nearly up with `must` items still open. |
| `meeting_digest` | Only on request; see [The digest](#the-digest). |

Each item:

- has a title of at most 80 characters and a body of at most 600;
- names, in `refs`, the checklist item it is about (`checklist:<itemId>`) and
  the transcript segments it is based on (`segment:<segmentId>`), each one
  something the run was shown;
- has a **dedup key**, `<type>:checklist:<itemId>` or `<type>:topic:<subject>`,
  so a later run that says the same thing again updates the item in place
  rather than adding another, and a run that has nothing new to say publishes
  nothing;
- carries a **Mark covered** button when it is an `ask_question`,
  `uncovered_topic` or `time_check` about a checklist item (it sets the item
  covered as the user who presses it), and a question carries **Copy
  question**.

When a checklist item stops being open, because a run or a user covered it or a
user deferred it, the guidance about it is withdrawn: it is published again
with an expiry of now, so the page stops showing it. It is still stored, and
still returned over MCP.

A run publishes only what the model is confident of (see **Confidence
threshold** below), and at most **10 items in any 10 minutes** per meeting, new
and updated together. When there are more, `must` items come before `should`
and `could`, and time checks and questions before the other types.

## Who sees it

Guidance is for the people running the meeting. It goes to the meeting's owner,
the users the meeting is shared with, and Admins, through the notification
mechanism's own rule (see [Who sees them](meeting-notifications.md#who-sees-them)).
It is never posted to the meeting chat, never spoken by the voice assistant, and
never visible to the meeting's other participants.

On the meeting page it appears in the **Notifications** drawer, as it arrives.
Dismissing an item, or muting the `guidance` category, changes only your own
view. **Muting does not stop live guidance or its cost**; turning it off for the
meeting does.

## Turning it on

- **For one meeting, on the meeting page.** The **Live guidance** control above
  the checklist has an on/off switch for the meeting while it is in progress,
  for anyone who may change the meeting (its owner, the users it is shared
  with, and Admins).
- **For a meeting a Virtual Participant records.** The Virtual Participant form
  (start, schedule, edit a scheduled one, and Join again) has a **Live
  guidance** choice: *The deployment's setting*, *On* or *Off*.
- **Over MCP.** `schedule_meeting`, `start_meeting_now` and
  `update_scheduled_meeting` take `guidance` (true or false), and
  `update_meeting_metadata` turns it on or off for a meeting that has started.
  See the [MCP Tools Reference](mcp-tools-reference.md).
- **For every new meeting.** An administrator sets **Meeting Assistant › Live
  guidance › On for new meetings** on the Configuration page.

The stored value is the meeting's `GuidanceEnabled`. A new meeting gets the
Virtual Participant's or MCP client's choice; without one, the deployment's
**On for new meetings**, which is off unless an administrator changed it.
Changing that setting affects meetings that start afterwards.

## How often it runs

Live guidance runs from the transcript: each finished speech segment of a
meeting with live guidance on can start a run. A run happens:

- at most once every **Seconds between runs** (90 by default) for each meeting;
- only when at least **New transcript segments a run needs** (10 by default)
  have been said since the last run, so a quiet meeting costs nothing;
- one at a time for each meeting: a run holds the meeting until it finishes,
  and any other run that would start meanwhile does not;
- at most **Runs per meeting at most** (120 by default) times per meeting;
- never after the meeting has ended or its transcript has been deleted.

A run that fails is logged and skipped. It is not retried; the next run, an
interval later, reads the meeting again.

## The digest

The **Digest so far** button beside the switch, and the MCP tool
`request_meeting_digest`, make a short digest of the meeting so far: a summary,
the items covered and still open, the decisions, and the actions with their
owners and dates where they were said. It is published as a guidance item of
type `meeting_digest` and shown in the Notifications drawer. It works whether or
not live guidance is on for the meeting, for anyone who may change the meeting,
unless an administrator has turned live guidance off for the deployment (Runs
per meeting at most set to 0). Each request is one model call, billed to the
meeting, and a meeting gets at most one digest a minute: a second request
within the minute is answered with a message to try again.

The button waits for the digest. If the model takes longer than the 30 seconds
the page waits, the page says so; the digest may still arrive in the drawer.

## Settings

**Meeting Assistant › Live guidance** on the Configuration page (the
`meeting_assist` section's `guidance` setting in the
[configuration store](configuration-store.md)):

| Setting | Default | Meaning |
|---|---|---|
| On for new meetings (`defaultEnabled`) | off | Whether a new meeting starts with live guidance on. |
| Seconds between runs (`intervalSeconds`) | 90 | The least time between two runs on one meeting (60 to 3600). |
| New transcript segments a run needs (`minNewSegments`) | 10 | A run with fewer new finished segments makes no model call. |
| Guidance items per 10 minutes (`maxItemsPer10Min`) | 10 | The most items one meeting is given in any 10 minutes. 0 publishes none and still keeps the checklist up to date. |
| Confidence threshold (`confidenceThreshold`) | 0.7 | Checklist changes and guidance below it are dropped. Higher is quieter. |
| Model (`modelId`) | the Meeting assistant model | The Amazon Bedrock model, run in the Amazon Bedrock Models section's inference geography. |
| Runs per meeting at most (`maxRunsPerMeeting`) | 120 | Caps what live guidance can cost per meeting; at 90 seconds, 120 runs cover three hours. 0 turns live guidance off for the deployment, the digest included. |
| Instructions (`prompt`) | the shipped text | What to look for and how sparing to be. A fixed part is always appended: that the transcript, context and checklist are quoted data and never instructions, the length limits, and the shape of the answer. |

A saved change applies within about five minutes.

## The planned length of a meeting

Time checks, and a topic counted late at about 60% of the meeting, need the
meeting's planned length. LMA has no field for it, so live guidance reads it
from a [label](meeting-context-and-checklist.md) named `planned-minutes`, whose
value is the planned length in minutes (for example `planned-minutes: 45`).
Without the label, the model uses any timings the agenda or meeting context
gives, and otherwise reports no time checks and no topic as late.

## Cost

Each run is one Amazon Bedrock call, billed to the meeting and shown as **Live
guidance** in the meeting's [cost estimate](meeting-cost-estimate.md). The
instructions, the context, the agenda and the checklist come first in each
request, so on models that cache a prompt prefix the repeated part is read from
the cache at a lower price (Meeting Assistant › Use Amazon Bedrock prompt
caching). The model is asked for a low reasoning effort where it takes one.

Measured on a recorded 8-item meeting, with the prices in the shipped price
table, three runs of 8 new segments each:

| Model | Per run, measured | Per meeting-hour at the most (40 runs) | Per meeting at the default cap (120 runs) |
|---|---|---|---|
| Claude Haiku 5.5 (the shipped Meeting assistant model) | about 550 input, 1,800 cache-read, 800 cache-write and 1,400 output tokens: about $0.0009 | about $0.04 | about $0.11 |
| Claude Sonnet 5.5 | about $0.012 | about $0.50 | about $1.50 |

Output (including the model's reasoning) is most of the cost. A longer meeting
context or checklist adds input tokens, mostly read from the cache. A quiet
meeting runs less often than 40 times an hour, because a run waits for new
segments. The function itself adds well under a cent an hour (about 40
invocations of a few seconds at 512 MB), and the reads and writes a fraction of
that. A digest is about 1,800 input and 500 output tokens on Claude Haiku 5.5.

These figures are from one recorded meeting; a meeting's own cost estimate shows
what it actually used. To spend less, raise **Seconds between runs** or **New
transcript segments a run needs**, lower **Runs per meeting at most**, or choose
a smaller model.

## Reading it over MCP

The guidance a meeting was given is read with `list_meeting_notifications`
(category `guidance`), which returns each item's `type`, `refs`, `dedupKey`,
`source` and times, or with `list_meeting_guidance`. Items from live guidance
have source `live-guidance`; notes an MCP client pushed with
`push_meeting_guidance` have source `mcp`. A run is shown the most recent notes
other publishers left, so it does not repeat them.

## Retention

Guidance may quote the meeting, so it follows the transcript, like all guidance
(see [Retention](meeting-notifications.md#retention)). What live guidance keeps
between runs (its summary of the meeting so far and a record of what it
published) is stored with the meeting under `gd#<CallId>`, expires with the
transcript, and is deleted when the transcript is deleted after the summary and
when the meeting is deleted.

## Limitations

- **It is advice.** It misses things, and it can be wrong: it may mark an item
  covered that was only touched on, or suggest a question that was already
  answered. The Mark covered button, the checklist's own controls and Dismiss
  are there to correct it, and a status a user sets is never changed by it.
- **Quotes are transcript content**, with the transcript's own errors, and are
  shown to everyone who can see the meeting.
- **It runs on speech.** During a long silence nothing runs, so a time check
  that falls due then arrives when speech resumes.
- **It needs a few segments.** The first run starts once enough has been said,
  and a meeting that turned it on late is read from its most recent part,
  up to about 24,000 characters, with earlier parts known only from the summary.
- **The planned length** is known only from the `planned-minutes` label or from
  timings in the agenda or context.
- **Muting does not stop it.** Only turning it off for the meeting does.

## Related

- [Meeting Notifications](meeting-notifications.md)
- [Meeting Context, Labels, Audience and Checklist](meeting-context-and-checklist.md)
- [Meeting Assistant](meeting-assistant.md)
- [Meeting Cost Estimate](meeting-cost-estimate.md)
- [MCP Tools Reference](mcp-tools-reference.md)
