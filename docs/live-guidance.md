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
- [Fact checks and lookups](#fact-checks-and-lookups)
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
| `fact_check` | A factual claim made in the meeting that the deployment's sources contradict ("Possibly inaccurate") or materially qualify ("Worth checking"). Off until an administrator turns it on; see [Fact checks and lookups](#fact-checks-and-lookups). |
| `lookup` | A product, acronym, ticket, document, internal term or role the people in the meeting seem not to know, answered from the deployment's sources. Off until an administrator turns it on. |

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

A run reads the segments said since the last run. It also reads a segment that
ended up to two minutes before the last run's latest segment but was finalized
after that run, such as a long utterance on another channel; the meeting's
guidance state keeps the segments each run saw near its end, so no segment is
read twice.

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

## Fact checks and lookups

Two kinds of guidance look things up rather than reading the meeting alone.
Both are **off** until an administrator turns them on (**Fact checks** and
**Lookups** in [Settings](#settings)), and they run only on a meeting that has
live guidance on.

- A **fact check** (`fact_check`) takes a specific factual claim made in the
  meeting (a number, price, date, version or limit, or what a product does or
  does not do) and checks it against the deployment's sources. It is published
  only when the sources contradict the claim (**Possibly inaccurate**, severity
  warning) or materially qualify it (**Worth checking**, severity suggestion). A
  claim the sources agree with is checked but not shown, unless **Fact checks:
  also show claims the sources support** is on; a claim they do not settle is
  never shown.
- A **lookup** (`lookup`) takes a product, acronym, ticket or issue id,
  document, internal term or role that the people in the meeting seem not to
  know (someone asks what it is), or that an open item depends on, and answers
  it in a short paragraph from the sources (**Lookup: *term***, severity info).

Each item quotes what was said, with the speaker and the time; gives what the
sources say; names up to three sources, each a link where the source has one
(a web page, a web-crawled document) and its title otherwise (a document in an
S3 data source), with where it was found (Documents, Past meetings, Web, or the
MCP server's name); and gives the confidence. It has a **Copy** button that
copies the finding and its sources. It is a guidance item like the others: in
the Notifications drawer, for the meeting's owner, the users it is shared with
and Admins only, never posted to the meeting chat and never spoken.

### Where it looks

A check uses only the sources the deployment has and the settings allow, each
through the meeting assistant's own tool, acting for the meeting's owner:

| Source | Setting (on by default) | The tool |
|---|---|---|
| The document knowledge base | **Fact checks and lookups: document knowledge base** | `document_search`, where the stack has a document knowledge base (a `STRANDS_BEDROCK_WITH_KB` mode) |
| The owner's past meetings | **Fact checks and lookups: the owner's past meetings** | `meeting_history`, which searches only the meetings the meeting's owner can see |
| The web | **Fact checks and lookups: web search** | `web_search` (Tavily), where the stack has a Tavily API key and **Meeting Assistant › Web search** is on |
| MCP servers | **Fact checks and lookups: MCP tools** (empty by default) | The installed MCP servers' tools named in the list, as below |

An MCP tool is used only when the **MCP tools** list names it, as
`<server>/<tool>` (the server's name or its id, then the tool's name as the
server gives it), or as `<server>/*`, which covers only that server's tools that
declare themselves read-only (the MCP `readOnlyHint` annotation). A tool whose
annotations say it changes anything (`readOnlyHint: false`, or
`destructiveHint: true`) is never used, whatever the list says. A server whose
**Owner use** is off in **Meeting Assistant › Tool selection** is not used, and
none is when **Meeting Assistant › MCP servers** is off. A check acts for the
meeting's owner, and its result reaches only the people who can see the
meeting's guidance, so it follows the servers' **Owner use** setting rather
than **Participant use**. No tool that reads the current meeting, starts a
Virtual Participant action or loads a skill is offered to a check.

The claim, or the term, and the few lines of transcript around it are what a
check works from, and it writes its own search queries from them; a query sent
to web search goes to Tavily, and one sent to an MCP tool goes to that server.
A deployment with no source at all makes no check: the meeting assistant
answers that it has nothing to search without calling a model, and live
guidance stops asking for candidates for 15 minutes.

### How a check runs

1. **Finding candidates.** A live guidance run that has either type on is also
   asked, in the same model call, for at most five candidates: each with its
   kind, a short subject, the claim restated in one sentence (or the term), the
   segment it was said in and its exact words. A candidate is kept only when
   its quote is in that segment and its confidence reaches **Confidence
   threshold** (the one the rest of the guidance uses). The run is shown the
   candidates already checked or waiting, so it does not raise them again.
2. **Waiting.** Kept candidates join the meeting's queue, which holds at most
   ten. A candidate whose subject, or whose wording, matches one already
   checked or waiting is not added: a repeated claim is checked once.
3. **Researching.** After the run, the same invocation researches the queue,
   oldest first, three candidates at a time, under a lease of its own: one
   research run at a time for each meeting, the first at least 20 seconds
   after the last. It goes on with the next three while the invocation has
   time for another research run and the meeting has research runs left, so a
   run that found five candidates has them all researched before it finishes,
   whether or not anyone speaks again. Each research run invokes the meeting
   assistant function, which runs one agent over the batch with only the tools
   above, at most **searches per run** tool calls (6 by default) and **seconds
   per run** (60 by default); past either, no further call starts and the agent
   reports what it found. The agent's instructions are fixed and no setting
   changes them. What the invocation has no time for waits for the next run.
4. **At the meeting's end.** When the meeting ends, one more run is queued, as
   the whiteboards' last run is. Runs follow speech, so a meeting's last minutes
   may never have been read: this run reads them, in one model call that asks
   for candidates only (no other guidance is published once the meeting is
   over), and then researches whatever waits. Its findings are published as
   usual, so the drawer holds them for the people reviewing the meeting. It
   makes no model call when nothing is left to read or research.
5. **Publishing.** A finding is published when its verdict is one that is
   shown, its confidence reaches **Fact checks and lookups: confidence
   threshold** (0.75 by default), and it cites at least one source the tools
   returned. It counts toward the same **Guidance items per 10 minutes** as the
   rest of the guidance. Its dedup key is `fact_check:claim:<subject>` or
   `lookup:term:<subject>`, and it refers to the segment it quotes
   (`segment:<segmentId>`). Every candidate in a batch is recorded as checked,
   published or not, so a check that failed is not repeated.

A candidate that is not researched is never dropped silently. The meeting's
guidance state keeps a log of the last 50, each with its kind, subject and
reason: its quote is not in its segment, its confidence is below the threshold,
it is over a run's five, it repeats one already checked or waiting (by subject
or by wording), the meeting's **claims and terms per meeting** are used, the
queue is full, or the meeting's **research runs per meeting** are used (what
still waits then is dropped). The analyzer's log records how many were queued,
deferred and dropped for each reason, never the claims themselves.

A fact check usually appears a minute or two after the claim: up to one interval
for the run that finds it (90 seconds by default, once enough has been said),
then the research, which took about 5 to 10 seconds a batch on Claude Haiku
5.5 when measured with document search.

Nothing is checked or published once the meeting's transcript has been deleted
or live guidance has been turned off for it. Once the meeting has ended, only
its run at the end researches and publishes.

### What a check trusts

Everything a source returns (a document, a web page, a past meeting, an MCP
server's result) is quoted data for the check, never instructions. Each result
is given to the agent inside a marked envelope that says so, and its sources
are listed after it with short ids; a finding cites those ids, so every link
or title in a published item is one a tool actually returned. A source whose
text addresses the model reading it ("ignore your previous instructions", a
note to "any AI assistant") is shown to the agent as data but given no id, so
it can never be a finding's source: for web search each page is judged on its
own, for the knowledge bases each cited document, and for an MCP tool the whole
result. The finding's own words are published as plain text: a link or
formatting in them is removed, and the only links in the item are its sources'.

### Dismissing one

**Dismiss** hides the item for you, as for any notification. A dismissed fact
check or lookup is also never published again for the meeting, and later runs
are told it was dismissed, so they do not raise one like it. The other people
who can see the meeting still see the item until they dismiss it.

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
| Fact checks (`factChecksEnabled`) | off | Check factual claims against the sources below; see [Fact checks and lookups](#fact-checks-and-lookups). |
| Lookups (`lookupsEnabled`) | off | Answer terms the meeting seems not to know from the sources below. |
| Fact checks: also show claims the sources support (`factChecksShowSupported`) | off | On, a claim the sources agree with is published too, as **Consistent with sources** (severity info). |
| Fact checks and lookups: document knowledge base (`researchDocuments`) | on | Search the document knowledge base, where the stack has one. |
| Fact checks and lookups: the owner's past meetings (`researchPastMeetings`) | on | Search the past meetings the meeting's owner can see. |
| Fact checks and lookups: web search (`researchWebSearch`) | on | Search the web, where web search is configured and on. |
| Fact checks and lookups: MCP tools (`researchMcpTools`) | none | The MCP tools a check may use, as `<server>/<tool>` or `<server>/*`, at most 50. |
| Fact checks and lookups: confidence threshold (`researchConfidenceThreshold`) | 0.75 | A finding below it is not published. Higher is quieter. |
| Fact checks and lookups: claims and terms per meeting at most (`researchChecksPerMeeting`) | 30 | The most candidates one meeting has researched (0 to 200). 0 turns both types off. |
| Fact checks and lookups: research runs per meeting at most (`researchRunsPerMeeting`) | 20 | The most research runs one meeting has, each of up to three candidates (0 to 200). 0 turns both types off. |
| Fact checks and lookups: searches per run at most (`researchToolCallsPerRun`) | 6 | The most tool calls one research run makes (1 to 20). |
| Fact checks and lookups: seconds per run at most (`researchSecondsPerRun`) | 60 | How long one research run may take (20 to 120); it reports what it found by then. |

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

### The cost of fact checks and lookups

With either type on, each run's model call also asks for candidates; that is
part of the run and of its **Live guidance** line. Measured on Claude Haiku 5.5
over a 14-turn meeting with seven checkable claims, it added about 700 input
tokens (the added instructions, which later runs read from the prompt cache)
and 150 to 340 output tokens a run, about $0.0002 a run at most.

Each research run is billed to the meeting as its own line, **Live guidance fact
checks and lookups** (context `GuidanceResearch`): the tokens of every model
call the research agent made, recorded by the meeting assistant function. It
uses the live guidance model (**Model** above, else the Meeting assistant
model), at a low reasoning effort where the model takes one, with prompt
caching as **Meeting Assistant › Use Amazon Bedrock prompt caching** says.
Measured on Claude Haiku 5.5 with the shipped price table, a research run of
three candidates with three document searches took about 1,000 output, 7,000
cache-read and 3,500 cache-write tokens, about $0.001; at the default cap of 20
research runs that is about $0.02 for a meeting. Measured by running the
research agent against Amazon Bedrock with stand-in tools, not on a deployed
stack.

Not in that line, as for the meeting assistant's own answers (see
[What is not included](meeting-cost-estimate.md#what-is-not-included)): the
knowledge bases' own generation for each document or past-meeting search, which
Amazon Bedrock does not report per call, Tavily's charge for each web search,
and whatever an MCP server's own service charges. Lower **searches per run at
most** or **research runs per meeting at most** to bound them.

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
between runs (its summary of the meeting so far, a record of what it
published, and the fact checks and lookups waiting or already checked, which
hold the claims and terms) is stored with the meeting under `gd#<CallId>`, expires with the
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
- **A fact check is only as good as its sources.** It says what the sources
  say, which may be out of date or about a different version or plan than the
  one the meeting meant; the item names its sources so that a reader can judge.
  A claim the sources do not cover is never shown, so silence does not mean a
  claim was right. Where none of the four sources is available, nothing is
  checked; where only the owner's past meetings are (a stack with neither a
  document knowledge base nor web search), mostly claims made in earlier
  meetings can be checked.
- **A check is not instant.** It waits for the run that finds the claim and
  then for its research, usually a minute or two in all.

## Related

- [Meeting Notifications](meeting-notifications.md)
- [Meeting Context, Labels, Audience and Checklist](meeting-context-and-checklist.md)
- [Meeting Assistant](meeting-assistant.md)
- [Meeting Cost Estimate](meeting-cost-estimate.md)
- [MCP Tools Reference](mcp-tools-reference.md)
