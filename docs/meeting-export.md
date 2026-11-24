---
title: "Meeting Export"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Export

A system that keeps its own record of a project, such as an AI assistant's
project folder, a CRM or a document store, often wants one structured record of
each meeting rather than several answers to piece together. The **meeting
export** is that record. It is one JSON document that holds:

- the meeting's details, labels and audience;
- who was there, and where each name came from;
- the consent record;
- the agenda, meeting context and checklist;
- what was said, typed in the chat and read from the shared screen;
- the links shared in the chat;
- the key frames, as alt text;
- the summary, section by section;
- the action items, with their owners and due dates;
- the guidance given during the meeting;
- the whiteboards, as they are now.

The format is published as a JSON Schema,
[`docs/schemas/meeting-export-v1.schema.json`](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/main/docs/schemas/meeting-export-v1.schema.json).

## Getting an export

| Where | How |
|---|---|
| Meeting page | **Download** > **Meeting export (.json)**. The file is prepared on the server and offered as a link, like the archives. |
| Everything (.zip) | The archive holds the export as `meeting-export.json`, and its `README.md` lists it. |
| MCP | The `get_meeting_export` tool on the [API key endpoint](mcp-api-key-auth.md), with `meetingId` and an optional `include`. |

An export is read by the people who can read the meeting: its owner, the users
it is shared with, and Admins. Anybody else gets the same answer as for a
meeting that does not exist.

### `include`

`include` is a list of the parts to add. By default, every part is included.

| Part | What it adds |
|---|---|
| `transcript` | Speech in `transcript`, `participants`, and, with `keyframes`, the text read from the shared screen as `screen` entries. |
| `chat` | Chat messages in `transcript`, `participants`, and `links`. |
| `summary` | `summary`. |
| `checklist` | `checklist`. |
| `consent` | `consent`. |
| `actionItems` | `actionItems`. |
| `keyframes` | `keyframes`. |
| `guidance` | `guidance`: the notifications of the category `guidance` ([meeting notifications](meeting-notifications.md)). |
| `whiteboards` | `whiteboards`: each [whiteboard](meeting-whiteboards.md) as it is now, with its panels; not its earlier versions. |

`meeting`, `agenda` and `context` are always included.

### Large exports

When an export is larger than about 5 MB, the MCP tool answers with
`{exportUrl, expiresAt, sizeBytes}` instead of the export itself. `exportUrl` is
a link to the JSON document, signed for ten minutes. The size is measured as the
MCP server encodes its answer, which is larger than the document on its own.

- The document is written under the recordings bucket's `lma-meeting-archives/`
  prefix, which its lifecycle rule empties after a day. Deleting the meeting
  deletes it too.
- The caller's access to the meeting is checked before anything is read or
  written.
- The link works for anyone who holds it until it expires, so do not share it.

The meeting page always offers its download as a link of this kind.

## The format

```json
{
  "format": "lma-meeting-export",
  "schemaVersion": 1,
  "exportedAt": "2026-11-19T16:05:00Z",
  "lmaVersion": "0.4.1",
  "meeting": {
    "meetingId": "...", "name": "...", "platform": "Zoom", "captureSource": "VIRTUAL_PARTICIPANT",
    "owner": "user@example.com", "sharedWith": ["..."],
    "status": "ENDED", "startTime": "...", "endTime": "...", "durationSeconds": 3540,
    "labels": {"project": "acme-claims"}, "audience": "external",
    "summaryProfile": "discovery-session", "transcriptRetained": true
  },
  "participants": [
    {"speakerId": "remote-spk_0", "label": "Other Participant (spk_0)", "name": "Priya Shah", "nameSource": "manual", "email": null, "consent": "unknown"}
  ],
  "consent": {"meetingId": "...", "mode": "opt_in", "rule": "each_participant", "status": "granted", "captureAllowed": true,
              "noticePostedAt": "...", "notices": [{"channel": "chat", "postedAt": "..."}],
              "events": [{"participantName": "Priya Shah", "decision": "consent", "source": "chat", "at": "...", "speakerId": null, "speakerLinkBasis": null, "speakerLinkedAt": null}],
              "attestation": null, "hasEvidence": true, "startedAt": "...", "updatedAt": "..."},
  "agenda": {"text": "...", "source": "mcp"},
  "context": "...",
  "checklist": [{"itemId": "q1", "text": "...", "kind": "question", "status": "covered",
                 "evidence": [{"segmentId": "...", "startTime": 12.5, "endTime": 15.0, "quote": "..."}]}],
  "transcript": [
    {"segmentId": "...", "startTime": 12.5, "endTime": 15.0, "speakerId": "remote-spk_0", "channel": "REMOTE", "kind": "speech", "text": "..."}
  ],
  "links": [{"url": "...", "startTime": 640.0, "sharedBy": "chat-1"}],
  "keyframes": [{"keyframeId": "...", "startTime": 900.0, "altText": "..."}],
  "summary": {"profile": "discovery-session", "source": "generated", "sections": [{"key": "DECISIONS", "label": "Decisions", "markdown": "..."}]},
  "actionItems": [{"text": "...", "owner": null, "dueDate": null, "dueText": null, "startTime": 1832.5, "endTime": 1838.0, "segmentId": "..."}],
  "guidance": [{"type": "ask_question", "title": "...", "body": "...", "refs": ["..."], "createdAt": "..."}]
}
```

**Stability.** A field's name and meaning never change within
`schemaVersion: 1`. A field added later is optional, and a consumer must ignore
any field it does not know. A change that cannot follow those rules gets a new
`schemaVersion`.

**Recognising an export.** Every export starts with
`"format": "lma-meeting-export"`, before `schemaVersion`, so a file can be
recognised by its content alone. LMA always writes it. The schema lists it as
optional, so a document validates with or without it.

### `meeting`

- `name` is the meeting's name as the meeting list shows it.
- `platform` is the meeting platform a Virtual Participant joined: Zoom,
  Teams, Chime, Webex or Google Meet. It is null for the other capture sources,
  and for meetings created before the platform was recorded.
- `captureSource` says how the meeting was captured.
- `endTime` is approximate. It is when an ended meeting last changed, which is
  its end unless somebody changed the meeting afterwards. It is null while the
  meeting runs.
- `summaryProfile` is the meeting's
  [summary profile](transcript-summarization.md#summary-profiles), or
  `default`. The profile that wrote the stored summary is `summary.profile`,
  which can differ: a regeneration can choose another profile for one run.
- When the transcript was deleted under the
  [Retention settings](transcript-deletion.md), `transcriptRetained` is false
  and `transcriptDeletedAt` says when.

### `participants`

`participants` lists every speaker in the transcript and every sender in the
chat. A speaker is one voice: a channel's speaker as stored, with its voice id
when speakers were separated. LMA does not merge voices into one person, even
when the meeting page shows them under one name.

- `speakerId` is opaque and unique within the export, for example
  `remote-spk_0`, `local-1` or `chat-1`. Transcript entries, links and consent
  events refer to it. A speaker has the same id whichever parts are exported,
  and the same as `get_meeting_consent` gives, for the same transcript.
- `label` is how LMA shows the speaker.
- `name` is a person's name, or null when no name was given or confirmed. LMA
  never guesses one.
- `consent` is `granted` or `objected`, from the speaker's latest reply among
  the consent events linked to that speaker, and `unknown` when there is none.
  A reply is linked to a speaker only when it was recorded: a reply said aloud
  to a Virtual Participant while the meeting was being transcribed is that
  speaker's own line of the transcript (see
  [Which speaker gave a reply](meeting-consent.md#which-speaker-gave-a-reply)).
  A chat reply, a reply heard while consent was awaited, an entry made on the
  meeting page and an attestation are not linked to any speaker, and LMA does
  not match a name to a speaker, because two people can show the same name and
  a voice can be named after the meeting. A speaker's `consent` is also
  `unknown` when the same participant (by the name the consent record holds)
  replied otherwise afterwards in a reply not linked to that speaker, such as a
  chat objection after a spoken consent, because nothing says which speaker
  that later reply came from. A linked reply counts for the voice it was linked
  to only: voices merged with it on the meeting page do not share it. The
  meeting's consent itself is in [`consent`](#consent).

`nameSource` says where a name came from:

| `nameSource` | Meaning |
|---|---|
| `manual` | Somebody named the voice on the meeting page's Speakers panel, or confirmed a suggested name. |
| `transcript` | The speaker named themselves in the meeting, and LMA applied the name ([finding the names automatically](transcription-and-translation.md#finding-the-names-automatically)). |
| `platform` | The meeting platform, the capture client or a [voice enrollment](voice-enrollment.md) match gave the name with the audio. |
| `enrolled` | Reserved. An enrolled voice's match is stored as the name alone, in the same form a platform's name takes, so it is reported as `platform`. |
| `none` | No name is known. A name suggested from the transcript that nobody has confirmed counts as none. |

`email` is null in schema version 1.

### `transcript`, `links` and `keyframes`

Transcript entries are in time order. `kind` is one of:

- `speech`, from the REMOTE or LOCAL channel;
- `chat`, a message typed in the meeting chat;
- `screen`, the text read from a key frame of shared content, with a null
  `speakerId` and `channel`.

The meeting assistant's own answers are not in the export.

`links` lists each address typed in the chat once, with the time it was first
shared and who shared it.

`keyframes` lists the key frames of shared content. Each has an `altText`: its
title, the start of its text, or the time it was shown. Frames of the camera
video, and repeats of an earlier frame, are left out. **The export holds no
images**, and no links to them; the summary's pictures become their alt text.

### `consent`

`consent` is the consent record in LMA's own values, in the same shape as the
answer of the MCP [`get_meeting_consent`](mcp-tools-reference.md#get_meeting_consent)
tool, which is built by the same code:

- `mode` is `off`, `notify` or `opt_in`; `rule` is `each_participant` or
  `owner_attests` for an opt-in meeting, and null otherwise;
- `status` is `pending`, `granted`, `objected` or `not_required`, and
  `captureAllowed` says whether the meeting may be captured (null when its
  consent was never started);
- `notices` lists each posting of the notice with its `channel` and `postedAt`;
- `events` has one entry per consent event, with `participantName`, `decision`
  (`consent` or `objection`), `source` (`chat`, `voice`, `ui` or
  `attestation`), `at`, `speakerId`, `speakerLinkBasis` and `speakerLinkedAt`.
  `speakerLinkBasis` says how the event was linked to the speaker who gave it,
  and `speakerLinkedAt` when; both are null for an event that was never linked.
  The basis is `same_segment`, the reply is that speaker's own line of the
  transcript, or `platform_identity`, a chat reply whose sender the meeting
  platform gave the same participant id as that speaker's lines (Teams through
  Azure Communication Services and the Zoom Meeting SDK; the id itself is not
  exported). `speakerId` is the linked speaker's id in
  [`participants`](#participants), and is null when the event was never
  linked, or when the transcript no longer confirms the link under that
  speaker: the lines expired or were deleted, or somebody named one of them as
  another person's on the meeting page. See
  [Which speaker gave a reply](meeting-consent.md#which-speaker-gave-a-reply).
  After the transcript was deleted the link is still given, with a null
  `speakerId`. Every event has `speakerLinkBasis` and `speakerLinkedAt` (the
  schema requires both), null when there is no link;
- `hasEvidence` says whether evidence images exist.

The evidence images themselves are only in the Everything archive, for the
meeting's owner and Admins ([meeting consent](meeting-consent.md)).

### `checklist`

`checklist` holds the checklist items as the MCP `get_meeting_checklist` tool
gives them, with their status and evidence
([meeting context and checklist](meeting-context-and-checklist.md)). Each piece
of evidence has a `segmentId`, a `startTime` and an `endTime`, so it can be placed
on the timeline even by a consumer that renumbers the segments. `startTime` is
the time stored with the evidence, else its segment's start; `endTime` is its
segment's end. Either is null when the evidence names no segment, or names one
the transcript does not hold. Once the transcript was deleted, each piece of
evidence keeps its segment id and its stored time, and has no quote and no
`endTime`.

### `summary` and `actionItems`

`summary.sections` lists the stored summary's sections in the same reading
order as the meeting page and the downloads. Each section has:

- `key`, the stored section title;
- `label`, a heading for the section;
- `markdown`, the section's text.

A summary edited by hand is one section.

`actionItems` lists the structured action items of a summary written with a
profile that has them
([structured action items](transcript-summarization.md#structured-action-items)).

- `owner` is a name as said in the meeting, and null when nobody was named.
- `dueDate` is a date written `YYYY-MM-DD`, and null when no date was said.
  `dueText` is how the due date was said.
- `startTime`, `endTime` and `segmentId` place the item on the transcript, as
  for checklist evidence: `endTime` is the end of the segment the item names,
  and null when it names none or the transcript was deleted.

The summary's sections are Markdown and name no segments, so they have no
times. Guidance items refer to segments by id in `refs`.

For a summary from a profile without structured action items, the list is
empty.

## Retention

When the transcript was deleted under the Retention settings:

- `transcript`, `links` and `participants` are left out;
- `meeting.transcriptRetained` is false;
- checklist evidence has no quotes, and neither it nor the action items have an
  `endTime`.

The guidance and the whiteboards are deleted with the transcript, so `guidance` and
`whiteboards` are then empty. The
summary and the action items are kept, as the meeting keeps them.

## Related

- [Transcript Summarization](transcript-summarization.md): summary profiles and
  structured action items.
- [MCP tools reference](mcp-tools-reference.md): `get_meeting_export` and the
  other meeting tools.
- [Web UI guide](web-ui-guide.md): the meeting page's Download menu.
