---
title: "Amazon Quick Desktop Integration"
---

# Amazon Quick Desktop Integration Guide

## Overview

This guide documents how to integrate LMA with **Amazon Quick Desktop** — a native macOS/Windows AI work companion that provides conversational AI, scheduled agents, a personal knowledge graph, and long-term memory. The integration uses LMA's MCP server to give Quick Desktop access to meeting transcripts, summaries, and virtual participant controls.

Once connected, you can:
- Search meeting transcripts conversationally ("What did we discuss about the roadmap last week?")
- Build scheduled agents that act on meeting data autonomously (pre-meeting briefings, action item extraction)
- Enrich Quick's personal knowledge graph with meeting context
- Schedule and start LMA virtual participants directly from chat

## Prerequisites

- LMA deployed with MCP Server enabled (v0.2.23 or later, `EnableMCP=true`)
- Amazon Quick Desktop installed (macOS or Windows)
- An LMA API key generated from the MCP Servers Configuration page (see [MCP API Key Authentication](mcp-api-key-auth.md))

## Step 1: Connect LMA to Amazon Quick Desktop

### 1.1 Generate an LMA API Key

1. Open your LMA web UI
2. Navigate to **Settings** → **MCP Servers Configuration** (admin access required)
3. In the **Hosted MCP Access** tab, click **Generate API Key**
4. Copy the key (format: `lma_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) — it's shown once only
5. Note the **MCP API Endpoint URL** displayed on the same page

### 1.2 Add LMA as an MCP Server in Quick Desktop

1. Open **Amazon Quick Desktop**
2. Navigate to **Settings** → **Capabilities** → **MCP**
3. Click **"+ Add MCP / Skill"**
4. Configure:
   - **Name**: `Live Meeting Assistant (LMA)`
   - **Endpoint URL**: Paste your MCP API Endpoint URL (from Step 1.1)
   - **Authentication**: Bearer Token
   - **Token**: Paste your API key from Step 1.1
5. Click **Connect**
6. Quick Desktop will discover the 6 available tools and confirm the connection

### 1.3 Verify the Connection

In a new Quick Desktop conversation, type:

```
Search my LMA meetings for discussions about [any recent topic]
```

Quick should call `search_lma_meetings` and return results. If it works, you're connected.

## Step 2: Basic Usage (Chat)

Once connected, you can ask Quick Desktop questions about your meetings naturally:

| Query | What happens |
|---|---|
| "What meetings did I have last week?" | Calls `list_meetings` with date filters |
| "Find meetings where we discussed pricing" | Calls `search_lma_meetings` with semantic search |
| "Summarize yesterday's standup" | Calls `list_meetings` to find it, then `get_meeting_summary` |
| "What action items came out of the Acme call?" | Calls `search_lma_meetings` + `get_meeting_summary` with `includeActionItems=true` |
| "Get the full transcript of meeting XYZ" | Calls `get_meeting_transcript` |
| "Schedule LMA to join my 2pm Zoom tomorrow" | Calls `schedule_meeting` (write operation) |

## Step 3: Build a Pre-Meeting Briefing Agent

This scheduled agent runs every 5 minutes and fires 15 minutes before any calendar event. It searches LMA for prior meetings with the same attendees, pulls summaries and action items, cross-references the knowledge graph, and posts a concise brief to your activity feed.

### 3.1 Create the Agent

In Amazon Quick Desktop, ask:

```
Create a scheduled agent called "lma-pre-meeting-brief" that:
- Triggers 15 minutes before calendar events
- Searches LMA for prior meetings with the same attendees
- Gets summaries and action items from recent meetings with those people
- Checks the knowledge graph for relationship context
- Posts a concise briefing to my activity feed with importance "important"
- Runs every 5 minutes to check for upcoming events
- Uses the Smart model
- Has access to: Outlook calendar, LMA MCP tools, knowledge graph
```

Or create it programmatically with these parameters:

**Agent Configuration:**

```
Agent ID: lma-pre-meeting-brief
Model: smart
Schedule: interval (every 5 minutes)
```

**Tool Policy:**

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

> **Note**: LMA MCP tools are classified as "write" because the MCP server doesn't set `readOnlyHint`. In practice, `search_lma_meetings`, `get_meeting_summary`, `list_meetings`, and `get_meeting_transcript` are read-only operations. You'll need to confirm unscoped write access (`allow_unscoped_write_tools=true`) when creating the agent.

**Trigger (Condition):**

```
Template: on_upcoming_event
Trigger ID: upcoming-meeting
Parameters: {"minutes_ahead": 15, "include_all_day": false}
```

**Agent Prompt:**

```
You are a pre-meeting briefing agent. Your job is to prepare concise, actionable
briefings before upcoming meetings by combining calendar context with LMA meeting history.

## When triggered

You receive context about an upcoming calendar event (subject, attendees, time). Your task:

1. Identify the meeting participants from the trigger context (attendee names/emails).

2. Search LMA for prior meetings with those participants:
   - Call `live_meeting_assistant_lma__search_lma_meetings` with queries like the
     participant names, or the meeting subject/topic.
   - If results are found, call `live_meeting_assistant_lma__get_meeting_summary`
     on the most recent 1-2 relevant meetings to get summaries and action items.

3. Check the knowledge graph for any relationship context about the attendees
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
   - Include any LMA meeting IDs so the user can look them up if needed

## Guidelines
- Keep briefs concise — aim for 3-5 bullet points, not a wall of text.
- If no prior LMA meetings are found with the attendees, say so and still provide
  any KG context.
- Skip trivial/recurring meetings with no meaningful history.
- Never fabricate information — only report what you actually find in LMA and the
  knowledge graph.
- Do NOT call schedule_meeting or start_meeting_now — you are read-only.
```

### 3.2 Test the Agent

Before enabling, do a manual test run:

```
Trigger the lma-pre-meeting-brief agent
```

Or wait until you have a meeting within 15 minutes and the condition will fire automatically.

### 3.3 Enable the Agent

Once satisfied with the test:

```
Enable the lma-pre-meeting-brief agent
```

The agent will now run every 5 minutes, checking for upcoming meetings and generating briefs when one is 15 minutes away.

## Step 4: Build an Action Item Agent (Phase 2)

This agent runs hourly and extracts action items from recently completed meetings, then posts them to your activity feed (or optionally creates Asana tasks).

### 4.1 Create the Agent

```
Agent ID: lma-action-items
Model: smart
Schedule: interval (every 60 minutes)
```

**Tool Policy:**

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

**Agent Prompt:**

```
You are an action item extraction agent. Your job is to find newly completed meetings
and extract actionable commitments.

## Process

1. Call `live_meeting_assistant_lma__list_meetings` to find meetings completed in
   the last 2 hours (use startDate/endDate filters).

2. For each meeting found, call `live_meeting_assistant_lma__get_meeting_summary`
   with includeActionItems=true.

3. Extract action items. For each:
   - Identify the owner (who committed to doing it)
   - Identify the action (what they committed to)
   - Note any deadline mentioned
   - Note the source meeting

4. Post a summary to the activity feed using `update_feed` with importance="important":
   - Group action items by owner
   - Include the meeting name and date for each
   - Flag any items assigned to the user specifically

## Guidelines
- If no new meetings are found, call skip_cycle (nothing to report).
- Focus on concrete, actionable commitments — not vague discussion points.
- Never fabricate action items — only report what's explicitly in the summary.
```

### 4.2 Add a Custom Trigger (Optional)

For efficiency, you can add a custom code trigger that only fires when new meetings exist since the last check:

```python
def check(params, tools, state):
    """Check for newly completed LMA meetings."""
    from datetime import datetime, timedelta, timezone
    
    list_meetings = tools.get("live_meeting_assistant_lma__list_meetings")
    if not list_meetings:
        return False
    
    # Check for meetings in the last 2 hours
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).isoformat()
    end = now.isoformat()
    
    result = list_meetings(startDate=start, endDate=end)
    if not result:
        return False
    
    meetings = result.get("meetings", []) if isinstance(result, dict) else []
    if not meetings:
        return False
    
    # Filter out meetings we've already processed
    seen_ids = set(state.get("processed_meeting_ids", []))
    new_meetings = [m for m in meetings if m.get("CallId") not in seen_ids]
    
    if not new_meetings:
        return False
    
    # Update state with newly seen IDs (keep last 100)
    all_ids = list(seen_ids | {m.get("CallId") for m in new_meetings})
    state["processed_meeting_ids"] = all_ids[-100:]
    
    return {
        "new_meetings": [{"id": m.get("CallId"), "name": m.get("Subject", "Unknown")} 
                         for m in new_meetings],
        "count": len(new_meetings)
    }
```

## Step 5: Async Meeting Catch-up (Chat-Based)

This doesn't require a scheduled agent — it works immediately with the LMA MCP connection. Simply ask:

```
Catch me up on the standup I missed this morning
```

Quick Desktop will:
1. Check your calendar for today's standup
2. Call `get_meeting_summary` for that meeting
3. Cross-reference Slack for post-meeting discussion
4. Give you a synthesized catch-up

## Advanced Use Cases

### Knowledge Graph Enrichment

Ask Quick Desktop to enrich your knowledge graph from meetings:

```
Search my LMA meetings from the past week and update my knowledge graph
with the people I met, topics discussed, and any commitments made.
```

### Proactive Monitoring (Requires Phase 4 MCP Enhancements)

Once `get_meeting_signals` is available, create a monitoring agent:

```
Agent ID: lma-risk-monitor
Trigger: Custom code (checks for new meetings, extracts signals)
Action: Alerts on churn risk, escalation signals, competitor mentions
```

## Troubleshooting

### "Tool not found" error

Verify the MCP connection is active:
- Settings → Capabilities → MCP → check LMA shows "Connected"
- If disconnected, click the connection and re-enter the API key

### "Permission denied" on meeting data

LMA enforces UBAC — you can only see your own meetings unless you're an admin:
- Verify you're using your own API key (not a shared one)
- For team-wide agents, ask your LMA admin for an admin-scoped key

### Agent triggers but finds no meetings

- Check that LMA has meeting history: open the LMA web UI and verify meetings are listed
- The agent searches based on attendee names — ensure meeting participants match your calendar invitees

### Agent doesn't trigger before meetings

- Verify the trigger is configured: ask Quick "Show me the lma-pre-meeting-brief agent config"
- Check that the schedule interval (5 min) is short enough to catch the 15-min window
- Verify Outlook calendar is connected in Settings → Capabilities → Connections

## MCP Server Enhancement Recommendations

For best integration experience, consider adding these fields to LMA's MCP server responses:

### URL Deep-Linking (Priority 1)

Add `meetingUrl` and `virtualParticipantUrl` to all tool responses:

```json
{
  "meetingId": "abc123-def456",
  "meetingName": "Acme Weekly Sync",
  "meetingUrl": "https://<your-lma-domain>/#/meeting/abc123-def456",
  "virtualParticipantUrl": "https://<your-lma-domain>/#/virtual-participant/abc123-def456"
}
```

This lets Quick Desktop include clickable links in briefings, action items, and search results.

### Structured Action Items (Priority 2)

Add a `get_action_items` tool that returns:

```json
{
  "actionItems": [
    {
      "owner": "Bob Strahan",
      "item": "Send updated proposal to Acme by Friday",
      "dueDate": "2026-05-23",
      "meetingId": "abc123-def456",
      "confidence": 0.92
    }
  ]
}
```

### readOnlyHint (Quick Win)

Add `readOnlyHint: true` to the `search_lma_meetings`, `get_meeting_transcript`, `get_meeting_summary`, and `list_meetings` tool definitions in the MCP server. This:
- Allows Quick Desktop's scheduled agents to use these tools without requiring `allow_unscoped_write_tools`
- Properly classifies them as read-only in Quick's permission UI
- Follows MCP protocol best practices

Implementation: In `MCPServerAnalyticsFunction`, when returning `tools/list`, add `annotations: { readOnlyHint: true }` to the tool definitions for the four read-only tools.

---

## Reference

- [MCP Servers Overview](mcp-servers.md)
- [MCP API Key Authentication](mcp-api-key-auth.md)
- [Quick Suite MCP Setup (OAuth)](quicksuite-mcp-setup.md) — for Quick Suite web (Enterprise tier)
- [Integration Proposal](../docs/quick-desktop-lma-integration-proposal.md) — full use case catalog and roadmap
