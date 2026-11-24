---
title: "MCP Tools Reference"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# MCP Tools Reference

LMA's hosted MCP server lets an AI assistant outside LMA do the whole job of a
meeting for its user: before it, schedule a Virtual Participant with an agenda,
a [meeting context, a checklist and labels](meeting-context-and-checklist.md),
share the meeting with colleagues, and change any of these until it starts;
during it, read the transcript as it grows, update the agenda or the checklist,
and end the Virtual Participant; after it, find its meetings by label and read
the consent record, the transcript and the summary.

This page lists every tool the server offers and, for the tools that prepare,
follow and close a meeting, gives one example request and answer each. How a
client connects, and the API key scopes, are in
[MCP API Key Authentication](mcp-api-key-auth.md).

## Conventions

- **Names.** Tool names are snake_case; argument and answer fields are
  camelCase. The guidance and notification tools take `meeting_id`.
- **`meetingId`** is the LMA meeting (its CallId) wherever a tool takes or
  returns it. The one exception is the `meetingId` *argument* of
  `schedule_meeting` and `start_meeting_now`, which is the meeting platform's ID
  or join link; their answers return that as `platformMeetingId`, and return
  `meetingId` as the LMA meeting, which is `null` until the Virtual Participant
  has started it. `get_virtual_participant_status` returns `meetingId` once it
  has. `callId` is also returned, with the same value, as `start_meeting_now`
  and `get_virtual_participant_status` already did.
- **Answers** are JSON text content. An error is a tool result with
  `isError: true` and a message saying what was wrong; a meeting that does not
  exist and one the caller may not see are refused with the same message.
- **Access.** Every tool answers the callers the meeting page answers: an
  Admin, the meeting's owner, or a user the meeting is shared with, and lets the
  same people make each change, except where the table says otherwise.
- **Front doors.** The API key endpoint offers every tool on this page. The
  OAuth (AgentCore Gateway) endpoint offers the seven original tools for now
  (`search_lma_meetings`, `list_meetings`, `get_meeting_summary`,
  `get_meeting_transcript`, `get_virtual_participant_status`, `schedule_meeting`
  and `start_meeting_now`), without the meeting preparation arguments.
- **Legacy keys** keep `schedule_meeting` and `start_meeting_now` with their
  original arguments: their `labels`, `audience`, `context`, `checklist`,
  `shareWith`, `summaryProfile` and `guidance` arguments need a **Read and
  write** key. The answers are the
  current ones on every endpoint and for every key.
- **Annotations.** Every tool declares `readOnlyHint`, `destructiveHint` and
  `idempotentHint`. A key with the **Read only** scope is offered, and answered
  for, the read-only tools only. `destructiveHint` is true for the tools whose
  change cannot be taken back or takes something away from people. That
  includes the tools that replace one of a meeting's preparation settings as a
  whole and clear it when sent an empty one (`update_meeting_agenda`,
  `set_meeting_context`, `set_meeting_checklist`, `update_meeting_metadata` and
  `update_scheduled_meeting`); a tool that changes one part, such as
  `update_checklist_item`, is not destructive.

## Every tool

| Tool | Changes something | Who, beyond the general rule |
|---|---|---|
| `search_lma_meetings` | no | |
| `list_meetings` | no | |
| `get_meeting_summary` | no | |
| `get_meeting_transcript` | no | |
| `get_meeting_record` | no | its `consent` section (with the evidence images): owner and Admin |
| `get_meeting_export` | no | |
| `get_meeting_cost` | no | |
| `get_meeting_agenda` | no | |
| `get_meeting_context` | no | |
| `get_meeting_checklist` | no | |
| `get_meeting_consent` | no | |
| `get_virtual_participant_status` | no | |
| `list_virtual_participants` | no | |
| `list_meeting_guidance` | no | |
| `list_meeting_notifications` | no | |
| `list_summary_profiles` | no | |
| `list_meeting_whiteboards` | no | |
| `get_meeting_whiteboard` | no | |
| `schedule_meeting` | yes | the caller schedules for themselves |
| `start_meeting_now` | yes | the caller starts it for themselves |
| `update_scheduled_meeting` | yes | the Virtual Participant's owner and Admin |
| `update_meeting_agenda` | yes | |
| `set_meeting_context` | yes | |
| `set_meeting_checklist` | yes | |
| `update_checklist_item` | yes | |
| `update_meeting_metadata` | yes | |
| `share_meeting` | yes | owner and Admin |
| `push_meeting_guidance` | yes | |
| `request_meeting_digest` | yes | |
| `create_meeting_whiteboard` | yes | |
| `update_meeting_whiteboard` | yes | |
| `ask_meeting_assistant` | yes | |
| `regenerate_meeting_summary` | yes | |
| `end_meeting` | yes | the Virtual Participant's owner and Admin |
| `end_virtual_participant` | yes | the Virtual Participant's owner and Admin |
| `delete_meeting` | yes | owner and Admin |

That is 36 tools. The examples below show the `arguments` of a `tools/call`
request and the JSON of the answer, shortened where marked.

## Before the meeting

### schedule_meeting

Schedules a Virtual Participant. Beside the meeting's name, platform, ID and
time, and the optional `agenda`, it takes the meeting's preparation, each
optional and checked with the limits in
[Meeting Context, Labels, Audience and Checklist](meeting-context-and-checklist.md#limits):
`labels` (an object of key to value), `audience` (`internal` or `external`),
`context` (Markdown), `checklist` (items of `itemId`, `text`, `kind`, and
optionally `priority` and `source`; each starts `open`), `summaryProfile` (the
[summary profile](transcript-summarization.md#summary-profiles) the meeting is
summarized with, from `list_summary_profiles`), `guidance` (true or false:
[live guidance](live-guidance.md) for the meeting; omitted, the deployment's
setting decides) and `shareWith` (email addresses). The Virtual Participant stores them and puts them on the meeting
when it creates it; the meeting is then shared with the `shareWith` addresses.
The Virtual Participant itself is not shared. Calling it again for a meeting
already scheduled updates that Virtual Participant
([Scheduling a Meeting Again](mcp-api-key-auth.md#scheduling-a-meeting-again)).

```json
{
  "meetingName": "Website relaunch review",
  "meetingPlatform": "Zoom",
  "meetingId": "https://example.zoom.us/j/81234567890?pwd=abc",
  "scheduledTime": "2026-12-01T15:00:00Z",
  "agenda": "Status\nBudget\nNext steps",
  "labels": {"project": "website-relaunch"},
  "audience": "external",
  "context": "Goal: agree the launch budget.",
  "checklist": [{"itemId": "q1", "text": "Who owns the budget?", "kind": "question", "priority": "must"}],
  "shareWith": ["colleague@example.com"]
}
```

```json
{
  "virtualParticipantId": "5f0c2c1e-…",
  "action": "created",
  "meetingName": "Website relaunch review",
  "meetingPlatform": "ZOOM",
  "meetingId": null,
  "platformMeetingId": "https://example.zoom.us/j/81234567890?pwd=abc",
  "callId": null,
  "scheduledFor": "2026-12-01T15:00:00Z",
  "status": "SCHEDULED",
  "virtualParticipantUrl": "https://…/#/virtual-participant/5f0c2c1e-…"
}
```

### start_meeting_now

Takes the same preparation arguments as `schedule_meeting` and starts the
Virtual Participant at once. Poll `get_virtual_participant_status` until it has
joined; its answer then carries the LMA `meetingId` the meeting tools take.

```json
{"meetingName": "Standup", "meetingPlatform": "Teams", "meetingId": "https://teams.microsoft.com/l/meetup-join/…", "labels": {"team": "web"}}
```

```json
{"virtualParticipantId": "9a41…", "meetingId": null, "platformMeetingId": "https://teams.microsoft.com/l/meetup-join/…", "callId": null, "status": "INITIALIZING", "message": "Virtual participant is initializing …"}
```

### update_scheduled_meeting

Changes a Virtual Participant that waits for its scheduled launch: any of
`scheduledTime`, `meetingName`, `agenda`, `labels`, `audience`, `context`,
`checklist`, `summaryProfile`, `guidance` and `shareWith`. Each one given replaces that
setting, and an empty value removes it. It is refused within 5 minutes of the start ("starts in less
than 5 minutes, so it can no longer be changed"), and a new time must be at least
5 minutes away. Once the Virtual Participant has started, change the meeting
with the meeting tools instead.

```json
{"virtualParticipantId": "5f0c2c1e-…", "scheduledTime": "2026-12-01T16:00:00Z", "labels": {"project": "website-relaunch", "phase": "2"}, "shareWith": []}
```

```json
{
  "virtualParticipantId": "5f0c2c1e-…",
  "meetingName": "Website relaunch review",
  "status": "SCHEDULED",
  "scheduledFor": "2026-12-01T16:00:00.000Z",
  "meetingId": null,
  "platformMeetingId": "https://example.zoom.us/j/81234567890?pwd=abc",
  "agenda": "Status\nBudget\nNext steps",
  "labels": {"project": "website-relaunch", "phase": "2"},
  "audience": "external",
  "context": "Goal: agree the launch budget.",
  "checklist": [{"itemId": "q1", "text": "Who owns the budget?", "kind": "question", "priority": "must", "source": null}],
  "shareWith": []
}
```

### share_meeting

Adds people to, or with `action: "remove"` removes them from, those the meeting
is shared with, through the meeting page's sharing code. Only the meeting's
owner or an Admin may change it. Returns the meeting's `sharedWith`.

```json
{"meetingId": "e3b9…", "users": ["colleague@example.com"], "action": "add"}
```

```json
{"meetingId": "e3b9…", "sharedWith": ["colleague@example.com"]}
```

## During the meeting

### get_meeting_transcript

`format: "json"` reads the transcript page by page, also while the meeting
runs. Call it without `nextToken`, then with the `nextToken` of the previous
answer; `maxSegments` is 500 by default and at most 2000. While the meeting is
in progress `nextToken` is always returned, so poll from it every
`pollAfterSeconds` (5); once the meeting has ended and the last segment has
been read it is `null`. Each segment has a stable `segmentId`, `startTime` and
`endTime` (seconds on the meeting's timeline), `speaker`, `channel`, `text` and
`type` (`speech`, `chat` for a meeting chat line, or `assistant` with
`includeAssistant: true`). `format: "text"` and `"srt"` return the whole
transcript file, as before.

The two speech channels are finalized separately, so each call looks back 60
seconds behind the last segment it returned and leaves out those already
returned; a segment finalized more than 60 seconds after later ones is not
returned to a client that has already read past it.

```json
{"meetingId": "e3b9…", "format": "json", "nextToken": "eyJ2Ijox…"}
```

```json
{
  "meetingId": "e3b9…",
  "format": "json",
  "meetingStatus": "STARTED",
  "segments": [
    {"segmentId": "a1f…", "type": "speech", "channel": "REMOTE", "speaker": "Dana", "text": "The budget is mine.", "startTime": 64.2, "endTime": 66.0},
    {"segmentId": "c7d…", "type": "chat", "channel": "MEETING_CHAT", "speaker": "Bo", "text": "+1", "startTime": 66.5, "endTime": 66.5}
  ],
  "nextToken": "eyJ2Ijox…",
  "hasMore": false,
  "pollAfterSeconds": 5
}
```

### get_meeting_agenda / update_meeting_agenda

Read or replace the meeting's agenda, while it runs or after it has ended. Both
answer `{agenda, agendaSource, updatedBy, updatedAt}`; a change made here has
`agendaSource` `mcp`. An empty string clears the agenda.

```json
{"meetingId": "e3b9…", "agenda": "Status\nBudget\nRisks\nNext steps"}
```

```json
{"meetingId": "e3b9…", "agenda": "Status\nBudget\nRisks\nNext steps", "agendaSource": "mcp", "updatedBy": "user@example.com", "updatedAt": "2026-12-01T15:20:11.120Z"}
```

### get_meeting_context / set_meeting_context

Read or replace the meeting context. Both answer `{context, updatedBy,
updatedAt}`.

```json
{"meetingId": "e3b9…", "context": "Goal: agree the launch budget.\n\nDana owns marketing spend."}
```

```json
{"meetingId": "e3b9…", "context": "Goal: agree the launch budget.\n\nDana owns marketing spend.", "updatedBy": "user@example.com", "updatedAt": "2026-12-01T15:21:40.002Z"}
```

### get_meeting_checklist / set_meeting_checklist

`get_meeting_checklist` answers `{items, updatedAt}`, each item with `itemId`,
`text`, `kind`, `priority`, `source`, `status`, `statusSource`, `evidence`
(`segmentId`, `startTime`, `quote`), `note`, `updatedBy` and `updatedAt`.
`set_meeting_checklist` replaces the whole list by LMA's rule: an item whose
`itemId` was already there keeps its status, evidence and note, unless
`resetStatuses` is true, and an item left out is removed.

```json
{"meetingId": "e3b9…", "items": [{"itemId": "q1", "text": "Who owns the budget?", "kind": "question", "priority": "must"}, {"itemId": "r1", "text": "Launch date risk", "kind": "risk"}]}
```

```json
{
  "meetingId": "e3b9…",
  "items": [
    {"itemId": "q1", "text": "Who owns the budget?", "kind": "question", "priority": "must", "source": null, "status": "covered", "statusSource": "assistant", "evidence": [{"segmentId": "a1f…", "startTime": 64.2, "quote": "The budget is mine."}], "note": null, "updatedBy": "LMA", "updatedAt": "2026-12-01T15:18:02Z"},
    {"itemId": "r1", "text": "Launch date risk", "kind": "risk", "priority": "should", "source": null, "status": "open", "statusSource": null, "evidence": [], "note": null, "updatedBy": "user@example.com", "updatedAt": "2026-12-01T15:22:00Z"}
  ],
  "updatedAt": "2026-12-01T15:22:00Z"
}
```

### update_checklist_item

Sets one item's `status` (`open`, `covered`, `partial`, `deferred` or
`not_applicable`) and/or `note` (an empty string clears it). A status set here
is recorded as `statusSource: "user"`, because the client acts for its user, so
the meeting assistant does not change it afterwards.

```json
{"meetingId": "e3b9…", "itemId": "r1", "status": "deferred", "note": "Next week"}
```

```json
{"meetingId": "e3b9…", "itemId": "r1", "text": "Launch date risk", "kind": "risk", "priority": "should", "source": null, "status": "deferred", "statusSource": "user", "evidence": [], "note": "Next week", "updatedBy": "user@example.com", "updatedAt": "2026-12-01T15:30:00Z"}
```

### update_meeting_metadata

Replaces the meeting's labels (`{}` clears them), sets its audience (`""`
clears it), sets its `summaryProfile` (`default` or `""` for the default),
turns its [live guidance](live-guidance.md) on or off (`guidance`, true or
false), or any of them. Answers `{labels, audience, summaryProfile, guidance}`.
A summary profile the configuration does not hold is refused, with the names it
does.

```json
{"meetingId": "e3b9…", "labels": {"project": "website-relaunch", "phase": "2"}, "audience": "external", "summaryProfile": "discovery-session"}
```

```json
{"meetingId": "e3b9…", "labels": {"project": "website-relaunch", "phase": "2"}, "audience": "external", "summaryProfile": "discovery-session", "updatedAt": "2026-12-01T15:31:00Z"}
```

### request_meeting_digest

Makes a short digest of a meeting so far (a summary, what is covered and still
open, the decisions and the actions) and publishes it to the meeting's
notifications as guidance of type `meeting_digest`, for the people who can see
the meeting. Answers with its text. One model call, billed to the meeting; see
[Live Guidance](live-guidance.md#the-digest).

```json
{"meeting_id": "e3b9…"}
```

```json
{"meetingId": "e3b9…", "digest": "The renewal price and term are agreed. …\n\n**Still open**\n- Who signs off on the budget", "guidanceId": "6f0c…", "delivered": "Shown on the meeting page to its owner and the people it is shared with."}
```

### list_meeting_whiteboards, get_meeting_whiteboard

A meeting's [whiteboards](meeting-whiteboards.md): named panels, each Markdown or a
diagram in a subset of Mermaid, that the assistant can keep up to date during the
meeting and its people can edit. `list_meeting_whiteboards` gives them without their
panels. `get_meeting_whiteboard` gives one with its panels, each marked `source`
`user` or `assistant`, or an earlier `version`. Pass `ifNewerThan` with the version
you have to poll cheaply, and `format: "markdown"` for one Markdown document.

```json
{"meetingId": "e3b9…", "whiteboardId": "4c1f…", "ifNewerThan": 7}
```

```json
{"meetingId": "e3b9…", "whiteboardId": "4c1f…", "version": 7, "notModified": true}
```

### create_meeting_whiteboard, update_meeting_whiteboard

Make a whiteboard from a `template` (`decisions`, `process`, `comparison`, `timeline`
or `blank`) or with the `panels` given, or change one. An update names the
`baseVersion` it was made from, is refused if the whiteboard has moved on since, and
takes the whole new list of `panels`. A panel the tool writes or changes is recorded
as the caller's (`source: user`), so the assistant does not change it afterwards; send
a panel with `source: "assistant"` to hand it to the assistant. Read-write key only.

```json
{"meetingId": "e3b9…", "whiteboardId": "4c1f…", "baseVersion": 7,
 "panels": [{"panelId": "decisions", "title": "Decisions", "content": "- Launch on March 4"}]}
```

```json
{"meetingId": "e3b9…", "whiteboardId": "4c1f…", "title": "Plan", "version": 8, "panels": [{"panelId": "decisions", "title": "Decisions", "kind": "markdown", "content": "- Launch on March 4", "source": "user", "updatedBy": "user@example.com", "updatedAt": "…"}], "…": "…"}
```

### list_summary_profiles

The summary profiles a meeting can be summarized with, `default` first: each
one's `name` (the value `summaryProfile` takes), `label`, `description` and
whether it lists `structuredActionItems`. See
[Summary Profiles](transcript-summarization.md#summary-profiles).

```json
{}
```

```json
{
  "profiles": [
    {"name": "default", "label": "Default", "description": "The Transcript Summary sections on the Configuration page.", "structuredActionItems": false},
    {"name": "discovery-session", "label": "Discovery session", "description": "For discovery workshops …", "structuredActionItems": true}
  ],
  "default": "default"
}
```

### end_virtual_participant

Ends a running Virtual Participant, which ends its meeting, or cancels one that
waits for its scheduled launch. Calling it again for one that has already
finished changes nothing and answers `alreadyEnded: true`. Only the Virtual
Participant's owner or an Admin may end it.

```json
{"virtualParticipantId": "5f0c2c1e-…"}
```

```json
{"virtualParticipantId": "5f0c2c1e-…", "meetingId": "e3b9…", "status": "ENDED", "endReason": "Ended from an MCP client", "alreadyEnded": false}
```

## After the meeting

### list_meetings

Beside `startDate`, `endDate`, `status`, `participant` and `limit`, it takes
`labels` (every pair must match), `audience`, `updatedSince` and `endedSince`
(ISO 8601). Each meeting has `labels`, `audience`, `status`, `startTime`,
`endTime` (null until it has ended), `owner`, `summaryProfile` (the summary
profile the meeting is summarised with, null when the meeting names none, which
means the shipped `default`), `summaryReady`,
and `virtualParticipantId` when a Virtual Participant captured it, beside the
earlier fields. The filters are applied before `limit`.

Meetings are found by when they were created: `startDate` and `endDate` bound
that, and the default is the last 7 days. When `updatedSince` or `endedSince`
is given without `startDate`, the search starts a day before that time if that
is earlier than 7 days ago, and the answer's `filters.createdFrom` and `note`
say where it started; pass `startDate` to look further back. A meeting has no
separate end time, so `endTime` and `endedSince` use when an ended meeting last
changed: `endedSince` never leaves out a meeting that ended after the time, but
also lists one that ended earlier and was changed after it.

```json
{"labels": {"project": "website-relaunch"}, "endedSince": "2026-12-01T00:00:00Z"}
```

```json
{
  "meetings": [
    {"meetingId": "e3b9…", "meetingName": "Website relaunch review", "status": "ENDED", "startTime": "2026-12-01T15:00:04Z", "endTime": "2026-12-01T15:58:40Z", "owner": "user@example.com", "labels": {"project": "website-relaunch", "phase": "2"}, "audience": "external", "summaryProfile": null, "summaryReady": true, "virtualParticipantId": "5f0c2c1e-…", "meetingUrl": "https://…"}
  ],
  "count": 1,
  "filters": {"labels": {"project": "website-relaunch"}, "endedSince": "2026-12-01T00:00:00Z", "createdFrom": "2026-11-24T…", "…": "…"},
  "note": "Only meetings created since 2026-11-24T… were checked; …"
}
```

### get_meeting_consent

The meeting's [consent record](meeting-consent.md), without the evidence
images. Every value is LMA's own, as the record stores it:

| Field | Values |
|---|---|
| `mode` | `off`, `notify` or `opt_in` |
| `rule` | `each_participant` or `owner_attests` for an opt-in meeting; null otherwise, as only opt-in uses a rule |
| `status` | `pending`, `granted`, `objected` or `not_required` |
| `captureAllowed` | Whether the meeting may be captured now |
| `noticePostedAt` | When the notice was first posted |
| `notices` | Each posting of the notice kept on the record: `channel` (`chat`, `voice`, `banner` or `invite`) and `postedAt` |
| `events` | One entry per consent event, in the order recorded: `participantName`, `decision` (`consent` or `objection`), `source` (`chat`, `voice`, `ui` or `attestation`), `at` and `speakerId` |
| `attestation` | The owner's attestation (`attestedBy`, `method`, `at`), or null |
| `hasEvidence` | Whether evidence images exist; the images are not returned |
| `startedAt`, `updatedAt` | When consent was started, and when the record last changed |

The names are those the record stores, so no value is translated. `at` is when
the reply or attestation happened, or when it was recorded if the record holds
only that. `speakerId` is null for every event: a consent event records the
participant's display name, not the transcript speaker it was heard from, and
LMA does not match a name to a speaker, because two people can show the same
name. The record holds display names only, so an event has no email address.

A meeting whose consent was never started answers with `mode` `off`, `status`
`not_required`, no notices or events, and `captureAllowed` as the meeting's
capture gate. The evidence images are in `get_meeting_record`'s `consent`
section, for the meeting's owner and Admins.

The meeting export's `consent` part is the same record in the same shape; see
[the meeting export](meeting-export.md#consent).

```json
{"meetingId": "e3b9…"}
```

```json
{
  "meetingId": "e3b9…",
  "mode": "opt_in",
  "rule": "each_participant",
  "status": "granted",
  "captureAllowed": true,
  "noticePostedAt": "2026-12-01T15:00:30Z",
  "notices": [{"channel": "chat", "postedAt": "2026-12-01T15:00:30Z"}],
  "events": [
    {"participantName": "Dana", "decision": "consent", "source": "chat", "at": "2026-12-01T15:01:02Z", "speakerId": null},
    {"participantName": "Bo", "decision": "consent", "source": "voice", "at": "2026-12-01T15:01:40Z", "speakerId": null}
  ],
  "attestation": null,
  "hasEvidence": true,
  "startedAt": "2026-12-01T15:00:00Z",
  "updatedAt": "2026-12-01T15:01:40Z"
}
```

### get_meeting_export

One structured record of the meeting, the [meeting export](meeting-export.md)
(`schemaVersion` 1). `include` lists the parts to add: `transcript`, `chat`,
`summary`, `checklist`, `consent`, `actionItems`, `keyframes` and `guidance`,
all of them by default. A large export is answered with
`{exportUrl, expiresAt, sizeBytes}` instead: a link to it, signed for ten
minutes.

```json
{"meetingId": "e3b9…", "include": ["summary", "actionItems", "checklist"]}
```

```json
{
  "schemaVersion": 1,
  "exportedAt": "2026-12-01T16:05:00Z",
  "lmaVersion": "0.4.1",
  "meeting": {"meetingId": "e3b9…", "name": "Website relaunch review", "summaryProfile": "discovery-session", "transcriptRetained": true, "…": "…"},
  "agenda": {"text": "Status\nBudget\nNext steps", "source": "mcp"},
  "context": "Goal: agree the launch budget.",
  "summary": {"profile": "discovery-session", "source": "generated", "sections": [{"key": "DECISIONS", "label": "Decisions", "markdown": "…"}]},
  "actionItems": [{"text": "Send the budget", "owner": "Dana", "dueDate": "2026-12-08", "dueText": "next Tuesday", "startTime": 1832.5, "segmentId": "…"}],
  "checklist": [{"itemId": "q1", "text": "Who owns the budget?", "status": "covered", "…": "…"}]
}
```

### regenerate_meeting_summary

Generates the summary again; the new summary replaces the current one when it
finishes. `summaryProfile` writes this summary with another
[summary profile](transcript-summarization.md#summary-profiles); the meeting's
own profile is not changed.

```json
{"meetingId": "e3b9…", "summaryProfile": "discovery-session"}
```

```json
{"meetingId": "e3b9…", "started": true, "summaryProfile": "discovery-session", "message": "A new summary is being generated. …"}
```

## Server version

`initialize` answers `serverInfo.version` with the LMA version the stack runs
(its release, as the web UI shows it).
