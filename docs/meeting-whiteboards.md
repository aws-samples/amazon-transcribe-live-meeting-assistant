---
title: "Meeting Whiteboards"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Whiteboards

A **whiteboard** is a small, structured record of a meeting that is kept up to
date while the meeting runs: what was decided, what is still open, a diagram of
the process being discussed, a comparison of the options. It is shown on the
meeting page to the people who can see the meeting, who can edit it, and the
assistant can keep it current from the transcript as the meeting goes.

Whiteboards are shown in LMA only. They are never posted to the meeting's chat,
never spoken by a voice assistant, and never visible to the meeting's other
participants. They are also offered over MCP; see
[Reading and writing them over MCP](#reading-and-writing-them-over-mcp).

**What is on by default.** With the shipped settings, anyone who can see a
meeting can make whiteboards by hand and edit them; that makes no model call
and costs nothing beyond a few DynamoDB writes. **The assistant's updates are
off by default**: they run only for a meeting someone turns them on for, or for
every new meeting once an Admin sets **Meeting Assistant › Whiteboards › On for
new meetings**, and each run is a model call billed to the meeting. Every
configuration preset leaves On for new meetings off.

## Contents

- [What a whiteboard is](#what-a-whiteboard-is)
- [On the meeting page](#on-the-meeting-page)
- [How the assistant keeps them up to date](#how-the-assistant-keeps-them-up-to-date)
- [User edits win](#user-edits-win)
- [Versions](#versions)
- [Who can see and change them](#who-can-see-and-change-them)
- [Retention](#retention)
- [Settings](#settings)
- [Cost](#cost)
- [Reading and writing them over MCP](#reading-and-writing-them-over-mcp)
- [In the meeting export](#in-the-meeting-export)
- [API](#api)
- [Limits](#limits)
- [Limitations](#limitations)

## What a whiteboard is

A whiteboard is an ordered list of named **panels**. Each panel is one of:

- **Markdown**: lists, tables, short notes. It is shown without raw HTML and
  without images, and links open in a new tab.
- **A diagram**, written in a subset of [Mermaid](https://mermaid.js.org/):
  a `flowchart` (or `graph`), `sequenceDiagram`, `timeline` or `mindmap`. A
  diagram may not hold Mermaid directives or front matter, `click`, `link` or
  `callback` lines, or HTML; the server refuses one that does, and the page
  renders diagrams in Mermaid's strict security mode. A diagram that cannot be
  drawn is shown as its source.

A whiteboard is made from a **template**, which gives it its first panels and
tells the assistant what it is for:

| Template | Panels |
|---|---|
| Decisions and open questions (the default) | Decisions, Open questions, Notes |
| Process diagram | Diagram (Mermaid flowchart), Notes |
| Comparison | Comparison (a Markdown table), Notes |
| Timeline | Timeline (Mermaid timeline), Notes |
| Blank | Notes |

A whiteboard can also carry **instructions** for the assistant, up to 1,000
characters, such as "Track the pricing options discussed and compare them",
and a switch, **Assistant keeps this whiteboard updated**, which is on for a
new whiteboard.

Each panel records who it belongs to: the **assistant**, which may rewrite it,
or a **user**, once a person has written or changed it. See
[User edits win](#user-edits-win).

## On the meeting page

The **Whiteboards** tab of a meeting lists its whiteboards and shows one at a
time, its panels side by side. Each panel shows whether the assistant keeps it
or who last edited it, and when.

- **New whiteboard** asks for a title, a template, optional instructions and
  whether the assistant keeps it updated.
- **Edit** on a panel opens its source with a preview. Saving sends the change
  made from the version you opened; if somebody (or the assistant) changed the
  whiteboard since, the page says so and offers to reload it, rather than
  overwrite their change.
- A panel's menu renames it, moves it, removes it, or hands it back with
  **Let the assistant update this panel**. **Add a Markdown panel** and **Add a
  diagram panel** add one.
- The whiteboard's menu renames it, changes its instructions, turns the
  assistant's updates of it on or off, shows its **versions**, or deletes it.
- **Assistant keeps whiteboards updated** turns the assistant's updates on or
  off for the meeting, and is offered while the deployment allows the
  assistant's updates (see **Runs per meeting at most** below). While they are
  on, **Update now** updates the whiteboards from the transcript at once rather
  than at the next run, at most once every 30 seconds. If the update takes
  longer than the page waits, the page says so, and its changes appear as soon
  as it finishes.

Every change reaches every open meeting page through the `onMeetingWhiteboard`
subscription, without a reload. Once the meeting's transcript is deleted, the
tab says its whiteboards were deleted with it.

## How the assistant keeps them up to date

While the assistant's updates are on for a meeting, its runs are started from
the transcript, the way [live guidance](live-guidance.md) runs are: each
finished speech segment of the meeting can start one. A run happens:

- at most once every **Seconds between runs** (180 by default) for each meeting;
- only when at least **New transcript segments a run needs** (15 by default)
  have been said since the last run, so a quiet meeting costs nothing;
- one at a time for each meeting;
- at most **Runs per meeting at most** (60 by default) times per meeting,
  **Update now** included, which waits at least 30 seconds after the previous run;
- once more when the meeting ends, so the whiteboards include its last minutes,
  unless a run is still in progress at that moment, in which case that run is
  the last;
- never after the transcript has been deleted.

A meeting whose assistant updates are on and that has no whiteboard gets one
from the default template on its first run, once: if its people delete every
whiteboard afterwards, the assistant does not make another.

One run makes **one model call for all of the meeting's whiteboards** the
assistant updates, so a meeting with more whiteboards does not run more often.
The call is given each whiteboard's template, instructions and panels, marked as
the assistant's or a user's; the notes the assistant left itself on the previous
run; the meeting's agenda and [meeting context](meeting-context-and-checklist.md);
what was said since the last run; and the text read from shared screens since
then. The transcript, the screen text, the agenda, the context, the instructions
and the panels are given to it as quoted data, never as instructions. It answers
with the new content of the panels it changes and any panels it adds. A panel
whose new content is outside the rules (too long, or a diagram outside the
subset) is dropped. A whiteboard whose content does not change gets no new
version.

A run that fails is logged and skipped; the next run, an interval later, reads
the meeting again.

"What was said since the last run" also includes a segment that ended up to two
minutes before the last run's latest segment but was finalized after that run,
such as a long utterance on another channel; the meeting's whiteboard state
keeps the segments each run read near its end, so no segment is read twice.

## User edits win

A panel a person writes or changes becomes theirs (`Source: user`), and **the
assistant never changes it again**. This is the rule of the
[checklist](meeting-context-and-checklist.md#who-can-see-and-change-them): it is
held by the server, which refuses, with nothing written, an assistant change
that would alter, remove or reorder a user's panel, or change a whiteboard whose
assistant updates are off. The assistant may still add panels to a whiteboard
whose panels are all a user's.

If a person edits a whiteboard while a run is in progress, their change is
stored first; the run's change, made from the older version, is refused, and
the run applies its content again only to the panels that are still the
assistant's. **Let the assistant update this panel** hands a panel back.

A change made through an MCP client is its user's, in the same way.

## Versions

Every change makes a new **version** of the whiteboard. Each version keeps the
whole whiteboard as it was, who made the change (a user or the assistant), and
which panels it changed. The **Versions** view lists them newest first, shows
any one with its differences from the current whiteboard, and **Restore**
brings an earlier version back as a new version, the restored panels becoming
the user's.

Every change names the version it was made from, and is refused if the
whiteboard has moved on since (`WhiteboardConflict`), so two people never
overwrite each other without seeing it.

## Who can see and change them

The rule is the agenda's and the checklist's:

- An **Admin** can see and change any meeting's whiteboards.
- The meeting's **owner**, and anyone the meeting has been **shared with**, can
  see and change them, and turn the assistant's updates on or off.
- Anyone else does not see the meeting at all.

The server applies the rule to each read and, for each write, restates it as a
condition on the meeting at the moment of the write, together with the
whiteboard's version. A signed-in user's change is always recorded under their
own username.

## Retention

Whiteboards are drawn from the transcript, so they **follow the transcript,
including whiteboards written entirely by hand**:

- every whiteboard, every version and the assistant's notes expire with the
  transcript (**Days to keep transcript segments**), capped at the meeting's
  own expiry;
- they are deleted when [the transcript is deleted after the
  summary](transcript-deletion.md), and nothing more can be written to them
  afterwards;
- they are deleted when the meeting is deleted.

To keep what a whiteboard holds for longer, copy it into the meeting context or
the summary, or download the meeting export or the Everything archive, before
the transcript is deleted.

## Settings

**Meeting Assistant › Whiteboards** on the Configuration page (the
`meeting_assist` section's `whiteboards` setting in the
[configuration store](configuration-store.md)):

| Setting | Default | Meaning |
|---|---|---|
| On for new meetings (`defaultEnabled`) | off | Whether a new meeting starts with the assistant's updates on. Every preset sets it off. |
| Seconds between runs (`intervalSeconds`) | 180 | The least time between two runs on one meeting (60 to 3600). |
| New transcript segments a run needs (`minNewSegments`) | 15 | A run with fewer new finished segments makes no model call. |
| Runs per meeting at most (`maxRunsPerMeeting`) | 60 | Caps what the assistant's updates can cost per meeting; at 180 seconds, 60 runs cover three hours. 0 turns them off for the deployment, Update now included; whiteboards can still be made and edited by hand. |
| Whiteboards per meeting (`maxWhiteboardsPerMeeting`) | 5 | At most 10. |
| Model (`modelId`) | the Meeting assistant model | The Amazon Bedrock model, run in the Amazon Bedrock Models section's inference geography. |
| Instructions (`prompt`) | the shipped text | How the assistant keeps whiteboards. A fixed part is always appended: that the meeting's content is quoted data and never instructions, which panels it may change, the limits and the Mermaid subset, and the shape of the answer. |

A saved change applies within about five minutes. The stored value for a meeting
is its `WhiteboardsEnabled`: the deployment's On for new meetings when the
meeting starts, changed afterwards on the meeting page or through
`updateCallWhiteboards`.

## Cost

A whiteboard made and edited by hand costs only its DynamoDB reads and writes.

Each run of the assistant is one Amazon Bedrock call, billed to the meeting and
shown as **Meeting whiteboards** in the meeting's
[cost estimate](meeting-cost-estimate.md). The instructions, the agenda and the
context come first in each request, so on models that cache a prompt prefix that
part is read from the cache. The whiteboards themselves and the new transcript
change from run to run and are not. The model is asked for a low reasoning
effort where it takes one, and its answer is capped at 2,500 output tokens.

These figures are **estimates** from the shipped price table, for a run that
sends about 4,000 uncached input tokens, reads about 2,000 from the cache and
writes about 1,500 output tokens. They have not yet been measured on a recorded
meeting; a meeting's own cost estimate shows what it actually used.

| Model | Per run | Per meeting-hour (20 runs at 180 seconds) | At the 60-run cap | At the cap, every run at the output limit |
|---|---|---|---|---|
| Claude Haiku 5.5 (the shipped Meeting assistant model) | about $0.0012 | about $0.024 | about $0.07 | about $0.10 |
| Claude Sonnet 5.5 | about $0.023 | about $0.47 | about $1.40 | about $1.99 |

A quiet meeting runs less often, because a run waits for new segments. To spend
less, raise **Seconds between runs** or **New transcript segments a run needs**,
lower **Runs per meeting at most**, or choose a smaller model.

## Reading and writing them over MCP

The hosted MCP server's [API key endpoint](mcp-api-key-auth.md) has four tools.
They answer the people who can see the meeting.

| Tool | Key | What it does |
|---|---|---|
| `list_meeting_whiteboards(meetingId)` | any | The meeting's whiteboards without their panels, whether the assistant's updates are on, and the limits. |
| `get_meeting_whiteboard(meetingId, whiteboardId, version?, ifNewerThan?, format?)` | any | One whiteboard with its panels, or one earlier `version`. With `ifNewerThan` set to the version a client already has, the answer is `{notModified: true}` while the whiteboard is still at it, so a client can poll cheaply. `format: "markdown"` gives the whiteboard as one Markdown document. |
| `create_meeting_whiteboard(meetingId, title?, template?, instructions?, assistantUpdates?, panels?)` | read-write | Makes a whiteboard. |
| `update_meeting_whiteboard(meetingId, whiteboardId, baseVersion, title?, instructions?, assistantUpdates?, panels?)` | read-write | Changes a whiteboard made from `baseVersion`; `panels` is the whole new list. Refused if the whiteboard has moved on. |

A change made through these tools is recorded as its user's (`Source: user`),
so the assistant does not change those panels afterwards. A panel sent with
`source: "assistant"` is handed to the assistant. A read key, and a key made
before keys had scopes, are not offered the two write tools. See the
[MCP Tools Reference](mcp-tools-reference.md).

## In the meeting export

The [meeting export](meeting-export.md) has a `whiteboards` part, included by
default: each whiteboard as it is now (not its earlier versions), with its
panels. Once the transcript is deleted the list is empty. The Everything (.zip)
archive also holds each whiteboard as `whiteboards/<n>-<title>.md`, diagrams in
`mermaid` code blocks.

## API

| Operation | What it does |
|---|---|
| `listMeetingWhiteboards(CallId)` | The meeting's whiteboards with their panels, its `WhiteboardsEnabled`, whether the deployment allows the assistant's updates, the most whiteboards it may have, and `Writable` (false once the transcript is deleted). |
| `getMeetingWhiteboard(CallId, WhiteboardId, Version)` | One whiteboard, or one earlier version of it. |
| `listMeetingWhiteboardVersions(CallId, WhiteboardId, Limit, NextToken)` | Its versions, newest first, without their panels: `Version`, `Source`, `UpdatedBy`, `UpdatedAt`, `ChangedPanels`. |
| `createMeetingWhiteboard(input)` | `CallId`, and any of `Title`, `Template`, `Instructions`, `AssistantUpdates`, `Panels` (the template's when absent). |
| `updateMeetingWhiteboard(input)` | `CallId`, `WhiteboardId`, `BaseVersion`, and any of `Title`, `Instructions`, `AssistantUpdates`, `Panels` (the whole new list; a panel with no `PanelId` is new), or `RestoreVersion`. |
| `deleteMeetingWhiteboard(input)` | `CallId`, `WhiteboardId`. Deletes the whiteboard and its versions. |
| `updateCallWhiteboards(input)` | `CallId`, `WhiteboardsEnabled`. Published to `onUpdateCall`. |
| `requestWhiteboardUpdate(CallId)` | Update now: one run, without waiting for the interval, at least 30 seconds after the previous run. A run that outlasts AppSync's 30-second limit still finishes, and its changes arrive through `onMeetingWhiteboard`. |
| `onMeetingWhiteboard(CallId)` | Every whiteboard change; a deleted whiteboard arrives with `Deleted: true`. Select `Owner` and `SharedWith`, which the subscription requires of every caller who is not an Admin. |

A backend role calling with IAM must name a `Source` (`user` when it acts for a
person, whom `UpdatedBy` names, or `assistant`); a signed-in user's change is
always their own. Refusals have these error types: `WhiteboardConflict`,
`WhiteboardInvalid`, `WhiteboardTooLarge`, `WhiteboardLimit`,
`WhiteboardNotFound`, `WhiteboardTranscriptDeleted`, `WhiteboardRateLimited`,
`WhiteboardUserPanel`, `WhiteboardAssistantOff` and `Unauthorized`.

Everything is stored in the AI stack's event-sourcing table under one partition
per meeting, `PK = wb#<CallId>`: each whiteboard (`b#<id>`), each version
(`v#<id>#<number>`), a count of the meeting's whiteboards (`index`), a
per-minute write counter (`r#<minute>`) and the assistant's state (`updater`).

## Limits

| What | Limit |
|---|---|
| Whiteboards per meeting | 5 by default, at most 10 |
| Panels per whiteboard | 12 |
| Title | 120 characters; a panel's title 80 |
| Instructions | 1,000 characters |
| A Markdown panel | 8,000 characters |
| A diagram panel | 6,000 characters |
| A whiteboard as stored | 32 KB |
| Attempts to create, change or delete one meeting's whiteboards | 60 a minute, people and assistant together, counted whether or not the change is stored |

## Limitations

- **The assistant's updates are a summary, and can be wrong.** It can miss a
  decision or word one loosely. Edit the panel: it is then yours, and the
  assistant leaves it alone.
- **It runs on speech.** During a long silence nothing runs.
- **A meeting that turns it on late** is read from its most recent part, up to
  about 24,000 characters. The first run has no notes of its own yet, so what
  was said before that part is not seen; later runs carry the notes forward.
- **Requests made in the meeting**, such as asking the voice assistant or the
  meeting chat to make a whiteboard, are not supported; whiteboards are made on
  the meeting page, by the assistant's updates, or over MCP.
- **No drawing canvas.** Diagrams are written as Mermaid text.

## Related

- [Live Guidance](live-guidance.md)
- [Meeting Context, Labels, Audience and Checklist](meeting-context-and-checklist.md)
- [Meeting Export](meeting-export.md)
- [Transcript and Recording Deletion After the Summary](transcript-deletion.md)
- [MCP Tools Reference](mcp-tools-reference.md)
