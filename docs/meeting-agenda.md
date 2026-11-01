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
| `AgendaSource` | Where the agenda came from: `scheduled` (given on the Virtual Participant form), `mcp` (passed by an MCP client that started or scheduled a Virtual Participant), `ui` (typed on the meeting page) or `derived` (read from the transcript). |
| `AgendaUpdatedBy` | The username of the person who last set it, or a fixed label when the backend set it. |
| `AgendaUpdatedAt` | When it was last set. |

An agenda is limited to 10,000 characters. Saving an empty agenda clears it.

## Where an agenda comes from

| Source | How |
|---|---|
| **The Virtual Participant form** (`scheduled`) | The **Create Virtual Participant** modal has an optional **Agenda** field. **Paste Meeting Invitation** fills it with the agenda the invitation lists, and leaves it as it was when the invitation lists none: the parser is told to extract an agenda, never to make one up from the title. |
| **An MCP client** (`mcp`) | The `start_meeting_now` and `schedule_meeting` tools take an optional `agenda` argument — text with one item per line — so an agent such as Amazon Quick can pass the agenda from a calendar invite. See [Amazon Quick MCP setup](amazon-quick-mcp-setup.md#schedule_meeting). |
| **The meeting page** (`ui`) | The Agenda panel, below. |
| **The transcript** (`derived`) | An agenda somebody stated in the meeting, read from the transcript automatically about five minutes in, or when **Derive agenda** is pressed. See [Deriving an agenda from the transcript](#deriving-an-agenda-from-the-transcript). |

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
replaced only by somebody saving a new one or pressing **Derive agenda** (which
asks first); the automatic derivation pass writes only while a meeting has none.

## Deriving an agenda from the transcript

Meetings often open with somebody saying what they will cover — "we have three
things today: the budget, hiring and the Q3 plan". LMA reads the transcript for
such a statement in two ways, and stores what it finds with source `derived`.

**Automatically, once per meeting.** When a meeting that has no agenda passes
about five minutes of transcript, the transcript so far is read once for a
stated agenda. If one was stated, it is stored; if not, nothing is stored and
the meeting is not read again automatically. For an uploaded recording, whose
segments arrive in a burst, the pass can run before the whole opening has been
stored, so it may read less than five minutes. The same holds if the pass fails —
the model call is throttled, say: it is not retried, and **Derive agenda**
remains the way to try again. The pass never replaces an agenda:
it writes only while the meeting still has none, checked in the same database
operation as the write, so an agenda somebody sets while it runs is kept.

**On demand, with Derive agenda.** The Agenda panel's **Derive agenda** button
reads the transcript again — useful when the agenda was stated later than five
minutes in, or after the meeting. It is available to the same people who can
edit the agenda. If the meeting already has an agenda that somebody gave (on the
Virtual Participant form, through an MCP client or on the page), the panel asks
before replacing it; if no agenda turns out to have been stated, the current one
is kept. The panel then says what happened, including "no agenda was stated in
the meeting".

**Only a stated agenda is stored.** The model is asked whether an agenda was
stated, for its items, and for the words it was stated in, and is told that
topics which merely come up are not an agenda. Its answer is accepted only if
those quoted words are actually in the transcript, and each item is kept only
if it is drawn from them — at least half of its content words appear in the
quote — so an agenda
made up from the discussion has nothing to point at and is discarded. Both passes read at
most about the first half hour of conversation, where agendas are stated.

**Model and cost.** Derivation uses the model summaries use: **Summaries and
meeting insights** in the Amazon Bedrock Models section of the
[Configuration page](configuration-guide.md), whose default is the
**BedrockModelId** stack parameter. Each pass is one model call, recorded
against the meeting as **Agenda derivation** in its
[cost estimate](meeting-cost-estimate.md). The automatic pass runs at most once
per meeting.

**Changing or turning off the automatic pass.** The Call Event Processor
function's `AGENDA_DERIVATION_AFTER_SECONDS` environment variable sets how far
into a meeting the automatic pass runs (300 seconds by default); `0` turns it
off. It is set by the stack, so a change made on the function directly is
undone by the next stack update. The **Derive agenda** button works either way.

The button's request is answered within AppSync's 30-second limit in the usual
case. If the model call takes longer, the panel says it is still reading the
transcript; the pass usually finishes on its own, and an agenda it finds
appears on the page without a reload.

## Agenda coverage in the summary

When a meeting has an agenda, its summary includes an **AGENDA COVERAGE**
section: one line per agenda item, in agenda order, marked **Covered**,
**Partly covered** or **Not covered**, with a few words on what was discussed
or decided. A meeting without an agenda has no such section; it is left out
rather than generated empty.

The section is one of the shipped summary prompts, so deployments that have not
removed it get it, after an upgrade too. Its prompt names the agenda with the
`{agenda}` placeholder, and any prompt of your own can do the same; see
[Customizing Prompts](transcript-summarization.md#customizing-prompts). To turn
the section off, set its prompt to `NONE` in the **Transcript Summary** section of
**Configuration ▸ Configuration**.

## Asking the assistant about the agenda

The Strands meeting assistant has a `get_meeting_agenda` tool that reads the
current meeting's agenda. Asked how the meeting is going against it, the
assistant reads the agenda and the transcript with its times, and lists every
item in agenda order, one line each, as **covered** (with the times it was
discussed), **in progress** or **remaining**, noting briefly any substantial
topic discussed that is not on the agenda. When the meeting has no agenda it
says so instead of making one up. The tool gives the assistant the agenda's
items and where the agenda came from, but not who last changed it, because the
voice assistant's and the meeting chat's answers reach everyone in the meeting.

The same question can be asked three ways:

- **The AGENDA CHECK chat button** on the meeting page's Meeting Assist panel.
  It is one of the shipped default buttons, so a deployment that has not saved a
  custom button list has it after an upgrade; a deployment with a custom list
  can add it from **Edit Chat Buttons** (the custom list replaces the shipped
  one, so it is not added there automatically).
- **The voice assistant** — for example *"Hey Alex, what's left on the
  agenda?"* The Nova Sonic voice assistant hands information requests to the
  same meeting assistant, which uses the agenda tool.
- **The meeting chat** — `LMA ask what's left on the agenda?` A question typed
  in the meeting chat is answered from the current meeting's transcript, its
  agenda and web search only, and the answer is posted in the chat for
  everyone. **Guests may read the agenda of the meeting they are in** this way,
  just as they can ask about its transcript.

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
