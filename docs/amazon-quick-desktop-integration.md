---
title: "Amazon Quick Desktop: LMA Workflows"
---

# Amazon Quick Desktop: LMA Workflows

Once LMA is connected to Amazon Quick Desktop as an MCP server, the seven
LMA tools become first-class capabilities that Quick can compose with
**any other tool you've connected** — your calendar, Slack, task trackers,
the knowledge graph, web search, and other MCP servers. That composability
is what makes the integration powerful: it isn't a fixed feature set, it's
a building block.

This guide shows three layers of usage:

1. **[Conversational workflows](#conversational-workflows)** — ad-hoc
   prompts you can type into Quick Desktop chat *today*, with no setup
   beyond the MCP connection. These are the easiest way to get value.
2. **[Scheduled agent recipes](#scheduled-agent-recipes)** — agent
   configurations that run autonomously on a schedule or trigger
   (pre-meeting briefings, action-item extraction, KG enrichment).
3. **[The skills pack](#the-skills-pack)** — a packaged bundle of the
   most popular workflows, installable in one step.

> **First time setting up?** Connect LMA to Quick Desktop first — see
> [Amazon Quick MCP Setup → Path B](amazon-quick-mcp-setup.md#path-b-quick-desktop-api-key).
> Or, for the fastest install, use the
> [`amazon-quick-desktop-skills-pack/`](../amazon-quick-desktop-skills-pack/) bundle which
> ships agents and skills as a one-step install.

## Prerequisites

- LMA connected to Quick Desktop via MCP API key (see
  [Amazon Quick MCP Setup](amazon-quick-mcp-setup.md))
- Quick Desktop with calendar (Outlook or Google), and optionally Slack and a
  task tool (Asana, Jira) connected
- Verified that LMA tools work in chat — try
  `Search my LMA meetings for [a recent topic]` and confirm results

## Available LMA tools

LMA exposes seven tools, of which five are read-only (annotated
`readOnlyHint: true` so Quick Desktop classifies them correctly for scheduled
agents — no `allow_unscoped_write_tools` required for read-only flows):

- **Read-only:** `search_lma_meetings`, `get_meeting_transcript`,
  `get_meeting_summary`, `list_meetings`, `get_virtual_participant_status`
- **Write:** `schedule_meeting`, `start_meeting_now`

All responses include `meetingUrl` and (where applicable)
`virtualParticipantUrl` deep-links so agents can post clickable references
back into the LMA UI.

---

## Conversational workflows

Once LMA is connected, Quick Desktop will automatically pick the right tools
to satisfy your request — including chaining LMA with your **calendar,
Slack, task tracker, knowledge graph, web search,** and any other MCP
servers you've installed. You don't need to write an agent or a skill;
just ask.

The examples below all work as plain chat prompts. Each one shows which
tools Quick will typically reach for so you can see the composition.

### Join my next meeting

> "Send the LMA virtual participant to my next scheduled meeting."

Quick reads the next event from your calendar (Outlook/Google), pulls the
meeting URL from the invite body, and calls
`live_meeting_assistant_lma__start_meeting_now` with the right name and
URL — then polls `get_virtual_participant_status` until the VP is live and
reports back with the [VNC viewer link](virtual-participant.md#status-lifecycle).

Variants that work the same way:

> "Join my 2pm with the design team — name the participant 'Design Sync
> Notes'."
>
> "Schedule the LMA bot for every recurring 1:1 on my calendar this
> week."  *(uses `schedule_meeting`)*
>
> "If my next call is on Zoom and I have stored Zoom credentials, sign
> in — otherwise join as a guest."

### Pull action items from the last meeting

> "What did I commit to in my last meeting? Post the action items
> assigned to me into #my-todos in Slack."

Quick calls `list_meetings` to find the most recent one,
`get_meeting_summary` with `includeActionItems=true`, filters for items
owned by you, and posts to Slack with the LMA `meetingUrl` deep-link so
your team can click through to the source transcript.

Replace Slack with whatever you have connected:

> "...create Asana tasks in the *Q3 Launch* project for each action item
> assigned to me, with the meeting link in the description."
>
> "...add them as Jira tickets under epic LMA-42 and tag the owner."
>
> "...DM each owner in Slack with their items."

### Answer questions from past transcripts

LMA's transcripts are searchable knowledge in their own right.

> "Did we ever decide on the database vendor for Project Atlas? Search
> my LMA meetings and quote the relevant transcript line with the
> meeting link."

Quick calls `search_lma_meetings` with topical queries, then
`get_meeting_transcript` on the top hits to find the literal quote, and
returns a citation with `meetingUrl`.

More examples:

> "Find every meeting where Priya mentioned the budget cap and summarize
> her position over time."
>
> "What was the timeline we agreed with the vendor in last Thursday's
> review? Quote the transcript."
>
> "I'm about to email the customer — pull every commitment we've made to
> them across our last five calls."

### Post a recap to Slack / email / Confluence

> "Summarize this morning's all-hands and post it to #general with the
> top three decisions and any unresolved questions."

Quick locates the meeting via `list_meetings`, calls `get_meeting_summary`,
shapes the output for Slack, and posts. Works the same with email,
Confluence, Notion, or any other connected destination.

> "Email the recap of today's customer call to <person@example.com> with
> action items as a checklist and the LMA recording link at the top."
>
> "Append the decisions from this week's design reviews to the *Design
> Decisions* page in Confluence."

### Pre-meeting prep on demand

You don't need a scheduled agent to get a brief — just ask before the
meeting:

> "I have a call with Acme Corp in 30 minutes. Pull every prior LMA
> meeting with anyone from acme.com, summarize what we last discussed,
> and list any open action items."

Quick uses your calendar to identify attendees, runs
`search_lma_meetings`, calls `get_meeting_summary` on the top results,
and produces a brief in the chat — with one-click `meetingUrl` links to
each source meeting.

### Cross-tool synthesis

The real win is when LMA is just one source among many:

> "Cross-reference the action items from yesterday's standup with the
> open Jira tickets in sprint 42. Anything we committed to but haven't
> ticketed yet?"
>
> "From the last four customer calls, extract every feature request and
> match it against open issues in our GitHub repo. Report any gaps."
>
> "Look at the action items from this morning's exec sync and check
> Slack #leadership for any follow-up discussion in the last 24h.
> Summarize the current state of each item."

These prompts blend `search_lma_meetings` /
`get_meeting_summary` with Jira, GitHub, and Slack tools — Quick handles
the orchestration.

### Tip: use the deep-links

Every LMA tool response includes a `meetingUrl` (and
`virtualParticipantUrl` for VP launches). When you ask Quick to post,
email, or DM, tell it to **include the link** — that turns every output
into a clickable reference back into the LMA UI for the full transcript,
recording, or live VP viewer.

---

## Scheduled agent recipes

The patterns above are great for ad-hoc use. When you want LMA workflows
to run *automatically* on a trigger or schedule — without you typing a
prompt — define them as Quick Desktop **agents**. The recipes below are
ready-to-paste configurations.

### Recipe 1: Pre-Meeting Briefing Agent

Runs every 5 minutes; fires 15 minutes before any calendar event. Searches
LMA for prior meetings with the same attendees, pulls summaries and action
items, cross-references the knowledge graph, and posts a concise brief to
your activity feed.

#### Configuration

```
Agent ID: lma-pre-meeting-brief
Model: smart
Schedule: interval (every 5 minutes)
```

**Tool policy:**

```json
[
  {"group": "outlook_builtin"},
  {"group": "user_mcp__live_meeting_assistant_lma"},
  {"group": "kg_tools"},
  {"effect": "allow", "tool": "live_meeting_assistant_lma__search_lma_meetings"},
  {"effect": "allow", "tool": "live_meeting_assistant_lma__get_meeting_summary"},
  {"effect": "allow", "tool": "live_meeting_assistant_lma__list_meetings"},
  {"effect": "allow", "tool": "live_meeting_assistant_lma__get_meeting_transcript"}
]
```

**Trigger:**

```
Template: on_upcoming_event
Trigger ID: upcoming-meeting
Parameters: {"minutes_ahead": 15, "include_all_day": false}
```

**Agent prompt:**

```
You are a pre-meeting briefing agent. Your job is to prepare concise, actionable
briefings before upcoming meetings by combining calendar context with LMA
meeting history.

## When triggered

You receive context about an upcoming calendar event (subject, attendees,
time). Your task:

1. Identify the meeting participants from the trigger context.

2. Search LMA for prior meetings with those participants:
   - Call `live_meeting_assistant_lma__search_lma_meetings` with queries
     like the participant names or the meeting subject/topic.
   - If results are found, call `live_meeting_assistant_lma__get_meeting_summary`
     on the most recent 1–2 relevant meetings to get summaries and action
     items.

3. Check the knowledge graph for relationship context about the attendees
   (recent interactions, commitments, project associations) using `kg_search`.

4. Synthesize a brief that includes:
   - Meeting: subject, time, attendees
   - Last interaction: when you last met with these people and key topics
   - Open action items: any outstanding commitments from prior meetings
   - Context: relevant relationship/project notes from the knowledge graph
   - Preparation suggestions: what the user might want to review or prepare

5. Post the brief to the activity feed using `update_feed` with:
   - importance: "important" (so it shows a toast notification)
   - A clear, scannable format with bullet points
   - Include the LMA meetingUrl from each referenced meeting so the user can
     jump to the full transcript with one click.

## Guidelines
- Keep briefs concise — aim for 3–5 bullet points, not a wall of text.
- If no prior LMA meetings are found with the attendees, say so and still
  provide any KG context.
- Skip trivial/recurring meetings with no meaningful history.
- Never fabricate information — only report what you actually find in LMA
  and the knowledge graph.
- Do NOT call schedule_meeting or start_meeting_now — you are read-only.
```

#### Test, then enable

```
Trigger the lma-pre-meeting-brief agent
```

Once you're satisfied with the output:

```
Enable the lma-pre-meeting-brief agent
```

The agent will now run every 5 minutes, checking for upcoming meetings and
generating briefs when one is 15 minutes away.

---

### Recipe 2: Action Item Tracker

Runs hourly. Extracts action items from recently completed meetings and posts
them to your activity feed (or optionally creates Asana/Jira tasks).

#### Configuration

```
Agent ID: lma-action-items
Model: smart
Schedule: interval (every 60 minutes)
```

**Tool policy:**

```json
[
  {"group": "outlook_builtin"},
  {"group": "user_mcp__live_meeting_assistant_lma"},
  {"group": "kg_tools"},
  {"effect": "allow", "tool": "live_meeting_assistant_lma__search_lma_meetings"},
  {"effect": "allow", "tool": "live_meeting_assistant_lma__get_meeting_summary"},
  {"effect": "allow", "tool": "live_meeting_assistant_lma__list_meetings"}
]
```

**Agent prompt:**

```
You are an action item extraction agent. Your job is to find newly completed
meetings and extract actionable commitments.

## Process

1. Call `live_meeting_assistant_lma__list_meetings` to find meetings completed
   in the last 2 hours (use startDate/endDate filters).

2. For each meeting found, call `live_meeting_assistant_lma__get_meeting_summary`
   with includeActionItems=true.

3. Extract action items. For each:
   - Identify the owner (who committed to doing it)
   - Identify the action (what they committed to)
   - Note any deadline mentioned
   - Note the source meeting (use the meetingUrl from the response so the
     user can jump to context)

4. Post a summary to the activity feed using `update_feed` with
   importance="important":
   - Group action items by owner
   - Include the meeting name, date, and meetingUrl for each
   - Flag any items assigned to the user specifically

## Guidelines
- If no new meetings are found, call skip_cycle (nothing to report).
- Focus on concrete, actionable commitments — not vague discussion points.
- Never fabricate action items — only report what's explicitly in the summary.
```

#### Optional: custom trigger to skip empty cycles

For efficiency, add a custom code trigger that only fires when new meetings
exist since the last check:

```python
def check(params, tools, state):
    """Check for newly completed LMA meetings."""
    from datetime import datetime, timedelta, timezone

    list_meetings = tools.get("live_meeting_assistant_lma__list_meetings")
    if not list_meetings:
        return False

    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).isoformat()
    end = now.isoformat()

    result = list_meetings(startDate=start, endDate=end)
    if not result:
        return False

    meetings = result.get("meetings", []) if isinstance(result, dict) else []
    if not meetings:
        return False

    seen_ids = set(state.get("processed_meeting_ids", []))
    new_meetings = [m for m in meetings if m.get("CallId") not in seen_ids]

    if not new_meetings:
        return False

    all_ids = list(seen_ids | {m.get("CallId") for m in new_meetings})
    state["processed_meeting_ids"] = all_ids[-100:]

    return {
        "new_meetings": [
            {"id": m.get("CallId"), "name": m.get("Subject", "Unknown")}
            for m in new_meetings
        ],
        "count": len(new_meetings),
    }
```

---

---

## The skills pack

The [`amazon-quick-desktop-skills-pack/`](../amazon-quick-desktop-skills-pack/)
bundle wraps the most popular workflows as installable Quick Desktop
**skills** (one-line trigger phrases) and **agents** (the scheduled
recipes above, pre-configured). Install the pack and you get:

- **`lma-meeting-catchup`** — *"catch me up on the standup I missed this
  morning"*. Finds the meeting from your calendar, pulls the LMA summary
  and transcript, optionally cross-references Slack threads from the same
  window, and synthesizes a personalized catch-up with deep-links.
- **`lma-live-coach`** — *"coach me on this call"*. Launches the LMA
  virtual participant into your current meeting and spawns a background
  agent that polls the live transcript and posts coaching cards
  (MEDDPICC, SPIN, Challenger) to your activity feed in real time.
  Inspired by [KenAI Live Call Coach](https://kenbeau.people.aws.dev/)
  by Ken Beauvais.
- **`lma-pre-meeting-brief`** and **`lma-action-items`** agents —
  pre-configured versions of Recipes 1 and 2 above.

Skills are just packaged versions of conversational workflows. Anything
in the skills pack you can also do ad-hoc with a chat prompt (see
[Conversational workflows](#conversational-workflows)) — the pack just
gives you a short trigger phrase and a curated prompt.

---

## Troubleshooting

### "Tool not found" error

Verify the MCP connection is active:
- **Settings → Capabilities → MCP** → confirm LMA shows **Connected**
- If disconnected, click the connection and re-enter the API key

### "Permission denied" on meeting data

LMA enforces UBAC — non-admin users only see their own meetings:
- Verify you're using your own API key (not a shared one)
- For team-wide agents, ask your LMA admin for an admin-scoped key

### Agent triggers but finds no meetings

- Confirm meeting history exists in the LMA web UI
- The pre-meeting agent searches based on attendee names — make sure meeting
  participants in LMA match your calendar invitees

### Agent doesn't trigger before meetings

- Verify the trigger config:
  `Show me the lma-pre-meeting-brief agent config`
- Check that the schedule interval (5 min) is short enough to catch the
  15-min trigger window
- Confirm Outlook/Google calendar is connected in
  **Settings → Capabilities → Connections**

### Write-tool prompts asking for `allow_unscoped_write_tools`

LMA's read-only tools (`search_lma_meetings`, `get_meeting_transcript`,
`get_meeting_summary`, `list_meetings`, `get_virtual_participant_status`)
are annotated `readOnlyHint: true` and should not require this flag.

If you're seeing the prompt, it's because your tool policy includes a
**write tool** (`schedule_meeting` or `start_meeting_now`). For read-only
agents like the pre-meeting brief and action item tracker, exclude those
two tools from the policy and the prompt should disappear.

### General connection / authentication issues

See the troubleshooting section in
[Amazon Quick MCP Setup](amazon-quick-mcp-setup.md#troubleshooting).

---

## Reference

- [Amazon Quick MCP Setup](amazon-quick-mcp-setup.md) — connect LMA to
  Quick Suite or Quick Desktop
- [`amazon-quick-desktop-skills-pack/`](../amazon-quick-desktop-skills-pack/) — installable
  bundle of these agents and skills
- [MCP Servers Overview](mcp-servers.md)
- [MCP API Key Authentication](mcp-api-key-auth.md)
