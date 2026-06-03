# LMA Skills Pack for Amazon Quick Desktop

Turn your Live Meeting Assistant into an AI-powered meeting intelligence system with pre-built agents and skills for Amazon Quick Desktop.

## What's Included

| Component | Type | Description |
|---|---|---|
| **Pre-Meeting Briefing** | Scheduled Agent | 15 min before meetings, get AI briefs with prior meeting context and action items |
| **Action Item Tracker** | Scheduled Agent | Hourly extraction of commitments from completed meetings |
| **Meeting Catch-up** | Skill | "Catch me up on the meeting I missed" — instant async summaries |
| **Live Call Coach** | Skill | Real-time coaching during calls (MEDDPICC/SPIN/Challenger) |

## Prerequisites

- [LMA](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant) deployed with MCP enabled (v0.2.23+)
- Amazon Quick Desktop installed
- An LMA API key (generated from LMA UI → Settings → MCP Servers Configuration)

## Quick Install

### Step 1: Connect LMA to Quick Desktop

1. Open **Amazon Quick Desktop**
2. Go to **Settings → Capabilities → MCP → "+ Add MCP / Skill"**
3. Enter:
   - **Name**: `Live Meeting Assistant (LMA)`
   - **Endpoint**: Your LMA MCP API endpoint URL
   - **Auth**: [REDACTED_TOKEN]
   - **Token**: Your LMA API key (`lma_xxxx...`)
4. Click **Connect**

### Step 2: Install the Skills Pack

Copy the skills to Quick's skills directory:

```bash
cp -r skills/* ~/.quickwork/skills/
```

### Step 3: Create the Agents

In Amazon Quick Desktop, paste each agent configuration from the `agents/` folder. Or just ask Quick:

```
Create a scheduled agent using the config in agents/pre-meeting-brief.json
```

### Step 4: Verify

Type in Quick Desktop:
```
Search my LMA meetings for discussions about [any recent topic]
```

If you get results, everything is connected.

## Agent Details

### Pre-Meeting Briefing (`lma-pre-meeting-brief`)

- **Schedule**: Every 5 minutes
- **Trigger**: Calendar events starting within 15 minutes
- **Actions**: Searches LMA for prior meetings with attendees, gets summaries, checks knowledge graph, posts brief to activity feed
- **Tools needed**: Outlook calendar, LMA MCP (read), Knowledge Graph

### Action Item Tracker (`lma-action-items`)

- **Schedule**: Every 60 minutes
- **Trigger**: New meetings completed since last run
- **Actions**: Gets summaries with action items, extracts commitments, posts to activity feed (optionally creates Asana tasks)
- **Tools needed**: LMA MCP (read), Asana (optional)

## Skills Details

### Meeting Catch-up (`lma-meeting-catchup`)

Trigger phrase: "catch me up on [meeting]"

Identifies the meeting from your calendar, pulls summary + transcript from LMA, cross-references Slack, and gives you a synthesized catch-up.

### Live Call Coach (`lma-live-coach`)

Trigger phrase: "coach me on this call"

Launches LMA's virtual participant into your active meeting, then spawns a background agent that:
1. Polls the live transcript every 60 seconds
2. Analyzes against sales methodology frameworks (MEDDPICC, SPIN, Challenger)
3. Posts coaching cards to a Slack channel or your activity feed
4. Post-call: generates follow-up email draft and action summary

*Inspired by [KenAI Live Call Coach](https://kenbeau.people.aws.dev/) by Ken Beauvais.*

## Customization

Each agent's prompt can be modified to match your workflow:
- Change the briefing format (bullet points vs. narrative)
- Adjust coaching methodologies
- Add/remove Slack delivery channels
- Tune the trigger timing (5 min vs. 10 min before meetings)

## Troubleshooting

- For setup / connection issues, see
  [docs/amazon-quick-mcp-setup.md](../docs/amazon-quick-mcp-setup.md#troubleshooting).
- For agent-specific issues (triggers, write-tool prompts, KG access), see
  [docs/amazon-quick-desktop-integration.md](../docs/amazon-quick-desktop-integration.md#troubleshooting).

## License

MIT-0 (same as LMA)
