---
title: "Amazon Quick Desktop: Agent Recipes"
---

# Amazon Quick Desktop: Agent Recipes

Once LMA is connected to Amazon Quick Desktop as an MCP server, you can build
**scheduled agents** that act on meeting data autonomously — pre-meeting
briefings, action item extraction, knowledge graph enrichment, and more. This
guide provides ready-to-use configurations and prompts for the most useful
patterns.

> **First time setting up?** Connect LMA to Quick Desktop first — see
> [Amazon Quick MCP Setup → Path B](amazon-quick-mcp-setup.md#path-b-quick-desktop-api-key).
> Or, for the fastest install, use the
> [`amazon-quick-desktop-skills-pack/`](../amazon-quick-desktop-skills-pack/) bundle which
> ships these agents and skills as a one-step install.

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

## Recipe 1: Pre-Meeting Briefing Agent

Runs every 5 minutes; fires 15 minutes before any calendar event. Searches
LMA for prior meetings with the same attendees, pulls summaries and action
items, cross-references the knowledge graph, and posts a concise brief to
your activity feed.

### Configuration

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

### Test, then enable

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

## Recipe 2: Action Item Tracker

Runs hourly. Extracts action items from recently completed meetings and posts
them to your activity feed (or optionally creates Asana/Jira tasks).

### Configuration

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

### Optional: custom trigger to skip empty cycles

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

## Recipe 3: Async Meeting Catch-up (Chat-Based)

This works immediately with the LMA MCP connection — no scheduled agent
needed. Just ask:

```
Catch me up on the standup I missed this morning
```

Quick Desktop will:
1. Find the meeting from your calendar
2. Call `get_meeting_summary` and/or `get_meeting_transcript`
3. Cross-reference Slack threads from the same time window for post-meeting
   discussion
4. Synthesize a personalized catch-up with the LMA `meetingUrl` so you can
   drill into the full transcript or recording if you want

Package this as a reusable skill via the
[`amazon-quick-desktop-skills-pack/`](../amazon-quick-desktop-skills-pack/)
(`lma-meeting-catchup`) for one-line invocation.

---

## Recipe 4: Knowledge Graph Enrichment

Ask Quick Desktop to enrich your knowledge graph from meetings:

```
Search my LMA meetings from the past week and update my knowledge graph
with the people I met, topics discussed, and any commitments made.
```

This works ad-hoc; you can also wrap it into a scheduled agent that runs
nightly to keep the graph current.

---

## Recipe 5: Live Call Coaching

Launches LMA's virtual participant into your active meeting and spawns a
background agent that polls the live transcript and posts coaching cards
(MEDDPICC, SPIN, Challenger) to your activity feed in real time.

This pattern is shipped as the `lma-live-coach` skill in the
[skills pack](../amazon-quick-desktop-skills-pack/). Trigger phrase:
`coach me on this call`.

Inspired by [KenAI Live Call Coach](https://kenbeau.people.aws.dev/) by
Ken Beauvais.

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
