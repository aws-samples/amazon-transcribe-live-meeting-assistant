---
title: "Meeting Agenda"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Agenda

A meeting in LMA can carry an **agenda**: a short list of the items the meeting
is meant to cover. The agenda is stored on the meeting record beside the
transcript and the summary, so everyone who can open the meeting sees the same
agenda, and every change to it reaches open meeting pages straight away.

## What is stored

The agenda is plain text, **one item per line**. That is the form every source
produces without translation — a person typing, a calendar invite pasted in, a
model reading one out of the transcript — and the meeting page shows it as a
numbered list. Bullet and numbering marks at the start of a line (`-`, `*`, `•`,
`1.`, `2)`, `a.`) are hidden when the list is shown, so a pasted list reads the
same whichever style it used; the stored text itself is kept exactly as saved.

Beside the text the meeting records three things about it:

| Field | Meaning |
|---|---|
| `AgendaSource` | Where the agenda came from: `scheduled` (given on the Virtual Participant form), `mcp` (passed by an MCP client that started or scheduled a Virtual Participant) or `ui` (typed on the meeting page). The API also accepts `derived` from a backend role. |
| `AgendaUpdatedBy` | The username of the person who last set it, or a fixed label when the backend set it. |
| `AgendaUpdatedAt` | When it was last set. |

An agenda is limited to 10,000 characters. Saving an empty agenda clears it.

## Where an agenda comes from

| Source | How |
|---|---|
| **The Virtual Participant form** (`scheduled`) | The **Create Virtual Participant** modal has an optional **Agenda** field. **Paste Meeting Invitation** fills it with the agenda the invitation lists, and leaves it as it was when the invitation lists none: the parser is told to extract an agenda, never to make one up from the title. |
| **An MCP client** (`mcp`) | The `start_meeting_now` and `schedule_meeting` tools take an optional `agenda` argument — text with one item per line, or a list of items — so an agent such as Amazon Quick can pass the agenda from a calendar invite. See [Amazon Quick MCP setup](amazon-quick-mcp-setup.md#schedule_meeting). |
| **The meeting page** (`ui`) | The Agenda panel, below. |

For the first two, the agenda is stored with the Virtual Participant when it is
started or scheduled. When the Virtual Participant starts, it reads the agenda
back from its own record and sends it in the event that creates the meeting, so
the meeting has its agenda from the moment it exists. Because the VP reads the
agenda itself, it does not depend on how the VP was launched: a meeting started
now (through the Step Functions state machine) and a scheduled one (through the
scheduler Lambda that the Virtual Participant table's stream drives) both get it.

A Virtual Participant that cannot read its agenda still joins and records the
meeting, without one; the VP's log says which. An agenda can be added on the
meeting page at any time.

## The Agenda panel

The meeting page has an **Agenda** panel, below the summary. It shows the items
as a numbered list, with a badge for where the agenda came from and a line
saying who last changed it and when. A meeting with no agenda says so.

**Edit** (or **Add agenda** on a meeting without one) opens the text for
editing, one item per line; **Save agenda** stores it. Editing is available
while the meeting is running and after it has ended. Unlike the summary, which
the end-of-meeting pipeline replaces when the meeting ends, a stored agenda is
replaced only by somebody saving a new one.

## Who can change it

The agenda follows the same rule as the rest of a meeting's editable content —
the summary and the speaker names:

- An **Admin** can change the agenda of any meeting.
- The meeting's **owner**, and anyone the meeting has been **shared with**, can
  change it.
- Anyone else does not see the meeting at all, so has no agenda to change.

The rule is enforced by the `updateCallAgenda` GraphQL mutation as a DynamoDB
condition on the write, not only by the page: the page hides the edit control
from a viewer without access, and the API refuses the write if one is sent
anyway. A change made in the browser is always recorded as source `ui` and
attributed to the signed-in user's own username, whatever the request says.

## API

```graphql
mutation {
  updateCallAgenda(input: { CallId: "...", Agenda: "Introductions\nBudget\nHiring" }) {
    CallId
    Agenda
    AgendaSource
    AgendaUpdatedBy
    AgendaUpdatedAt
    Owner
    SharedWith
  }
}
```

Every change is published to the `onUpdateCall` subscription, which is how open
meeting pages update without reloading. A client that subscribes should select
`Owner` and `SharedWith`, as the subscription requires them for every caller who
is not an Admin.

Backend roles calling with IAM credentials must name an `AgendaSource` (any of
the four values above), may set `AgendaUpdatedBy`, and may pass
`OnlyIfUnset: true` to write only while the meeting has no agenda, which is
checked atomically with the write.
