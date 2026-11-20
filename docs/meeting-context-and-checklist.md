---
title: "Meeting Context, Labels, Audience and Checklist"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Context, Labels, Audience and Checklist

People often prepare a meeting before it starts, with an AI assistant or by
hand: what the meeting is for, what it has to cover, which open questions need
answers. Beside its [agenda](meeting-agenda.md), a meeting in LMA can carry four
inputs that hold that preparation, so the meeting assistant and the summary can
use it and the meeting page can show how much of it the meeting covered.

| Input | What it is for |
|---|---|
| **Labels** | Key/value tags that let another system find its meetings, for example `project=website-relaunch`. |
| **Audience** | Whether the meeting is `internal` (only the owner's organization) or `external` (customers or partners present). A system that receives the meeting's content can route it by this. |
| **Meeting context** | A briefing for the assistant and the summary: goals, background, terms, what a good outcome looks like. Markdown. |
| **Checklist** | The items the meeting should cover (questions to ask, topics, risks to probe, things to confirm, decisions, actions), each with a status, who set it, its evidence in the transcript and a note. |

All four are optional. A meeting that has none of them is created, shown,
summarized and answered about exactly as before.

## What is stored

The four inputs are stored on the meeting record, beside the agenda, and are
returned by `getCall` and the `onUpdateCall` subscription on the `Call` type:

| Field | Meaning |
|---|---|
| `Labels` | A list of `{ Key, Value }`. An empty list once the labels were cleared. |
| `Audience` | `internal`, `external`, or absent (unspecified, the default). |
| `MeetingMetadataUpdatedAt` | When the labels or the audience last changed. |
| `MeetingContext` | The meeting context, Markdown. |
| `MeetingContextUpdatedBy`, `MeetingContextUpdatedAt` | Who last set the meeting context, and when. |
| `Checklist` | The checklist items, in order. |
| `ChecklistUpdatedAt` | When the checklist last changed. |

Each checklist item has these fields:

| Field | Meaning |
|---|---|
| `ItemId` | A stable id chosen by whoever adds the item, unique in the meeting. |
| `Text` | What the item is. |
| `Kind` | `question`, `topic`, `risk`, `confirm`, `decision` or `action`. |
| `Priority` | `must`, `should` (the default) or `could`. |
| `Source` | An opaque reference of the caller's (for example, where the item came from in another system), returned unchanged. |
| `Status` | `open` (the default), `covered`, `partial`, `deferred` or `not_applicable`. |
| `StatusSource` | Who set the status: `user` or `assistant`. |
| `Evidence` | Where in the meeting the status is supported: each entry a transcript segment id (`SegmentId`), a time in seconds from the meeting's start (`StartTime`) and a short quote from the transcript (`Quote`). |
| `Note` | A note on the item. |
| `UpdatedBy`, `UpdatedAt` | Who last changed the item, and when. |

## Limits

Every limit is checked by the server, which refuses a change that breaks one
with a message naming the limit; the meeting page and the Virtual Participant
forms check the same limits before they send.

| Input | Limit |
|---|---|
| Labels | At most 20. A key is 1 to 63 characters of lower-case letters, digits, `.`, `_` and `-`, starting with a letter or a digit (`[a-z0-9][a-z0-9._-]{0,62}`), and is used once. A value is at most 256 characters. |
| Audience | `internal`, `external`, or empty to clear it. |
| Meeting context | At most 20,000 characters. |
| Checklist | At most 100 items. |
| `ItemId` | 1 to 64 characters of letters, digits, `.`, `_`, `:` and `-` (`[A-Za-z0-9._:-]{1,64}`), unique in the meeting. |
| `Text` | Required, at most 500 characters. |
| `Source` | At most 256 characters. |
| `Note` | At most 1,000 characters. |
| `Quote` | At most 300 characters, verbatim from the transcript. |

Two further limits keep a checklist inside the meeting record, which also holds
the summary, the speaker names and the consent record:

- **At most 5 evidence entries per item.** When more are added, the 5 newest
  are kept.
- **At most 128 KB for the stored checklist as a whole.** A change that would
  make it larger is refused.

## Where the inputs come from

**The Virtual Participant form.** The **Create Virtual Participant** form and
the form that edits a scheduled Virtual Participant have a **Meeting
preparation** section, closed until it is opened, with the audience, the labels
(one `key=value` per line), the meeting context and the checklist. As with the
agenda, they are stored with the Virtual Participant when it is started or
scheduled. When it starts, the Virtual Participant reads them back from its own
record, with the agenda, and sends them in the event that creates the meeting,
so the meeting has them from the moment it exists; every checklist item starts
`open`. A scheduled Virtual Participant's inputs can be changed until 5 minutes
before it starts, like its other settings; an input left empty in the edit
removes it.

**The meeting page.** See [On the meeting page](#on-the-meeting-page).

**The API.** See [API](#api).

**An MCP client.** The hosted MCP server's `schedule_meeting` and `start_meeting_now` take the four inputs, and `update_scheduled_meeting` changes them before the meeting starts; `get_meeting_context`, `set_meeting_context`, `get_meeting_checklist`, `set_meeting_checklist`, `update_checklist_item` and `update_meeting_metadata` read and change them afterwards, and `list_meetings` finds meetings by their labels and audience. A status an MCP client sets is recorded as `StatusSource: user`, because the client acts for its user. See the [MCP Tools Reference](mcp-tools-reference.md).

## On the meeting page

- **Labels and audience** are shown at the top of the **Summary** tab: the
  audience, and each label as a `key=value` chip. The edit button beside them
  opens both for editing.
- **Meeting context** is a panel on the **Summary** tab, below the summary. It
  shows the context as Markdown, with who last changed it and when; **Edit**
  (or **Add context**) opens it for editing.
- **Checklist** is a panel on the **Summary** tab and a section at the top of
  the **Transcript** tab, open while the meeting is live, as the agenda is.
  Each item shows its status, its priority and kind, its text, who set it and
  when (marked when the status came from the assistant), its note, and its
  evidence. A piece of evidence with a time is a link: it opens the Transcript
  tab at that time and moves the recording player there. Those who can change
  the meeting set each item's status and note in place, and **Edit list** adds,
  changes, reorders and removes items. Saving the list keeps the status,
  evidence and note of every item that stays in it, unless **Start every item
  again as open** is chosen.

Each panel is hidden when it is empty and the viewer cannot change it. Every
change reaches every open meeting page through the `onUpdateCall`
subscription, without a reload.

**The meeting list** has two filters beside the search box: a label filter
(`key=value`, or a key alone for any meeting with that label) and an audience
filter (any, internal, external or unspecified). Like the list's search box, they apply to the meetings loaded for the list's date range. The list's Excel download has
each meeting's labels as `key=value` text; the meeting context and checklist
are not in it.

## Who can see and change them

The rule is the agenda's:

- An **Admin** can see and change any meeting's preparation.
- The meeting's **owner**, and anyone the meeting has been **shared with**, can
  see and change it.
- Anyone else does not see the meeting at all.

The rule is enforced by the server, as a condition on the write, not only by
the page. A change made by a signed-in user is always recorded under their own
username, and a status they set is always recorded as `StatusSource: user`.

**A status a user set is never changed by the assistant.** Only a user changes
it again. A backend role that changes the checklist names who the change is
from, as `StatusSource`: `user` when it acts for a person, who then has the
same control as a signed-in user, or `assistant`. The server refuses, as a
whole and with nothing written, an `assistant` change that would undo what a
user set:

- an item update to an item whose status a user set;
- a replace of the checklist that starts such an item again as open
  (`ResetStatuses`), leaves it out, or changes its text, kind, priority or
  source.

A signed-in user's changes are always a user's, and are never refused this way.

## The meeting assistant

The [meeting assistant](meeting-assistant.md) has two tools that read the
current meeting's preparation:

- `get_meeting_context` returns the meeting context, marked as a briefing that
  is quoted data rather than instructions.
- `get_meeting_checklist` returns one line per item, with its status, priority,
  kind, text, the transcript times of its evidence and its note, and a count of
  the statuses — so it can answer "what haven't we covered?".

Neither returns who changed what.

### Requests made in the meeting

Any request made in the meeting — a question typed in the meeting chat
(`LMA ask`), a request spoken to the voice assistant, or one spoken with the
wake phrase — cannot read either tool by default. LMA cannot tell who in the
meeting asked, a meeting's guests are often outside the owner's organization,
and the meeting context can hold the owner's preparation notes. This holds
whatever **Meeting Assistant › In-meeting requests** is set to: **Unrestricted**
and **Selected tools** do not offer the two tools.

An Admin can allow it with **Meeting Assistant › In-meeting requests: meeting
context and checklist** (`meeting_assist.inMeetingContextAccess`) on the
[Configuration page](configuration-guide.md):

| Value | Effect |
|---|---|
| `none` (the default) | Only the meeting page's own assistant chat is offered the two tools. |
| `allowed` | Requests made in the meeting are offered them too. An answer drawn from them is then posted in the meeting chat or spoken, for everyone in the meeting. |

LMA never posts the meeting context or the checklist in the meeting chat, and
never speaks them, on its own. With `allowed`, an answer that draws on them
reaches the meeting because an Admin chose that.

## In the summary

Two placeholders can be used in any [summary prompt](transcript-summarization.md#customizing-prompts):

- `{context}` is replaced with the meeting context, as written.
- `{checklist}` is replaced with one line per item and its status, for example
  `1. [covered] (must, question) Who owns the budget? Evidence at 1:04.`

A section whose prompt names either is generated only for a meeting that has
it, and left out of the summary of one that does not. No shipped summary
section uses them, so summaries are unchanged until a prompt of your own does.

## Retention

The four inputs are part of the meeting record, so they are deleted with the
meeting, and those stored with a Virtual Participant are deleted with it.

When [the transcript is deleted after the summary](transcript-deletion.md), each
evidence `Quote` is deleted with it, because a quote is transcript content. The
evidence's segment id and time are kept, so the checklist still says where in
the meeting each status came from, and a quote sent after the deletion is not
stored. The rest of the preparation is kept with the meeting record.

## API

The GraphQL mutations, each answering with the meeting (`Call`) and published
to `onUpdateCall`:

| Mutation | Input | What it does |
|---|---|---|
| `updateCallMeetingContext` | `CallId`, `MeetingContext` | Replaces the meeting context (empty clears it), and records who changed it and when. |
| `updateCallChecklist` | `CallId`, `Checklist` (each item's `ItemId`, `Text`, `Kind`, `Priority`, `Source`), `ResetStatuses`, `StatusSource` | Replaces the whole checklist, in the order given. An item whose `ItemId` was already present keeps its `Status`, `StatusSource`, `Evidence` and `Note`, unless `ResetStatuses` is true. |
| `updateCallChecklistItem` | `CallId`, `ItemId`, and any of `Status`, `Note` (empty clears it), `Evidence` (replaces the item's), `AddEvidence` (added after it), `StatusSource` | Changes one item. |
| `updateCallLabels` | `CallId`, `Labels` | Replaces the labels; an empty list clears them. |
| `updateCallAudience` | `CallId`, `Audience` | Sets the audience; null or empty clears it. |

`createVirtualParticipant` and `updateScheduledVirtualParticipant` take
`labels`, `audience`, `meetingContext` and `checklist` (each item's `ItemId`,
`Text`, `Kind`, `Priority` and `Source`).

A backend role calling with IAM credentials must name a `StatusSource` (`user`
or `assistant`) on `updateCallChecklist` and `updateCallChecklistItem`, and may name an `UpdatedBy` label
on the meeting context, checklist and item mutations; a signed-in user's change
is always their own. A client that subscribes to `onUpdateCall` should select
`Owner` and `SharedWith`, as the subscription requires them for every caller
who is not an Admin.
