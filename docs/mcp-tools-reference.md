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
  camelCase. The two meeting guidance tools still take `meeting_id`.
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
- **Front doors.** Every tool is served on both the API key endpoint and the
  OAuth (AgentCore Gateway) endpoint, with the same definitions. The OAuth
  endpoint has no key scopes: a user signed in through it can call every tool,
  including those that change something, for the meetings they may change, as on
  the meeting page.
- **Legacy keys** keep `schedule_meeting` and `start_meeting_now` as they were:
  their `labels`, `audience`, `context`, `checklist` and `shareWith` arguments
  need a **Read and write** key.
- **Annotations.** Every tool declares `readOnlyHint`, `destructiveHint` and
  `idempotentHint`. A key with the **Read only** scope is offered, and answered
  for, the read-only tools only.

## Every tool

| Tool | Changes something | Who, beyond the general rule |
|---|---|---|
| `search_lma_meetings` | no | |
| `list_meetings` | no | |
| `get_meeting_summary` | no | |
| `get_meeting_transcript` | no | |
| `get_meeting_record` | no | its `consent` section (with the evidence images): owner and Admin |
| `get_meeting_cost` | no | |
| `get_meeting_agenda` | no | |
| `get_meeting_context` | no | |
| `get_meeting_checklist` | no | |
| `get_meeting_consent` | no | |
| `get_virtual_participant_status` | no | |
| `list_virtual_participants` | no | |
| `list_meeting_guidance` | no | |
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
| `ask_meeting_assistant` | yes | |
| `regenerate_meeting_summary` | yes | |
| `end_meeting` | yes | the Virtual Participant's owner and Admin |
| `end_virtual_participant` | yes | the Virtual Participant's owner and Admin |
| `delete_meeting` | yes | owner and Admin |

That is 28 tools. The examples below show the `arguments` of a `tools/call`
request and the JSON of the answer, shortened where marked.

## Before the meeting

### schedule_meeting

Schedules a Virtual Participant. Beside the meeting's name, platform, ID and
time, and the optional `agenda`, it takes the meeting's preparation, each
optional and checked with the limits in
[Meeting Context, Labels, Audience and Checklist](meeting-context-and-checklist.md#limits):
`labels` (an object of key to value), `audience` (`internal` or `external`),
`context` (Markdown), `checklist` (items of `itemId`, `text`, `kind`, and
optionally `priority` and `source`; each starts `open`) and `shareWith` (email
addresses). The Virtual Participant stores them and puts them on the meeting
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
`checklist` and `shareWith`. Each one given replaces that setting, and an empty
value removes it. It is refused within 5 minutes of the start ("starts in less
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
clears it), or both. Answers `{labels, audience}`.

```json
{"meetingId": "e3b9…", "labels": {"project": "website-relaunch", "phase": "2"}, "audience": "external"}
```

```json
{"meetingId": "e3b9…", "labels": {"project": "website-relaunch", "phase": "2"}, "audience": "external", "updatedAt": "2026-12-01T15:31:00Z"}
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
`endTime` (null until it has ended), `owner`, `summaryReady`, and
`virtualParticipantId` when a Virtual Participant captured it, beside the
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
    {"meetingId": "e3b9…", "meetingName": "Website relaunch review", "status": "ENDED", "startTime": "2026-12-01T15:00:04Z", "endTime": "2026-12-01T15:58:40Z", "owner": "user@example.com", "labels": {"project": "website-relaunch", "phase": "2"}, "audience": "external", "summaryReady": true, "virtualParticipantId": "5f0c2c1e-…", "meetingUrl": "https://…"}
  ],
  "count": 1,
  "filters": {"labels": {"project": "website-relaunch"}, "endedSince": "2026-12-01T00:00:00Z", "createdFrom": "2026-11-24T…", "…": "…"},
  "note": "Only meetings created since 2026-11-24T… were checked; …"
}
```

### get_meeting_consent

The meeting's [consent record](meeting-consent.md), without the evidence
images: `mode` (`off`, `notify` or `opt-in`), `consentGivenBy` for an opt-in
meeting (`each_participant` or `owner_attests`), `status`, `captureAllowed`,
`noticePostedAt`, the owner's `attestation`, one row per participant (`name`,
`status` `consented`, `objected` or `no_reply`, `at` and `method`, the channel
the reply came by), and `evidence` (`exists` and `count`). The record holds
participants' display names only, so a row has no email address. A meeting
whose consent is off, or was never started, answers `{"mode": "off"}`. The
evidence images are in `get_meeting_record`'s `consent` section, for the
meeting's owner and Admins.

```json
{"meetingId": "e3b9…"}
```

```json
{
  "meetingId": "e3b9…",
  "mode": "opt-in",
  "consentGivenBy": "each_participant",
  "status": "granted",
  "captureAllowed": true,
  "noticePostedAt": "2026-12-01T15:00:30Z",
  "attestation": null,
  "participants": [
    {"name": "Dana", "status": "consented", "at": "2026-12-01T15:01:02Z", "method": "chat"},
    {"name": "Bo", "status": "consented", "at": "2026-12-01T15:01:40Z", "method": "voice"}
  ],
  "evidence": {"exists": true, "count": 2}
}
```

## Server version

`initialize` answers `serverInfo.version` with the LMA version the stack runs
(its release, as the web UI shows it).
