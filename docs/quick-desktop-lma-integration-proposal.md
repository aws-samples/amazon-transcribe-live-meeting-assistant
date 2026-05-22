# Amazon Quick Desktop × LMA Integration Proposal

## Amazon Quick Desktop at a Glance

**Amazon Quick Desktop** is a native macOS/Windows AI-powered work companion for knowledge workers. It combines conversational AI (Claude via Amazon Bedrock), deep integrations, local intelligence, and autonomous agents into a single desktop application. Everything runs locally — the agent, scheduled tasks, file access, memory, and knowledge graph — with cloud calls only to AI models and connected services.

### Core Capabilities

| Capability | What it does |
|---|---|
| **Conversational AI** | Multi-model chat (Fast/Smart/Advanced) with extended thinking, file analysis, code execution |
| **Scheduled Agents** | Autonomous agents on schedule/trigger — morning briefings, monitoring, periodic tasks |
| **Knowledge Graph** | Personal context graph — entities & relationships extracted from Slack, email, calendar, files |
| **Long-term Memory** | Persistent learned facts, preferences, and procedures across conversations |
| **Deep Analysis** | Multi-track research with structured investigation, citations, and deliverables |
| **Search Indexing (RAG)** | Semantic + keyword search over local files and connected content |
| **Skills & Workflows** | Reusable multi-step automations authored from successful conversations |
| **HTML Artifacts** | Interactive dashboards, visualizations, and data apps rendered inline |
| **Browser Automation** | Chrome control with full auth context for web-based tasks |
| **Background Tasks** | Parallel sub-agent execution for complex/long-running work |

### Current Connectors

| Category | Connectors |
|---|---|
| **Messaging** | Slack, Microsoft Teams |
| **Email & Calendar** | Outlook (Microsoft 365), Gmail, Google Calendar |
| **File Storage** | SharePoint, OneDrive, Local Folders |
| **Project Management** | Asana |
| **BI & Workspaces** | Quick Suite (Spaces, dashboards, Q&A resources) |
| **Custom Integrations** | Any service via **MCP (Model Context Protocol)** remote servers |

### Extension Model: MCP

Amazon Quick Desktop uses **MCP as its open extension standard**. Any service exposing a remote MCP server (streamable HTTP, JSON-RPC 2.0) can be connected via Settings → Capabilities → MCP → "+ Add MCP / Skill". Authentication supports OAuth 2.0/2.1, Bearer tokens, API keys, and custom headers. This is how LMA connects.

---

## LMA's MCP Server

LMA already exposes a production-ready remote MCP server with two auth paths:

1. **3LO OAuth** via Cognito + BedrockAgentCore Gateway — for interactive clients (Quick Desktop, Claude Desktop)
2. **API Key** via REST API Gateway + Lambda authorizer — for headless/programmatic clients

### Current Tools (6)

| Tool | Description | Auth |
|---|---|---|
| `search_lma_meetings` | Semantic search across all meeting transcripts and summaries | UBAC enforced |
| `get_meeting_transcript` | Retrieve complete transcript (json/text/srt formats) | UBAC enforced |
| `get_meeting_summary` | AI-generated summary with action items and topics | UBAC enforced |
| `list_meetings` | List meetings with date/participant/status filters | UBAC enforced |
| `schedule_meeting` | Schedule future meeting with virtual participant | UBAC enforced |
| `start_meeting_now` | Start immediate meeting with virtual participant | UBAC enforced |

**UBAC (User-Based Access Control)**: Non-admin users see only their own meetings. Admin users see all meetings. Enforced via JWT `sub` claim on every tool call.

**Setup guide**: [Amazon Quick Suite MCP Action Connector Setup Guide](docs/quicksuite-mcp-setup.md) documents the full OAuth + Quick Suite integration flow.

---

## Integration Use Cases (Organized by Phase)

---

### Phase 0: Validation (Done ✅)

LMA's MCP server already connects to Amazon Quick Desktop via API key auth. The 6 tools are discoverable and callable in chat. Basic queries ("search my meetings for X") work end-to-end.

---

### Phase 1: Read-Only Agents (Weeks 1–3)

*No MCP changes required. Uses existing 6 tools.*

#### 1.1 Pre-Meeting Briefings (Scheduled Agent)

**Trigger**: 15 minutes before any calendar event.

**Flow**:
1. Agent reads upcoming calendar event from Outlook, extracts invitee list
2. Calls `search_lma_meetings` for prior conversations with those participants
3. Calls `get_meeting_summary` on the most recent meetings with each person
4. Cross-references Quick's **knowledge graph** for relationship context (last commitments, project associations)
5. Delivers a synthesized brief via Slack DM or in-app notification — includes **clickable link to the LMA meeting page** for full detail

**Value**: Every meeting starts informed; replaces 20 min of manual prep. Knowledge graph + LMA transcripts = complete relationship memory.

---

#### 1.2 Async Meeting Catch-up ("I Missed the Meeting")

**Scenario**: User says "Catch me up on the standup I missed this morning."

**Flow**:
1. Agent identifies the meeting from calendar (today's standup, user was invited but missed)
2. Calls `get_meeting_summary` for the high-level overview
3. Calls `get_meeting_transcript` for full details
4. Cross-references Slack threads from the same time window for post-meeting discussion
5. Synthesizes a personalized catch-up with a **link to the LMA meeting page** to see the full transcript/recording

**Value**: Asynchronous catch-up in 30 seconds vs. asking a colleague or reading a raw transcript.

---

#### 1.3 Deal/Account Research (Deep Analysis)

**Scenario**: "Generate a Q2 executive review for the Acme account."

**Flow**:
1. Quick's Deep Analysis skill launches multi-track research
2. Track A: `search_lma_meetings` for all Acme mentions → pull transcripts for key calls
3. Track B: CRM activity via MCP (pipeline changes, deal stages, ARR)
4. Track C: Support ticket history and resolution metrics
5. Track D: Public news and competitive intelligence
6. Output: A cited report including **direct quotes from calls** with links to source meetings in LMA

**Value**: Direct quotes from real conversations make executive reports far more credible than CRM summaries alone.

---

#### 1.4 Customer/Account 360 (Chat Agent)

**Scenario**: Sales/CS reps ask "What's going on with Acme?" in a Quick Desktop conversation.

**Flow**:
1. Agent calls `search_lma_meetings` for recent meetings mentioning Acme
2. Pulls open tickets from Jira/ServiceNow (via additional MCP servers)
3. Queries Salesforce (via CRM MCP server) for pipeline status
4. Synthesizes a unified answer with **links to relevant LMA meeting pages** for drill-down

**Value**: Unifies "what was discussed in meetings" with "what's in structured systems" — the perennial gap in account visibility.

---

### Phase 2: Action Item Agent (Weeks 3–5)

*MCP enhancement: Add `get_action_items`. Also add `get_meeting_url` for deep-linking.*

#### 2.1 Action Item Enforcement (Scheduled Agent)

**Trigger**: Runs hourly (or on-meeting-completion webhook).

**Flow**:
1. `list_meetings` since last run to find newly completed meetings
2. `get_action_items` for each meeting → structured output: `{owner, item, due_date?, meeting_id, confidence}`
3. Resolve owners against org directory
4. Create Jira/Asana tickets via connector — include **link to LMA meeting page** in ticket description for context
5. DM owners in Slack with their commitments, ticket links, and **LMA meeting link**

**Value**: Action items don't die in transcripts. Probably the **highest-ROI use case** for measurable productivity gain.

---

#### 2.2 Onboarding & Coaching Agent

**Scenario**: New sales reps ask: "Show me how senior reps handle pricing objections."

**Flow**:
1. `search_lma_meetings` filtered to senior team members + pricing-related queries
2. Surface relevant transcript segments with timestamps and **direct links to those moments in LMA**
3. Optionally synthesize a "playbook" document from multiple call examples

**Value**: Institutional knowledge locked in transcripts becomes searchable training material. Dramatically accelerates ramp time.

**UBAC note**: May need admin-level access or team-scoped sharing model.

---

### Phase 3: Knowledge Graph Integration (Weeks 5–8)

*MCP enhancements: Add `get_meeting_entities`, `get_meeting_participants`.*

#### 3.1 Knowledge Graph Enrichment (Continuous)

**Flow**:
- On meeting completion, Quick calls `get_meeting_entities` → receives structured: people, projects, decisions, commitments, topics
- Calls `get_meeting_participants` → structured participant list with speaking time and email identifiers
- Ingests into Quick's personal KG: "Bob and Alice discuss Project Aurora regularly" / "Last commitment to Acme: deliver POC by May 30"
- KG nodes link back to **LMA meeting URLs** for provenance

**Value**: Quick's "What did I commit to last week?" and "When did I last talk to Alice about X?" answers become dramatically better — meetings are the primary source of relationship context for most knowledge workers.

---

### Phase 4: Proactive Monitoring (Weeks 8–10)

*MCP enhancements: Add `get_meeting_signals`, `subscribe_to_events`.*

#### 4.1 Proactive Risk Monitoring (Scheduled Agent)

**Trigger**: Event-driven via `subscribe_to_events` (or polling `list_meetings` as fallback).

**Signal detection**:
- Churn signals: "considering alternatives," "evaluating competitors," "frustrated with"
- Escalation signals: "need to involve leadership," "deadline at risk"
- Compliance keywords: industry-specific regulatory mentions
- Competitor mentions: named competitor references

**Flow**:
1. Receive notification of newly completed meeting
2. Call `get_meeting_signals` → pre-classified: `{type, snippet, severity, timestamp}`
3. Alert account team in Slack with transcript snippet, severity, and **link to LMA meeting page** for full context

**Value**: Turns LMA's transcript archive into a real-time signal source. Catches risks that would otherwise surface weeks later.

---

### Phase 5: Bidirectional & Write Operations (Weeks 10–12)

*MCP enhancements: Add `set_meeting_context`. Validate write-tool UBAC end-to-end.*

#### 5.1 "Schedule It Now" Voice/Chat Actions

**Scenario**: User says in Quick Desktop chat: "Spin up a follow-up with the Acme team tomorrow at 2pm with the LMA bot joining."

**Flow**:
1. Agent resolves meeting time from natural language
2. Creates calendar event via Outlook connector
3. Calls `schedule_meeting` to register LMA's virtual participant
4. Returns confirmation with **link to the LMA Virtual Participant page** showing scheduled status

**Value**: Closes the loop — Quick can not only *read* LMA, it can *drive* LMA. Meeting capture becomes a first-class action, not an afterthought.

---

#### 5.2 Meeting Continuity (Bidirectional)

**Scenario**: When a recurring meeting starts, the in-meeting LMA agent is pre-loaded with context from Quick.

**Flow**:
1. Quick detects meeting start via calendar
2. Calls `search_lma_meetings` for previous meetings with same participants
3. Pushes "last time we met" summary back to LMA's Strands agent via `set_meeting_context`
4. LMA's in-meeting assistant can reference prior context without being asked
5. Quick receives back the **live meeting page URL** for the user to open and see real-time transcription

**Value**: Meetings don't start from zero. The assistant remembers what was discussed last time.

---

#### 5.3 Live Call Coaching (Real-Time)

**Scenario**: User says "coach me on this call" during an active meeting.

**Flow**:
1. Quick launches LMA's virtual participant into the active meeting via `start_meeting_now`
2. Spawns a background coaching agent that polls `get_meeting_transcript` every 60 seconds
3. Each chunk is analyzed against sales methodology frameworks (MEDDPICC, SPIN, Challenger)
4. Coaching cards are posted to the activity feed in real-time:
   - 🟣 MEDDPICC insights (Decision Criteria, Champion identification, etc.)
   - 🟢 SPIN coaching (Implication questions, Need-Payoff opportunities)
   - 🟠 Challenger moments (Teach, Tailor, Take Control)
   - 🟡 Buying signals detected
   - 🔴 Objection handling suggestions
5. Post-call: auto-generates debrief with call score, missed opportunities, and action items

**Value**: Real-time methodology coaching during live calls. Institutional sales knowledge applied at the moment it matters, not after the fact.

*Inspired by [KenAI Live Call Coach](https://kenbeau.people.aws.dev/) by Ken Beauvais — who built this exact pattern on LMA + Amazon Quick with 8 methodology prompts, Slack delivery, and a live coaching dashboard.*

**Requirements**: Existing `start_meeting_now` + `get_meeting_transcript` tools. Background task polling in Quick Desktop.

---

## Distribution: Skills Pack

Following the model established by Ken Beauvais's KenAI installer, LMA ships a **Quick Desktop Skills Pack** (`quick-desktop-skills-pack/`) containing:

- Pre-configured agent definitions (JSON) for pre-meeting briefing and action-item tracking
- Reusable skills (SKILL.md format) for meeting catch-up and live coaching
- One-step install instructions

Users copy skills to `~/.quickwork/skills/`, create agents from the bundled configs, and connect their LMA MCP endpoint. Total setup: ~5 minutes.

---

## Proposed MCP Server Enhancements

### New Tools

| New Tool | Purpose | Phase | Enables |
|---|---|---|---|
| `get_meeting_url` | Returns the LMA web UI URL for a given meeting (meeting detail page) | **1** | Deep-linking from Quick to LMA UI for all use cases |
| `get_virtual_participant_url` | Returns the VP status/control page URL for a scheduled/active VP session | **1** | Users can jump to LMA to see live transcription or VP status |
| `get_action_items` | Returns structured action items: `{owner, item, due_date?, meeting_id, confidence}` | **2** | Reliable ticket creation without parsing free-text summaries |
| `get_meeting_participants` | Returns structured participant list with speaking time, roles, email identifiers | **3** | Better pre-briefs and graph relationship building |
| `get_meeting_entities` | Returns extracted entities: people, projects, decisions, commitments, topics | **3** | Structured KG ingestion |
| `get_meeting_signals` | Returns pre-classified signals: `{type: "churn_risk"\|"escalation"\|"competitor", snippet, severity}` | **4** | Efficient proactive monitoring without re-analyzing transcripts |
| `subscribe_to_events` | Webhook/SSE registration for meeting-completion events | **4** | Event-driven agents vs. polling `list_meetings` |
| `set_meeting_context` | Accepts pre-loaded context to inject into LMA's in-meeting agent | **5** | Bidirectional meeting continuity (Quick → LMA) |

### Alternative: Enrich Existing Tool Responses

Rather than adding `get_meeting_url` as a separate tool, we could **include `meetingUrl` and `virtualParticipantUrl` fields in the responses** of existing tools:

```json
// In list_meetings, search_lma_meetings responses:
{
  "meetingId": "abc123-def456",
  "meetingName": "Acme Weekly Sync",
  "meetingUrl": "https://lma.example.com/#/meeting/abc123-def456",
  "virtualParticipantUrl": "https://lma.example.com/#/virtual-participant/abc123-def456",
  ...
}

// In schedule_meeting, start_meeting_now responses:
{
  "meetingId": "new-meeting-789",
  "status": "scheduled",
  "meetingUrl": "https://lma.example.com/#/meeting/new-meeting-789",
  "virtualParticipantUrl": "https://lma.example.com/#/virtual-participant/new-meeting-789"
}
```

**Recommendation**: Embed URLs in existing responses (simpler, fewer tool calls) AND expose `get_meeting_url` as a standalone tool for cases where the agent only has a `meetingId` from context/memory.

### Priority Order for Enhancements

1. **URL fields in existing responses** — Lowest effort, highest breadth. Every tool call gives the user a way to jump into LMA UI.
2. **`get_action_items`** — Highest standalone ROI. Unblocks reliable action-item enforcement.
3. **`get_meeting_participants`** — Low effort, improves pre-briefs and KG enrichment.
4. **`get_meeting_entities`** — Enables KG enrichment without brittle text parsing.
5. **`subscribe_to_events`** — Unlocks event-driven architecture for all scheduled agents.
6. **`get_meeting_signals`** — Efficiency gain for monitoring (works without it, just less efficiently).
7. **`set_meeting_context`** — Most ambitious; requires LMA architecture changes.

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────────┐
│              Amazon Quick Desktop                         │
│                                                          │
│  ┌──────────┐  ┌───────────┐  ┌──────────────────┐     │
│  │ Chat AI  │  │ Scheduled │  │ Knowledge Graph  │     │
│  │ (Claude) │  │ Agents    │  │ + Long-term Mem  │     │
│  └────┬─────┘  └─────┬─────┘  └────────┬─────────┘     │
│       │               │                  │               │
│       └───────────────┼──────────────────┘               │
│                       │                                  │
│              ┌────────▼────────┐                         │
│              │   MCP Client    │                         │
│              └────────┬────────┘                         │
└───────────────────────┼─────────────────────────────────┘
                        │ HTTPS (Bearer/API-key or OAuth)
                        ▼
┌───────────────────────────────────────────────────────────┐
│              LMA MCP Server                                │
│                                                           │
│  ┌─────────────────┐    ┌──────────────────────────────┐ │
│  │ API Gateway      │    │ BedrockAgentCore Gateway     │ │
│  │ (API Key auth)   │    │ (OAuth 3LO)                  │ │
│  └────────┬─────────┘    └────────────┬─────────────────┘ │
│           │                           │                    │
│           └───────────┬───────────────┘                    │
│                       ▼                                    │
│         ┌─────────────────────────┐                        │
│         │ MCPServerAnalytics      │                        │
│         │ Lambda (UBAC enforced)  │                        │
│         └────────────┬────────────┘                        │
│                      │                                     │
│     ┌────────────────┼────────────────┐                    │
│     ▼                ▼                ▼                    │
│  DynamoDB    Bedrock KB        Virtual Participant          │
│  (meetings)  (semantic search) (Zoom/Teams/Chime/Meet)     │
└───────────────────────────────────────────────────────────┘
```

---

## Key Differentiators vs. Generic Meeting-AI Integrations

1. **Personal context graph** — Quick doesn't just search transcripts; it builds a persistent model of relationships, commitments, and topics that compounds over time
2. **Autonomous agents** — Not just query/response; scheduled agents act on meeting data without user prompting
3. **Multi-source synthesis** — LMA data is combined with Slack, email, calendar, CRM, and files in a single reasoning pass
4. **Local-first privacy** — Knowledge graph, memory, and agent logic run on the user's machine; only model inference goes to the cloud
5. **MCP-native** — Zero custom integration code; LMA is just another MCP server, enabling community adoption
6. **Deep-link to source** — Every meeting reference includes a clickable URL back to the full LMA UI for live transcription, recording playback, and meeting detail

---

## Open Questions

1. **UBAC scope for team use cases**: Use Cases 2.2, 4.1 require access beyond the user's own meetings. How do we handle team/org-level access without making everyone an admin? Options: role-based scopes, team-level sharing, delegated access.
2. **Event-driven vs. polling**: `subscribe_to_events` requires LMA to push notifications. Should this be webhooks, SSE, or a polling-optimized `list_meetings(since=last_sync)` pattern?
3. **Reverse flow auth**: For `set_meeting_context` (Use Case 5.2), what auth model lets Quick push into a live LMA session?
4. **Rate limiting**: Scheduled agents polling `list_meetings` hourly across many users — what's the API Gateway throttle strategy?
5. **URL format stability**: Should `meetingUrl` use the CloudFront distribution URL, a custom domain, or a stable redirect endpoint that survives stack updates?
