---
title: "Meeting Playbooks"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Playbooks

A **meeting playbook** sets a meeting up for one kind of meeting: a sprint
planning, an architecture review, a customer discovery call, an incident review,
a retrospective. Applied to a meeting, it makes the [whiteboards](meeting-whiteboards.md)
that kind of meeting needs, turns on the [live guidance](live-guidance.md) checks
that matter for it and no others, adds quick actions to the meeting's assistant
chat, and gives the meeting assistant its instructions for that meeting.

A playbook is a [skill](meeting-assistant-skills.md) with a playbook section. It
is written, imported, exported and deleted on the Skills page like any other
skill, and the skill's instructions are what the assistant follows in a meeting
that runs it.

**No playbook is applied unless someone chooses one.** A meeting runs a playbook
only when its owner or an Admin applies one on the meeting page, when the
Virtual Participant that records it was given one, or when an Admin sets a
default playbook in the configuration profile. The shipped profile names none.
A playbook suggested from the meeting's name or agenda is shown, never applied by
itself.

## Contents

- [What a playbook does](#what-a-playbook-does)
- [The shipped playbooks](#the-shipped-playbooks)
- [Choosing a playbook](#choosing-a-playbook)
- [Suggestions](#suggestions)
- [Applying, switching and removing](#applying-switching-and-removing)
- [What a playbook holds](#what-a-playbook-holds)
- [Writing a playbook](#writing-a-playbook)
- [Cost](#cost)
- [Who can see and choose playbooks](#who-can-see-and-choose-playbooks)
- [API](#api)
- [Limits](#limits)
- [Limitations](#limitations)

## What a playbook does

Applying a playbook to a meeting in progress does four things:

| Part | What happens | When it takes effect |
|---|---|---|
| **Whiteboards** | Each whiteboard the playbook names is made from its template, with its title and its instructions for the assistant, and with the assistant's updates on or off as the playbook says. A meeting that already has a whiteboard of the same title and template keeps that one rather than getting a second. While any of them is kept by the assistant, the meeting's **Assistant keeps whiteboards updated** switch is turned on. | At once; the assistant fills them at its next run. |
| **Live guidance** | Live guidance is turned on for the meeting, and only the checks the playbook names are published, with the playbook's assertiveness and item rate. | From the next live guidance run, usually within a minute or two of speech. |
| **Quick actions** | The playbook's quick actions are shown first among the buttons of the meeting page's assistant chat. | At once. |
| **Instructions** | The playbook skill's instructions are in the meeting assistant's prompt for every request about the meeting that may use the skill, as if it had loaded the skill. | From the next question. |

A playbook turns on only what it names. A playbook with no guidance part leaves
live guidance as it was; one that keeps none of its whiteboards updated leaves
the whiteboard updates as they were. Everything the deployment turns off stays
off: if an administrator has turned live guidance or the whiteboard updates off
for the deployment (**Runs per meeting at most** set to 0), or **Fact checks** or
**Lookups** are off, the playbook does not turn them on, and the meeting page
says so.

Live guidance and whiteboards are shown in LMA only. A playbook never posts to the
meeting chat or speaks.

## The shipped playbooks

LMA ships five playbooks, seeded with the [default skills](meeting-assistant-skills.md#default-skills)
and by the same rules: a playbook an Admin edited is never replaced by a later
release, one an Admin deleted is never created again, and an unedited one is
brought up to the new release's text. Their files are in
`lma-ai-stack/source/lambda_functions/configuration_seeder/default_skills/<name>/SKILL.md`.

| Playbook | Whiteboards | Live guidance checks | Quick actions |
|---|---|---|---|
| **Sprint planning** (`sprint-planning`) | Sprint backlog (user stories), Sprint plan (Gantt chart), Risks and dependencies (risk register) | Actions without an owner or a due date, things to clarify, risks to probe, topics not yet covered, time checks; balanced, at most 6 items in 10 minutes | Scope vs capacity, Stories without owners, Sprint summary |
| **Architecture review** (`architecture-review`) | Architecture (architecture diagram), Decision log, Open questions and follow-ups (decisions and open questions) | Risks to probe, things to clarify, fact checks, questions to ask, actions without an owner; balanced, at most 6 | Trade-offs so far, Unanswered questions, Draft decision record |
| **Customer discovery** (`customer-discovery`) | Customer call notes (blank, kept under pain points, requirements, objections, budget and timing, next steps), Requirements and alternatives (comparison) | Lookups, questions to ask, topics not yet covered, actions without an owner or a due date; balanced, at most 5 | Customer's needs, Questions still to ask, Next steps and owners |
| **Incident review** (`incident-review`) | Incident timeline (timeline), Contributing factors (decisions and open questions), Follow-up actions (blank, kept as an actions table) | Actions without an owner or a due date, things to clarify, topics not yet covered, questions to ask; balanced, at most 6 | Timeline so far, Gaps in the timeline, Actions and owners |
| **Retrospective** (`retrospective`) | Retrospective (went well, to improve, actions) | Actions without an owner or a due date, topics not yet covered, time checks; quiet, at most 4 | Themes so far, Actions and owners |

Fact checks and lookups run only where an administrator has turned them on
(**Meeting Assistant › Live guidance › Fact checks** and **Lookups**); otherwise
the Architecture review and Customer discovery playbooks run their other checks.

Like the default skills, a shipped playbook is offered for Owner use and not for
[Participant use](meeting-assistant-skills.md#participant-use-of-a-default-skill)
until an Admin allows it, so its instructions are given to the meeting page's own
chat and not to requests made in the meeting.

## Choosing a playbook

- **On the meeting page.** The **Meeting playbook** panel, on the Summary tab and,
  while the meeting is in progress, on the Transcript tab, says which playbook the
  meeting runs and what it turned on. Its owner and Admins choose one with
  **Choose a playbook** (or **Change**), which lists every playbook, marks the
  suggested ones with the reason, and shows what the selected one makes, turns on
  and may cost before **Apply**. **Remove** takes it off. A playbook can be applied
  only while the meeting is in progress; it can be removed at any time.
- **For a meeting a Virtual Participant records.** The Virtual Participant form
  (start, schedule, edit a scheduled one, and Join again) has a **Meeting
  playbook** choice: *The profile's default*, *No playbook*, or a playbook. It
  shows the suggestion for the meeting name and agenda with **Use it**, and what
  the chosen playbook does. The playbook is applied when the meeting starts. A
  playbook deleted after the Virtual Participant was scheduled is not applied, and
  the meeting page says so.
- **For every new meeting.** An Admin sets **Meeting Assistant › Meeting
  playbooks › Default playbook** on the Configuration page. It is applied to each
  new meeting that is not given one, from any capture source except an uploaded
  recording; a Virtual Participant given *No playbook* gets none. A meeting whose
  owner chose a playbook on the meeting page before the default was applied keeps
  the owner's choice.
- **Over MCP.** `schedule_meeting`, `start_meeting_now` and
  `update_scheduled_meeting` take `playbook`: a playbook's skill id, or `none`. See
  the [MCP Tools Reference](mcp-tools-reference.md).

## Suggestions

A playbook is suggested when its display name, or one of the phrases its
`suggestFor` lists, is in the meeting's name (for a meeting, its CallId) as whole
words, or in its agenda or invitation. Numbers are ignored, so "Sprint 42
planning" matches "sprint planning". A phrase in the name counts for more than
one in the agenda, and a longer phrase for more than a shorter one, so "Sprint 41
retrospective" suggests Retrospective rather than Sprint planning. Nothing is
suggested without a match. The suggestion costs nothing: it is a comparison of
words, not a model call.

## Applying, switching and removing

**Switching** to another playbook takes the first one off and applies the second.
The whiteboards the first one made are kept, unless **Delete the whiteboards …
made that nobody has written on** is ticked, which deletes those whose panels are
all still the assistant's; a whiteboard someone wrote on is always kept.

**Removing** a playbook puts live guidance and the whiteboard updates back to what
they were before the first playbook was applied to the meeting, takes its quick
actions out of the chat and its instructions out of the assistant's prompt, and
keeps its whiteboards unless asked to delete the untouched ones as above. A
switch the owner changed by hand after applying the playbook is set back too.

**What could not be applied is said, not hidden.** A part of a playbook that this
deployment cannot apply is left out and the rest applied, and the meeting records
a note for each, shown on the meeting page: a whiteboard over the meeting's
**Whiteboards per meeting**, a check turned off for the deployment, a template or
check a playbook names that this release does not know (one imported from a later
release, for example), or a playbook that no longer exists.

## What a playbook holds

A playbook is a skill whose front matter has a `playbook` section. The section is
kept in the skill's metadata, so the Skills page's editor, `SKILL.md`, the zip of
a skill folder and the skill library all carry it unchanged.

```markdown
---
name: launch-review
display_name: Launch review
description: Playbook for a product launch review. Use when asked about the launch plan, its risks or the go/no-go decision.
playbook:
  suggestFor:
    - launch review
    - go/no-go
  whiteboards:
    - template: risks
      title: Launch risks
      instructions: Each risk to the launch date, with its owner and mitigation.
      assistantUpdates: true
    - template: decisions
      title: Go/no-go
  guidance:
    checks: [risk_probe, action_missing_owner, action_missing_due_date, time_check]
    assertiveness: balanced
    maxItemsPer10Min: 5
  quickActions:
    - label: Go or no-go
      prompt: Summarise the arguments for and against launching on the planned date.
---

# Launch review

Instructions for the assistant in a launch review ...
```

| Field | Meaning | Rules |
|---|---|---|
| `suggestFor` | Phrases that suggest the playbook. The display name always does. | Up to 20, each up to 60 characters. |
| `whiteboards` | The whiteboards to make. | Up to 5. |
| `whiteboards[].template` | One of the [whiteboard templates](meeting-whiteboards.md#what-a-whiteboard-is): `decisions`, `process`, `architecture`, `comparison`, `timeline`, `gantt`, `user_stories`, `raci`, `risks`, `decision_log`, `retrospective`, `blank`. | Required. |
| `whiteboards[].title` | The whiteboard's title. | Up to 120 characters; the template's name when absent. |
| `whiteboards[].instructions` | The whiteboard's instructions for the assistant. | Up to 1,000 characters. |
| `whiteboards[].assistantUpdates` | Whether the assistant keeps it updated. | `true` (the default) or `false`. |
| `guidance.checks` | The live guidance types the meeting publishes: `ask_question`, `uncovered_topic`, `clarify`, `risk_probe`, `action_missing_owner`, `action_missing_due_date`, `time_check`, `fact_check`, `lookup`. | Without a `guidance` part, live guidance is left as it is. |
| `guidance.assertiveness` | `quiet`, `balanced` or `assertive`: the confidence an item needs, 0.85, 0.75 or 0.65, in place of the deployment's **Confidence threshold**. | `balanced` when absent. |
| `guidance.maxItemsPer10Min` | The most items in any 10 minutes. | 1 to 20; never more than the deployment's **Guidance items per 10 minutes**. |
| `quickActions` | Buttons in the meeting's assistant chat: `label` and the `prompt` it sends as the user's question. | Up to 6; a label up to 40 characters, a prompt up to 1,000. |

The digest (**Digest so far**) is made on request, and is not a check: a playbook
never turns it off. The skill's instructions, description and reference files
follow the [skill rules](meeting-assistant-skills.md#what-a-skill-holds).

A playbook is checked when it is saved or imported: a field the section does not
have, a template or check that does not exist, or a value out of range refuses
the save or the import, with every problem listed and the names it accepts. The
same checks run when a playbook is applied, where a part that fails them is left
out and noted (see above), so a playbook stored by another release applies what
it can.

## Writing a playbook

On the Skills page an Admin turns **This skill is a meeting playbook** on in the
skill editor and fills in its parts: the phrases that suggest it, its whiteboards
(each with a template, a title, instructions and whether the assistant keeps it
updated), its live guidance checks with their assertiveness and item rate, and
its quick actions. **Preview** shows what applying it does. The editor checks the
playbook as the server does and lists every problem before saving. The Skills
page marks each playbook in its **Meeting playbook** column.

A playbook can also be imported as a `SKILL.md` or a zip (**Import / export**),
exported, and moved between deployments with the skill library. See
[Importing and Exporting a Skill](meeting-assistant-skills.md#importing-and-exporting-a-skill).

Write the instructions as for any skill: what the people running this kind of
meeting want from it, and how to answer their requests. Name only tools the
meeting assistant has. The instructions are an administrator's guidance, held
to the assistant's fixed rules like every skill (see
[How the Assistant Uses a Skill](meeting-assistant-skills.md#how-the-assistant-uses-a-skill)):
they cannot change a fixed rule, cannot give the assistant a tool it is not
offered, and cannot make anything a meeting says into instructions. The
whiteboards' instructions are given to the whiteboard updates as quoted data,
like the rest of a whiteboard.

## Cost

Applying a playbook makes no model call. What it turns on does: each live
guidance run, each research run of a fact check or lookup, and each run of the
whiteboard updates is a model call billed to the meeting and shown in its
[cost estimate](meeting-cost-estimate.md), as without a playbook.

Before a playbook is applied, the meeting page and the Virtual Participant form
show an **estimate** of what it may cost: per meeting-hour for a meeting busy
enough for every run to happen, and per meeting at the deployment's limits (**Runs
per meeting at most** for live guidance and for the whiteboards, **research runs
per meeting at most** for fact checks and lookups). It is worked out from the
deployment's settings, the model each part uses, the shipped price table, and
the token use of a typical run: about 550 input, 1,800 cache-read, 800
cache-write and 1,400 output tokens for a live guidance run, about 4,000 input,
2,000 cache-read and 1,500 output tokens for a whiteboard run, and about 7,000
cache-read, 3,500 cache-write and 1,000 output tokens for a research run, as
[Live Guidance](live-guidance.md#cost) and [Meeting Whiteboards](meeting-whiteboards.md#cost)
give them. A model the price table does not list is shown as not priced.

With Claude Haiku 5.5 and the shipped settings, the Sprint planning playbook is
estimated at about $0.06 per meeting-hour and under $0.20 for a meeting;
with fact checks or lookups on, a playbook that names them adds about $0.02 at
most. These are estimates, not measurements of a recorded meeting; a quiet
meeting runs less often, and the meeting's own cost estimate shows what it used.

## Who can see and choose playbooks

- **Choosing, switching and removing** a meeting's playbook is for the meeting's
  **owner** and **Admins**, and for LMA's own backend (a Virtual Participant's
  choice, the profile's default). A user the meeting is shared with sees which
  playbook it runs and what that turned on, but cannot change it.
- **The list of playbooks and the suggestions** (`meetingPlaybookOptions`) are
  for every signed-in Admin and User; with a meeting named, only for someone who
  can see that meeting.
- **Participants** see no playbook controls and no playbook quick actions, and
  both operations refuse them. A Participant's own questions keep the
  [Participant use](meeting-assistant.md#participant-use) rules: a playbook's
  instructions reach them only when its skill is allowed for Participant use.
- **Writing playbooks** is for Admins, on the Skills page, as for every skill.

## API

| Operation | What it does |
|---|---|
| `meetingPlaybookOptions(CallId, Title, Agenda, Invitation)` | AWSJSON: `Playbooks` (each with `SkillId`, `DisplayName`, `Description`, `Whiteboards`, `Guidance`, `QuickActions`, `Notes` and `Estimate`), `Suggestions` (`SkillId`, `DisplayName`, `Reason`, `MatchedPhrase`, `MatchedIn`), `ProfileDefault`, `Current` (the meeting's playbook, when `CallId` is given), `Deployment` (what the deployment allows), `Templates` and `Checks`. |
| `applyMeetingPlaybook(input)` | `CallId`, `SkillId` (null or empty removes the playbook), `DeleteWhiteboards`. Returns the meeting, published to `onUpdateCall`. A backend caller also names `Origin` (`virtual-participant` or `profile-default`) and `UpdatedBy`. |
| `Call.Playbook` | `SkillId`, `Name`, `DisplayName`, `AppliedAt`, `AppliedBy`, `Origin` (`chosen`, `virtual-participant` or `profile-default`), `Guidance` (`Checks`, `Assertiveness`, `MaxItemsPer10Min`), `QuickActions` (`Label`, `Prompt`), `Whiteboards` (`WhiteboardId`, `Title`, `Template`, `Reused`), `Notes`, and `Previous` (the switches before the first playbook). For a playbook that could not be applied, `Requested` and `Notes` without a `SkillId`. Null when none is applied. `Call.PlaybookUpdatedAt` is when it last changed. |
| `VirtualParticipant.playbook` | A skill id or `none`; absent for the profile's default. Set through `createVirtualParticipant`, `updateScheduledVirtualParticipant` (null removes it) and `joinAgainVirtualParticipant`. The Virtual Participant sends it in its START event as `PlaybookId`, which the call event processor takes off the event and applies once the meeting exists. |

Refusals: `Unauthorized` (not the owner or an Admin, or a Participant),
`BadRequest` (no such playbook, or the meeting has ended),
`WhiteboardTranscriptDeleted`.

## Limits

| What | Limit |
|---|---|
| Whiteboards a playbook makes | 5, within the meeting's **Whiteboards per meeting** |
| Quick actions | 6; a label 40 characters, a prompt 1,000 |
| Phrases that suggest it | 20, each 60 characters |
| Items per 10 minutes | 1 to 20, and at most the deployment's |
| Playbooks | Within the 40 skills a deployment holds |

## Limitations

- **A playbook applies to a meeting in progress.** It cannot be applied to a
  meeting that has ended, and an uploaded recording is not given the profile's
  default.
- **A suggestion is a word match.** It does not read the meeting's content, and it
  can miss a meeting named in other words; add phrases to `suggestFor`.
- **The estimate is an estimate.** It assumes every run happens and a typical
  run's token use; the knowledge bases' own generation, web search and MCP
  servers' own charges are not in it, as they are not in the meeting's cost
  estimate.

## Related

- [Meeting Assistant Skills](meeting-assistant-skills.md)
- [Meeting Whiteboards](meeting-whiteboards.md)
- [Live Guidance](live-guidance.md)
- [Virtual Participant](virtual-participant.md)
- [Configuration Guide](configuration-guide.md)
- [MCP Tools Reference](mcp-tools-reference.md)
