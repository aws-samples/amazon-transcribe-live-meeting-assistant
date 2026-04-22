# LMA Salesforce Demo — Production Plan

A focused ~10-minute video demo of [Live Meeting Assistant](../../README.md) for non-technical potential users and line of business managers. The demo IS the meeting — a realistic Zoom sales pipeline review with 3 human participants + the LMA Virtual Participant bot, showcasing live transcription, AI assistant, Salesforce CRM integration, voice assistant, and post-meeting features.

## Files in This Folder

| File | Description |
|------|-------------|
| [demo-script.md](demo-script.md) | Scene-by-scene script with exact dialogue, screen actions, timing, pacing notes, and recovery lines |
| [slides/index.html](slides/index.html) | Reveal.js slide deck (2 slides: title + closing with differentiators and GitHub QR placeholder) |
| [slides/lma-theme.css](slides/lma-theme.css) | AWS-branded CSS theme for the slides |
| [setup-checklist.md](setup-checklist.md) | Pre-demo infrastructure, data, recording, and dry-run checklist |
| [salesforce-test-data.md](salesforce-test-data.md) | Fictional Salesforce accounts, contacts, and opportunities to load in the demo org |

## Structure (~10 min)

| Scene | Content | Duration | Cumulative |
|-------|---------|----------|------------|
| Title | Title slide + 2-sentence positioning | 0:15 | 0:15 |
| 1 | Meeting start + live transcription | 1:30 | 1:45 |
| 2 | Live translation (Spanish) | 0:45 | 2:30 |
| 3 | Chat button: SUMMARIZE | 0:45 | 3:15 |
| 4 | Salesforce MCP lookup — pull CRM data mid-meeting | 1:30 | 4:45 |
| 5 | Fact check — catch $2.5M vs actual $1.8M discrepancy | 1:00 | 5:45 |
| 6 | Voice assistant (Hey Alex) + web search | 1:30 | 7:15 |
| 7 | Post-meeting: auto-summary, meeting inventory, semantic search | 1:30 | 8:45 |
| 8 | Closing slide | 0:30 | 9:15 |

~45 seconds buffer for natural pauses and tool response times.

## Participants

| Person | Role | Responsibility |
|--------|------|----------------|
| **Bob** | VP Sales / Demo driver | Operates LMA UI, narrates features, clicks chat buttons |
| **Jeremy** | Account Executive | Reports on deals, gives wrong $2.5M figure (triggers fact check), says "Hey Alex" |
| **Chris** | Solutions Architect | Adds technical context, sets up web search ("what's trending in healthcare IT?") |
| **Alex** | LMA Virtual Participant (bot) | Already in the Zoom call. Transcribes, responds to chat and voice queries. |

## Salesforce Demo Accounts

All company names are fictional. Full field details in [salesforce-test-data.md](salesforce-test-data.md).

| Account | Amount | Stage | Primary Contact |
|---------|--------|-------|-----------------|
| ACME Healthcare | $1.8M | Negotiation | James Wilson, CTO |
| Zephyr Software | $750K | Proposal | Lisa Park, Director of IT |
| Brightpath Financial | $3.2M | Discovery | David Okafor, CIO |

## Key Demo Moments

1. **Live transcription** — words appear on screen as people speak, each speaker identified
2. **One-click translation** — toggle Spanish, transcript translates live
3. **Salesforce lookup** — CRM data pulled into the meeting without tab-switching
4. **Fact check** — assistant catches Jeremy's $2.5M vs the actual $1.8M in Salesforce
5. **"Hey Alex" voice assistant** — Nova Sonic searches the web and speaks results in the meeting
6. **Meetings as a knowledge base** — post-meeting semantic search across all past meetings

## Risk Mitigation

| Risk | Fallback |
|------|----------|
| VP fails to join | Fall back to Stream Audio tab |
| Salesforce MCP timeout | "We ran this earlier" + pre-captured screenshot |
| Nova Sonic no response | Type the question in chat instead |
| Translation slow | Have it pre-enabled, just toggle language |
| Web search fails | Skip — not critical, move to next scene |
| Any feature fails | "Let me show you this from a previous session" + backup clips from dry run |

## Prerequisites

See [setup-checklist.md](setup-checklist.md) for the full checklist. In summary:

- LMA deployed with Virtual Participant (EC2 mode), Nova Sonic voice assistant, and Salesforce MCP
- Salesforce demo org with test data loaded
- Tavily API key configured for web search
- 2-3 past meetings in LMA (for Meetings Query in Scene 7)
- Full dry run completed day before recording
- Backup screenshots/clips captured during dry run
