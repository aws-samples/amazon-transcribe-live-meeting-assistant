---
name: lma-live-coach
display_name: LMA Live Call Coach
trigger: coach me on this call
icon: 🎯
inputs:
  - meeting_platform
  - meeting_id
---

# LMA Live Call Coach

Real-time AI sales coaching during calls — launch LMA's virtual participant, then get live coaching cards analyzed against MEDDPICC, SPIN, and Challenger frameworks.

*Inspired by [KenAI Live Call Coach](https://kenbeau.people.aws.dev/) by Ken Beauvais.*

## Prerequisites
- LMA MCP server connected (Settings → Capabilities → MCP)
- A live meeting with a Zoom/Teams/Chime/WebEx meeting ID

## Workflow

### Step 1: Identify the meeting
- **Mode**: agentic
- Ask the user for meeting platform and meeting ID (or extract from calendar)
- If the user has an active calendar event with a meeting link, extract the platform and numeric meeting ID automatically

### Step 2: Launch Virtual Participant
- **Mode**: deterministic
- Call `live_meeting_assistant_lma__start_meeting_now` with:
  - `meetingName`: from calendar subject or user input
  - `meetingPlatform`: Zoom | Teams | Chime | Webex
  - `meetingId`: numeric meeting ID
- Confirm the VP has joined and share the `meetingUrl` and `virtualParticipantUrl` with the user

### Step 3: Start coaching loop
- **Mode**: agentic
- Spawn a background task that runs a coaching loop:
  1. Wait 60 seconds
  2. Call `live_meeting_assistant_lma__get_meeting_transcript` with the meeting ID
  3. Extract the latest transcript chunk (since last check)
  4. Analyze against sales methodology frameworks:
     - **MEDDPICC**: Metrics, Economic Buyer, Decision Criteria, Decision Process, Paper Process, Identify Pain, Champion, Competition
     - **SPIN**: Situation, Problem, Implication, Need-Payoff
     - **Challenger**: Teach, Tailor, Take Control
     - **Buying Signals**: positive indicators, commitment language
     - **Objections**: pushback, concerns, hesitation
  5. Post a coaching card to the activity feed with:
     - Methodology category and color
     - The insight/observation
     - Suggested action for the rep
     - importance: "important"
  6. Repeat until the meeting ends (detect via `list_meetings` showing status=ENDED)

### Step 4: Post-call summary
- **Mode**: agentic
- Once meeting ends:
  1. Call `get_meeting_summary` with includeActionItems=true
  2. Generate a coaching debrief:
     - Key methodology moments identified
     - Missed opportunities
     - Action items extracted
     - Overall call score (based on methodology coverage)
  3. Post final debrief to activity feed

## Coaching Card Format

```
🟣 MEDDPICC — Decision Criteria
"The customer mentioned needing SOC2 compliance and FedRAMP authorization"
→ Action: Confirm you can meet both requirements. Ask about timeline for compliance review.

🟢 SPIN — Implication Question Opportunity
"They mentioned their current solution takes 3 weeks to deploy"
→ Action: Ask "What does that 3-week delay cost you in terms of time-to-market?"

🟠 Challenger — Teach Moment
"Customer seems unaware of the serverless pricing model advantage"
→ Action: Share the TCO comparison — reframe from license cost to total operational cost.
```

## Notes
- The coaching agent runs as a background task — the user can continue chatting
- Coaching cards appear in the activity feed with toast notifications
- The loop polls every 60 seconds to balance responsiveness with cost
- Post-call analysis runs once when the meeting status changes to ENDED
