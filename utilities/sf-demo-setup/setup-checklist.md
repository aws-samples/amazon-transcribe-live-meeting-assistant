# Pre-Demo Setup Checklist

Complete each section in order. Check off items as you go. Final dry run should be done the day before recording.

---

## 1. LMA Infrastructure

- [ ] LMA stack deployed and healthy (check CloudFormation stack status: `CREATE_COMPLETE` or `UPDATE_COMPLETE`)
- [ ] Virtual Participant enabled with **EC2 launch type** (faster joins, ~30-60 sec vs 1-2 min for Fargate)
- [ ] Nova Sonic voice assistant configured:
  - [ ] `VoiceAssistantProvider` = `amazon_nova_sonic`
  - [ ] `VoiceAssistantActivationMode` = `wake_phrase`
  - [ ] `VoiceAssistantWakePhrases` includes `hey alex`
  - [ ] Test wake phrase detection in a short test meeting
- [ ] Tavily API key configured (for web search)
  - [ ] `TavilyApiKey` parameter set in CloudFormation
  - [ ] Test: ask "what is the weather today?" in chat to confirm web search works
- [ ] Bedrock Knowledge Base operational (if using Meetings Query)
  - [ ] `ShouldUseTranscriptKnowledgeBase` = `true`
  - [ ] At least 2-3 past meetings indexed

## 2. Salesforce MCP

- [ ] Salesforce demo org provisioned (Developer Edition or Sandbox)
- [ ] Salesforce Connected App created with OAuth 2.1 + PKCE (see `docs/salesforce-mcp-setup.md`)
- [ ] Salesforce MCP server installed in LMA (Admin > MCP Servers > Public Registry)
- [ ] OAuth flow completed (authenticate via LMA UI)
- [ ] Test data loaded (see `salesforce-test-data.md`)
- [ ] Test: type "Look up ACME Healthcare in Salesforce" in chat and verify result

## 3. Past Meeting Data

For Scene 7 (Meetings Query), you need past meetings in the system:

- [ ] Run 2-3 short test meetings (5-10 min each) covering topics that will be searchable:
  - One meeting discussing "ACME Healthcare" and pricing (so the query in Scene 7 returns results)
  - One meeting on a different topic (to show variety in the Meetings List)
- [ ] Verify meetings appear in Meetings List with status "Ended"
- [ ] Verify Meetings Query returns results for "ACME Healthcare"

## 4. Zoom Meeting Setup

- [ ] Create a Zoom meeting for the demo recording
- [ ] Ensure Zoom settings allow bots/web clients to join (no waiting room, or pre-admit the VP)
- [ ] VP display name: confirm it shows as "LMA Meeting Assistant" or similar in Zoom
- [ ] All 3 participants (Bob, Jeremy, Chris) have tested audio on their respective devices
- [ ] All participants use **headphones** (prevents echo that confuses transcription)

## 5. Browser & Screen Setup (Bob's Machine)

Bob's screen is the one being captured for the recording.

- [ ] Chrome browser, full screen, no bookmarks bar
- [ ] Tab 1: LMA UI — Meeting detail page (will show live transcript + chat)
- [ ] Tab 2: LMA UI — Meetings List (for Scene 7 navigation)
- [ ] Tab 3: Slide deck (`scratch/slides/index.html`) — for title and closing slides
- [ ] Close all other tabs, notifications, and popups
- [ ] Screen resolution: 1920x1080 recommended
- [ ] System notifications silenced (Do Not Disturb mode)
- [ ] If using macOS: hide Dock, menu bar set to auto-hide

## 6. Recording Setup

- [ ] Screen recording tool ready (OBS, QuickTime, or Zoom's built-in recording)
- [ ] Recording captures Bob's full screen + system audio + microphone
- [ ] Test recording: verify transcript text is readable at 1080p
- [ ] Verify all participants' voices are captured clearly in the recording

## 7. Backup Assets (Capture During Dry Run)

In case a feature fails during the real recording, have these ready:

- [ ] Screenshot: Salesforce MCP response for ACME Healthcare
- [ ] Screenshot: Fact check response catching the $2.5M discrepancy
- [ ] Screenshot: Web search results for healthcare IT trends
- [ ] Screenshot: Post-meeting summary
- [ ] Screenshot: Meetings Query results
- [ ] Short audio/video clip of Alex (Nova Sonic) responding to a voice query

## 8. Day-Before Dry Run

- [ ] Run the full script end-to-end with all 3 participants
- [ ] Time each scene — verify total is under 10 minutes
- [ ] Capture backup screenshots/clips (Section 7 above)
- [ ] Note any timing issues, awkward pauses, or unclear transitions
- [ ] Verify Salesforce MCP response time (if >5 sec, rehearse filler lines)
- [ ] Verify Nova Sonic activation on "Hey Alex" — test 3+ times
- [ ] Verify summary generation after meeting ends (note how long it takes)

## 9. Day-Of Checklist (30 min before recording)

- [ ] LMA stack healthy — check a quick meeting in the UI
- [ ] Salesforce MCP still authenticated (tokens may expire)
- [ ] Zoom meeting open, all participants connected
- [ ] VP joined the Zoom call — confirm "Joined" status in LMA UI
- [ ] Slide deck loaded in browser tab
- [ ] Recording tool running, test clip captured and reviewed
- [ ] All participants have the script open for reference
- [ ] Bob has the timing sheet handy
- [ ] Go!
