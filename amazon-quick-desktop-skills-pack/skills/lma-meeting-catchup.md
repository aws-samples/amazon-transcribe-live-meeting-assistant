---
name: lma-meeting-catchup
display_name: LMA Meeting Catch-up
trigger: catch me up on
icon: 📋
inputs:
  - meeting_description
---

# LMA Meeting Catch-up

Quickly catch up on meetings you missed with a synthesized summary combining LMA data with Slack context.

## Prerequisites
- LMA MCP server connected
- Outlook calendar connected (for meeting identification)
- Slack connected (optional, for post-meeting context)

## Workflow

### Step 1: Identify the meeting
- **Mode**: agentic
- Parse the user's request to identify which meeting they missed
- Check today's calendar via `calendar_view` for matching events
- If ambiguous, ask the user to clarify which meeting

### Step 2: Get LMA data
- **Mode**: deterministic
- Call `live_meeting_assistant_lma__search_lma_meetings` with the meeting subject and/or date
- Call `live_meeting_assistant_lma__get_meeting_summary` with `includeActionItems=true` and `includeTopics=true`
- Note the `meetingUrl` from the response for deep-linking

### Step 3: Get Slack context (optional)
- **Mode**: agentic
- Search Slack for messages in the meeting's time window that reference the same topic or participants
- Look for post-meeting follow-ups, decisions shared, or action items discussed after the call

### Step 4: Synthesize catch-up
- **Mode**: agentic
- Combine LMA summary + Slack context into a personalized catch-up:
  - **Key decisions** made during the meeting
  - **Action items** — especially any assigned to the user
  - **What happened after** — Slack follow-ups, if any
  - **What needs your attention** — anything requiring your response
  - **Full meeting link** — clickable URL to the LMA meeting page for full transcript/recording
- Keep it concise — prioritize what the user needs to know to be up to speed

## Output Format

```
📋 **Meeting Catch-up: [Meeting Name]** (missed [time])

**Decisions:**
- [Decision 1]
- [Decision 2]

**Your action items:**
- ⚠️ [Item assigned to you]

**Other action items:**
- [Person]: [Item]

**Post-meeting (Slack):**
- [Any relevant follow-up discussion]

🔗 [Full meeting in LMA](meetingUrl)
```
