---
title: "Amazon Quick MCP Setup"
---

# Amazon Quick MCP Setup

This guide covers connecting **Live Meeting Assistant (LMA)** to Amazon's two
Quick products:

| Product | Type | Auth path | Best for |
|---|---|---|---|
| **Amazon Quick Suite** | Web app (Enterprise tier) | OAuth 3LO via Cognito | Multi-user organizations using Quick Suite for chat, dashboards, and connectors |
| **Amazon Quick Desktop** | Native macOS / Windows app | API key (Bearer) | Individual users wanting scheduled agents and a personal knowledge graph |

Both expose the same seven LMA tools (search, transcript, summary, list,
schedule, start-now, VP status). The API key path offers 10: those seven and
`list_active_meetings`, `get_meeting_agenda` and `get_meeting_frames`; and 11 while the
Configuration page setting **Meeting Assistant › MCP server: live meeting context**
is on, which adds `get_live_context`. Pick the path below that matches the product
you're connecting.

> **Looking for ways to use LMA from Quick Desktop?** Once you've completed
> the Quick Desktop setup below, see
> [Quick Desktop: LMA Workflows](amazon-quick-desktop-integration.md) for
> conversational workflows (joining meetings, posting recaps to Slack,
> querying past transcripts), pre-built scheduled agents, and the skills
> pack.

---

## Prerequisites (both paths)

- LMA deployed with MCP Server enabled (v0.2.23 or later)
  - During deployment, the `EnableMCP` parameter must be set to `true` (this
    is the default)
  - If you already have LMA deployed without it, see
    [Enabling MCP Server on Existing Deployment](#enabling-mcp-server-on-existing-deployment)
    below
- Admin access to the LMA CloudFormation stack outputs

### Enabling MCP Server on Existing Deployment

If you deployed LMA with MCP Server disabled:

1. Log into the **AWS Management Console**
2. Navigate to **CloudFormation**
3. Select your LMA stack (default name: `LMA`)
4. Click **Update**, choose **Use current template**, click **Next**
5. Find **Enable MCP Server Integration** and set it to `true`
6. Click **Next** through the remaining screens, acknowledge, and **Update stack**
7. Wait ~10–15 minutes for the update to complete
8. Open the **Outputs** tab to see the new MCP configuration values

---

## User-Based Access Control (UBAC)

LMA enforces UBAC across both paths via the JWT or API key user context:

- **Non-admin users** can only access their own meetings
- **Admin users** can access all meetings

UBAC is enforced at the MCP tool level on every call, regardless of which
auth path Quick uses.

---

## Available LMA Tools

Both auth paths expose the same set of tools, except that the tools marked "API
key endpoint only" are offered on the API key endpoint alone. Read-only tools are annotated
with `readOnlyHint: true` so MCP clients (including Quick Desktop scheduled
agents) classify them correctly.

| Tool | Read-only | Description |
|---|---|---|
| `search_lma_meetings` | ✅ | Semantic search across meeting transcripts and summaries |
| `get_meeting_transcript` | ✅ | Full transcript in `text`, `json`, or `srt` format |
| `get_meeting_summary` | ✅ | AI-generated summary with action items and topics |
| `list_meetings` | ✅ | Filter by date range or status |
| `list_active_meetings` | ✅ | The meetings running now (API key endpoint only) |
| `get_meeting_agenda` | ✅ | The agenda of a meeting, as numbered items (API key endpoint only) |
| `get_meeting_frames` | ✅ | The shared-screen key frames of a meeting, as text (API key endpoint only) |
| `get_live_context` | ✅ | The transcript of a running meeting, as it grows (API key endpoint only; listed only while live meeting context is on) |
| `get_virtual_participant_status` | ✅ | Poll VP status (joining, active, manual-action-required, failed) |
| `schedule_meeting` | — | Schedule a future meeting with the LMA virtual participant |
| `start_meeting_now` | — | Start an immediate meeting with the LMA virtual participant |

All tool responses include a `meetingUrl` and (where applicable) a
`virtualParticipantUrl` deep-link back into the LMA web UI for quick drill-down
into the full transcript, recording, or VP viewer.

---

# Path A: Quick Suite (OAuth)

Use this path when connecting LMA to **Amazon Quick Suite** (the web product).
Quick Suite uses three-legged OAuth (3LO) so each user authenticates with
their own LMA Cognito credentials before Quick Suite can call LMA tools on
their behalf.

## Path A Prerequisites

- Amazon Quick Suite **Enterprise** subscription (required to create action connectors)
- Amazon Quick Suite **Professional or Enterprise** subscription (required to invoke action connectors)
- Quick Suite Author role or higher

## Authentication: Three-Legged OAuth (3LO)

LMA's MCP server uses **OAuth 2.0 Authorization Code Flow** with Amazon
Cognito as the identity provider:

1. Quick Suite redirects the user to LMA's Cognito login page
2. The user authenticates with their LMA credentials
3. Cognito returns an authorization code to Quick Suite
4. Quick Suite exchanges the code for access and refresh tokens
5. Quick Suite uses the access token to call LMA's MCP tools

> **Each request must identify the user.** LMA scopes every tool result to the
> calling user, so a tool call that reaches LMA without a resolvable user
> identity is answered with `403 Unable to determine the calling user` instead
> of being served. If you see that response on this path, use the per-user API
> key endpoint described in [Path B](#path-b-quick-desktop-api-key) — it carries
> the user's identity on every request and exposes the same seven tools, and the ones offered on that endpoint only. See
> [MCP API Key Authentication › Connecting with a User Identity](mcp-api-key-auth.md#connecting-with-a-user-identity).

## Step 1: Gather LMA MCP Server Configuration

1. Log into the **AWS Management Console** → **CloudFormation**
2. Select your **main LMA stack** (default name: `LMA`)
3. Click the **Outputs** tab
4. Copy these values:

| Output Key | Description |
|---|---|
| `MCPServerEndpoint` | OAuth-protected MCP server URL (BedrockAgentCore Gateway) |
| `MCPServerClientId` | OAuth Client ID |
| `MCPServerClientSecret` | OAuth Client Secret (sensitive — store securely) |
| `MCPServerTokenURL` | Cognito OAuth token endpoint |
| `MCPServerAuthorizationURL` | Cognito OAuth authorization endpoint |

> These outputs only appear when the stack was deployed with `EnableMCP=true`.

Also copy `MCPServerApiKeyEndpoint` while you are on this tab. That is the
per-user API key endpoint, and it is the route to use if tool calls through
`MCPServerEndpoint` come back with `403 Unable to determine the calling user`
(see [Path B](#path-b-quick-desktop-api-key)).

> **To confirm on a deployed stack:** whether Quick Suite can send a static LMA
> API key as `Authorization: Bearer <key>` has not been verified against a live
> Quick Suite tenant. §2.3 below documents only user-authentication (OAuth) as the
> Quick Suite auth method, and Quick Suite does not let you add arbitrary headers.
> If your Quick Suite version has no bearer-token/API-key field for a remote MCP
> server, use Quick Desktop (Path B) or another MCP client for the API key route.
> LMA's endpoint itself accepts the key over `Authorization: Bearer` or
> `x-api-key` either way.

## Step 2: Create the MCP Integration in Quick Suite

### 2.1 Add the integration

1. Log into the **Amazon Quick Suite console**
2. Click **Integrations** in the left navigation
3. Click **Add** (the plus "+" icon)

### 2.2 Configure integration details

1. **Name**: `LMA Meeting Assistant`
2. **Description**: `Access Live Meeting Assistant transcripts, summaries, and meeting data`
3. **MCP server endpoint**: paste the `MCPServerEndpoint` value from Step 1
   > If tool calls later return `403 Unable to determine the calling user`, change
   > this to the `MCPServerApiKeyEndpoint` value and authenticate with a personal
   > LMA API key instead ([Path B, Step 1](#step-1-generate-an-lma-api-key)) — if
   > your Quick Suite version offers a bearer-token / API-key field for a remote
   > MCP server. See the caveat under Step 1.
4. Click **Next**

### 2.3 Configure authentication

1. Authentication method: **User authentication (OAuth)**
2. Configuration approach: **Manual configuration**
   > LMA's Cognito does not support Dynamic Client Registration (DCR).
3. Enter:
   - **Client ID**: `MCPServerClientId`
   - **Client Secret**: `MCPServerClientSecret`
   - **Token URL**: `MCPServerTokenURL`
   - **Auth URL**: `MCPServerAuthorizationURL`
   - **Redirect URL**: use Quick Suite's callback URL (provided by Quick Suite)
4. Click **Create and continue**

### 2.4 Authorize the integration

1. You'll be redirected to the **LMA Cognito login page**
2. Sign in with your LMA email and password
3. Review the requested permissions and click **Allow**
4. You'll be redirected back to Quick Suite

### 2.5 Review discovered tools

Quick Suite will discover the seven LMA tools. Click **Next** to continue.

### 2.6 Share (optional)

To let other users in your organization use this integration:

1. Click **Share integration**
2. Select users or groups
3. Click **Save**

> Each user authenticates with their own LMA credentials the first time
> they use the integration.

### 2.7 Finish

Click **Done**. Quick Suite will create the action connector — wait for the
status to change from **Creating** to **Active** (1–2 minutes).

## Step 3: Test (Quick Suite)

In the Quick Suite **Chat** interface, try natural language queries:

- `Search my LMA meetings for discussions about the product roadmap`
  (calls `search_lma_meetings`)
- `Get the transcript for LMA meeting <CallId>`
  (calls `get_meeting_transcript`)
- `List my recent LMA meetings from the past week`
  (calls `list_meetings`)

The first time any user invokes the connector, they'll see a **Sign in**
button to start their personal OAuth flow.

---

# Path B: Quick Desktop (API Key)

Use this path when connecting LMA to **Amazon Quick Desktop** (the native
macOS/Windows app). Quick Desktop uses a per-user API key with Bearer-token
authentication.

## Path B Prerequisites

- Amazon Quick Desktop installed (macOS or Windows)
- An LMA account (the API key is tied to your individual LMA user)

## Authentication: Per-User API Key

LMA exposes a separate REST API Gateway endpoint for API-key access. Keys
are SHA-256 hashed at rest, scoped to a single user, and revocable from the
LMA UI. See [MCP API Key Authentication](mcp-api-key-auth.md) for the full
architecture.

## Step 1: Generate an LMA API Key

1. Open your LMA web UI
2. Navigate to **Settings → MCP Servers Configuration**
3. In the **Hosted MCP Access** section at the top, click **Generate API Key**
4. Copy the key (format `lma_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`) —
   **it's shown once only**
5. Note the **MCP API Endpoint URL** displayed on the same page (this maps
   to the `MCPServerApiKeyEndpoint` CloudFormation output)

## Step 2: Add LMA as an MCP Server in Quick Desktop

1. Open **Amazon Quick Desktop**
2. Navigate to **Settings → Capabilities → MCP**
3. Click **+ Add MCP / Skill**
4. Choose **REMOTE** as the connection type
5. Configure:
   - **Name**: `Live Meeting Assistant (LMA)`
   - **URL**: your **MCP API Endpoint URL** from Step 1
   - **Headers**: click **+ Add header** and enter:
     - **Header Name**: `x-api-key`
     - **Value**: your API key from Step 1
6. Click **Connect**

Quick Desktop will discover the LMA tools (10, or 11 while live meeting context
is on) and confirm the connection.

> **Note**: Quick Desktop authenticates the API-key path with a custom
> `x-api-key` header rather than a Bearer-token field. LMA's MCP API Gateway
> accepts the API key over either `x-api-key` or `Authorization: Bearer`, so
> if your Quick Desktop version offers a Bearer-token option instead of custom
> headers, that works too.

## Step 3: Test (Quick Desktop)

In a new Quick Desktop conversation:

```
Search my LMA meetings for discussions about [any recent topic]
```

Quick should call `search_lma_meetings` and return results.

### Common chat queries

| Query | Tools called |
|---|---|
| "What meetings did I have last week?" | `list_meetings` |
| "Find meetings where we discussed pricing" | `search_lma_meetings` |
| "Summarize yesterday's standup" | `list_meetings` → `get_meeting_summary` |
| "What action items came out of the Acme call?" | `search_lma_meetings` → `get_meeting_summary` |
| "Get the full transcript of meeting XYZ" | `get_meeting_transcript` |
| "Schedule LMA to join my 2pm Zoom tomorrow" | `schedule_meeting` |
| "Catch me up on the standup I missed this morning" | `list_meetings` → `get_meeting_summary` (often combined with Slack/calendar lookups) |

### Next: workflows, agents, and skills

For end-to-end LMA workflows in Quick Desktop — conversational prompts
(*"join my next meeting"*, *"post the action items to Slack"*,
*"find what we agreed about X in past meetings"*), pre-built scheduled
agents, and packaged skills (live coaching, async catch-up) — see:

- [Quick Desktop: LMA Workflows](amazon-quick-desktop-integration.md) —
  conversational workflows, scheduled agent recipes, and the skills pack
- [`amazon-quick-desktop-skills-pack/`](../amazon-quick-desktop-skills-pack/) — installable
  bundle of agents and skills

---

## Tool Reference (Both Paths)

Common parameters for the most-used tools.

### `search_lma_meetings`

- `query` (required) — natural-language query
- `maxResults` — default 10
- `startDate` / `endDate` — optional ISO 8601

### `get_meeting_transcript`

- `meetingId` (required) — the meeting `CallId`
- `format` — `text` (default), `json`, or `srt`

### `get_meeting_summary`

- `meetingId` (required)
- `includeActionItems` — default `true`
- `includeTopics` — default `true`

### `list_meetings`

- `startDate` / `endDate` — ISO 8601
- `participant` — matches a name in a participant list stored on the meeting
  record, which LMA itself does not write, so it matches no meeting LMA recorded
- `status` — `ALL` (default), `IN_PROGRESS` (meetings that are running: `STARTED`
  or `TRANSCRIBING`), `STARTED`, `TRANSCRIBING`, `ERRORED`, or `ENDED`. The value
  is not case sensitive, and a blank value means `ALL`; any other value is refused
  with the list of accepted ones
- `limit` — default 20

Your access and `status` are applied while the meetings are read, so `limit`
counts only the meetings you can see that match: other people's newer meetings,
or a `status` that fits an older meeting, no longer leave the list short or empty.
At most 1,000 of the most recent meetings in the date range are looked at. When
that limit ends the search with older meetings still in the range, the answer
carries `truncated: true` and a `note` saying that older matching meetings may not
be listed; narrow the date range to see them. If some meetings could not be read,
the `note` says so ("Some meetings could not be read; try again."), and if the
search itself stopped early because the date index could not be read, it says
"The search could not be completed; try again." The sentences are joined when
more than one applies. `truncated` and `note` are absent otherwise.

### `list_active_meetings`

Available on the API key endpoint only (Path B), not on the OAuth path.

- `limit` — default 10, from 1 to 50; a number outside the range is moved to the
  nearest end, and a value that is not a whole number is refused

Returns the meetings that are running now (`STARTED` or `TRANSCRIBING`, created
in the last 24 hours) that you own or that are shared with you; an Admin sees
all of them. Each has `meetingId`, `meetingName`, `status`, `startedAt`,
`lastActivityAt`, `owner`, `consent` (`mode`, `status`, `captureAllowed`, or
`null`), `meetingUrl` and `virtualParticipantUrl`; there is no transcript text.
Use the `meetingId` with the other tools.

- `lastActivityAt` is when the meeting's record last changed. A meeting that
  stopped long ago but was never closed is still listed until LMA ends it
  (after several hours by default), so an old `lastActivityAt` marks one that
  has probably finished.
- `truncated: true` with a `note` means the search reached its limit of the
  1,000 most recent meetings of the last 24 hours with more left, so a running
  meeting that started earlier may not be listed.
- Uploaded recordings that are still being processed are not listed.

Meetings shared with you are listed here although `list_meetings`,
`get_meeting_transcript` and `get_meeting_summary` show only the meetings you
own. See [MCP API Key Authentication](mcp-api-key-auth.md#list_active_meetings).

### `get_meeting_agenda`

Available on the API key endpoint only (Path B), not on the OAuth path.

- `meetingId` (required) — the meeting `CallId`

Returns the agenda stored on a meeting you can read (one you own, one shared with
you, or any meeting for an Admin): `meetingId`, `hasAgenda`, `items` (each with
`number` and `text`), `source` and `sourceDescription`. Items are numbered the way
the meeting page numbers them: one per line, with blank lines and list marks (`-`,
`*`, `1.`, `a)`) removed. A meeting with no agenda returns `hasAgenda: false`, an
empty `items` and `source: null`. No model is called and nothing is judged: the tool
returns what is stored, and does not say which items were covered.

The answer is bounded however long the stored agenda is. At most 100 items are
returned, and the text of all items together is held to 20,000 units (an ASCII
character counts 1, any other character 3, an emoji 6), shared out so that a short
item keeps all of its room and a long one is cut. When anything was cut, the answer
has `truncated: true` and a `note` that says whether items were left out, texts were
shortened, or both; an item whose text was shortened has `textTruncated: true`. An
agenda that fits (every real one) has none of these fields.

`source` is where the agenda came from: `scheduled` (given when the Virtual
Participant was started or scheduled), `mcp` (passed by an MCP client), `ui` (set on
the meeting page) or `derived` (read from the transcript), or `null` when none is
recorded. An agenda that a person typed or passed in (`scheduled`, `mcp`, `ui`,
including one edited on the meeting page) is returned whatever the meeting's
consent settings say. An agenda with any other source, `derived` included, is
captured content and is returned only when the meeting's consent settings allow
capture; otherwise the tool answers that capture is not allowed for the meeting.

A meeting that does not exist and a meeting you may not read give the same answer,
`Access denied to this meeting`.

### `get_meeting_frames`

Available on the API key endpoint only (Path B), not on the OAuth path.

- `meetingId` (required) — the meeting `CallId`
- `last` — how many of the latest frames to return: default 3, from 1 to 20; a
  number outside the range is moved to the nearest end, and a value that is not a
  whole number is refused
- `fromSeconds` / `toSeconds` — optional, seconds from the start of the meeting.
  With either, the tool returns the frames that were on screen at any point of the
  range, up to the latest 20, and `last` is ignored. A boolean, text, a negative
  number, a value that is not finite or too large, and a `fromSeconds` after
  `toSeconds` are refused

Returns the key frames of the meeting's shared screen (slides, documents), as text:
`meetingId`, `count`, `totalFrames` (the frames of shared content the meeting has),
`frames` and `textRead` (how many of the returned frames had their text read from
the picture). Each frame has `frameId`, `fromSeconds` and `toSeconds` (when it went
up and when the next frame replaced it; `toSeconds` is `null` for the latest),
`from` and `to` as `MM:SS` (`H:MM:SS` from one hour), `title`, `text` and
`textTruncated`. `text` is `null` for a frame whose text has not been read yet. Frames
of the meeting's camera video are not returned. Pictures and links to them are never
returned.

- The titles and the texts of one answer share 20,000 units: an ASCII character
  counts 1, any other character 3, and an emoji or other character beyond U+FFFF
  counts 6. A title is cut to 200 characters and 200 units first, a `frameId` to
  64 characters, and what the titles and ids use comes off the 20,000. The texts
  then get equal shares, and a short text leaves what it does not use to longer
  ones. A frame whose text was cut has `textTruncated: true`, and the answer's
  `note` says so. Twenty frames of 6,000 Chinese characters come to about 52 KB as
  sent; the largest answer of any alphabet (20 frames, every title and text at its
  maximum, made of characters that are escaped twice, such as quotation marks) is
  under 100 KB.
- Every call reads the projected attributes (id, time, kind) of all the meeting's
  frames, up to 2,000, whatever `last` or the time range is, because DynamoDB bills
  a query by the size of the items it reads and not by the attributes it returns. A
  meeting with 2,000 frames costs about 22 requests: the meeting row, 20 queries of
  100 frames one after another, and one batch read for the text of the chosen
  frames. A meeting with a few hundred frames costs a handful. Only the chosen
  frames' text is returned.
- At most 2,000 frames of a meeting are read. If it has more, the `note` names the
  time after which frames were not read.
- The meeting must be one you can read, and its consent settings must allow capture:
  frames are always withheld from a meeting whose consent settings do not. The
  meeting row is read with a strongly consistent read, so an objection that was
  just recorded is seen by the next call. A meeting whose own consent record names a
  mode is decided by that record. A meeting without one (every meeting in a
  deployment where consent is off) is decided by the deployment's consent mode,
  which is read from the configuration table on each call (three strongly consistent
  reads); if that read fails, the tool refuses with "try again" and returns nothing.

See [MCP API Key Authentication](mcp-api-key-auth.md#get_meeting_agenda).

### `get_live_context`

Available on the API key endpoint only (Path B), and only while an Admin has
switched on **Meeting Assistant › MCP server: live meeting context** on the
Configuration page (off by default; a change reaches a running deployment
within about five minutes). While it is off the tool is not listed, and a call
is refused with a sentence that says where to turn it on.

- `meetingId` (required) — the meeting `CallId`, for example from
  `list_active_meetings`
- `cursor` — the `nextCursor` of the previous answer; omit it (or pass an empty
  value) for the newest lines. A cursor that is not valid, belongs to another
  meeting or was made by an older version is refused; call again without one
- `maxLines` — most lines in one answer, default 200, from 1 to 1,000; read
  like `limit` of `list_active_meetings`
- `includeChat` — `true` (default) or `false`: whether the messages typed in
  the meeting chat are among the lines

The answer has `meetingId`, `status`, `ended`, `lines`, `nextCursor`,
`truncated` and sometimes a `note`. Each line has `t` (the time from the start
of the meeting, `MM:SS`, or `H:MM:SS` from one hour on), `speaker` (named as
the meeting page names them; `MeetingAssistant` for the assistant's answers,
the sender for a chat message), `channel`, `segmentId` and `text`. A chat
message is one line, its line breaks shown as " / ". A line longer than 2,000
characters is cut and ends in `[...]`.

How to follow a meeting:

1. Call without a cursor: you get the newest lines (up to `maxLines`).
2. Wait at least 5 seconds, then call with the `nextCursor` of the last answer.
   You get every line written since, and again the lines written in the 60
   seconds before the cursor, so that a line LMA revised, or stored a little
   late (an assistant answer, a corrected phrase), reaches you. Keep one line
   per `segmentId`: replace its text when it changed, and skip a repeat with
   the same text. The repeats are the price of catching late lines: when
   someone speaks every 4 seconds, about 15 lines of each answer are repeats.
3. When `truncated` is `true`, more lines are waiting: call again at once with
   `nextCursor`. It goes on after the last line you got, with no gap and no
   repeat. An answer holds at most `maxLines` lines and is kept under about
   100 KB whatever the language: the tool counts an ASCII character as 1
   (3 for a quotation mark, a backslash or a control character, which are
   escaped), another character as 3 (6 for an emoji), and 60 more for the JSON
   around each line, up to 30,000 per answer. That is about 130 lines of 120
   English characters, or about 60 of Chinese.
4. When `ended` is `true` the meeting is over; the lines are still returned.
   The summary job writes the full transcript shortly after the meeting ends:
   until then `get_meeting_transcript` answers that the transcript is being
   prepared and to try again in a minute (or to use `get_meeting_summary`
   later). It says so only in the two hours after the meeting's record last
   changed and only while its transcript has not been deleted; otherwise it
   answers "Transcript not found", and the `note` of `get_live_context` no
   longer says to try again.

Limits: a line is missed only if LMA stores it more than about 60 seconds after
the write time it stamps on the line when it starts processing it, and a
difference between clocks counts against those 60 seconds. A first call without
a cursor returns only the newest `maxLines` lines. A speaker renamed later is
not sent again under the new name. A line with no readable write time (no LMA
writer produces one today) is placed by its time in the meeting, and is missed
if it is stored more than about 60 seconds after it ends.

A deployment with a Transcript Lambda Hook (`TRANSCRIPT_LAMBDA_HOOK_FUNCTION_ARN`)
runs the hook between stamping a line and storing it, so a slow hook uses up
those 60 seconds. Each call reads the meeting's whole live transcript, so its
cost grows with the meeting: about 90 read units for 1,000 lines and 1,700 for
20,000, which is why a client should poll no faster than every 5 seconds. A
meeting too large to read and answer within 20 seconds is refused with a
message that says to use `get_meeting_transcript` after the meeting.

**Who can read a meeting live.** The owner, the people the meeting is shared
with, and Admins; and only when the meeting's consent settings allow capture
(see [Meeting Consent](meeting-consent.md)). A meeting shared with you can be
read live although `get_meeting_transcript` and `list_meetings` show only the
meetings you own. A meeting that does not exist and one you cannot read get the
same answer, `Access denied to this meeting`.

While a meeting runs, `get_meeting_transcript` answers that it is still running
and, if live meeting context is on, to use `get_live_context`; after it ends and
before the transcript file is written, that the transcript is being prepared
(within two hours of the end, unless the transcript was deleted).

### `schedule_meeting`

- `meetingName`, `meetingPlatform` (Zoom/Teams/Chime/Webex/Google Meet), `meetingId`
  (numeric), `scheduledTime` (ISO 8601), `meetingPassword` (optional)
- `meetingPassword` is the passcode the invitation text shows (for example
  `Passcode: 1234 5678`), not the `pwd=` value of a join link. For Zoom,
  `meetingId` may instead be the full join link, which is the most reliable
  option; see [Zoom passcodes and join links](virtual-participant.md#zoom-passcodes-and-join-links)
  for what the tools accept and refuse
- `agenda` (optional) — the meeting agenda, one item per line, for example the
  agenda from the calendar invite. It is stored on the meeting record when the
  meeting starts; see [Meeting Agenda](meeting-agenda.md)

### `start_meeting_now`

- `meetingName`, `meetingPlatform`, `meetingId`, `meetingPassword` (optional), as for
  `schedule_meeting`
- `useStoredZoomCredentials` — default `true`; when the user has stored Zoom
  credentials in LMA, the VP signs in with them rather than joining as a guest
- `agenda` (optional) — as for `schedule_meeting`

### `get_virtual_participant_status`

- `virtualParticipantId` (required) — the id returned by `start_meeting_now`
- Poll every 5–10 seconds for ~2 minutes after `start_meeting_now` to detect
  `MANUAL_ACTION_REQUIRED` (CAPTCHA / 2FA / SSO) — surface the
  `manualActionMessage` and `virtualParticipantUrl` to the user so they can
  complete the challenge in the LMA viewer

---

## Troubleshooting

### Cannot connect to MCP server

**Quick Suite (OAuth):**
- Verify `EnableMCP=true` on the LMA stack
- Confirm the `MCPServerEndpoint` is reachable: `curl -I <MCPServerEndpoint>`
- Check that `MCPServerTokenURL` and `MCPServerAuthorizationURL` match your
  AWS region

### Tool calls return `403 Unable to determine the calling user`

LMA scopes every tool result to the calling user, so it answers a request that
does not resolve to a user with this error rather than serving it.

Switch the client to the per-user API key endpoint: take
`MCPServerApiKeyEndpoint` from the stack outputs, generate a personal key on the
**MCP Servers Configuration** page in the LMA UI (Hosted MCP Access tab), and
supply it as `Authorization: Bearer <key>` or `x-api-key: <key>`. The tools and
their behaviour are identical; only the endpoint and the credential change. Full
steps are in [Path B](#path-b-quick-desktop-api-key), and the two paths are
compared in
[MCP API Key Authentication › Connecting with a User Identity](mcp-api-key-auth.md#connecting-with-a-user-identity).
Whether Quick Suite itself exposes a field for a static key is the unverified
point noted under [Step 1](#step-1-gather-lma-mcp-server-configuration); Quick
Desktop, Claude Desktop and `curl` all work on this endpoint today.

**Quick Desktop (API key):**
- Confirm the endpoint URL is the **MCP API Endpoint** (the API Gateway URL),
  not the BedrockAgentCore Gateway URL
- Verify the API key hasn't been revoked (check **MCP Servers Configuration**
  in the LMA UI)
- Confirm the connection type is **REMOTE** and the key is supplied via an
  `x-api-key` header (or a Bearer-token field, if your Quick Desktop version
  offers one instead)

### Authentication failed / Invalid credentials

**Quick Suite:** Double-check Client ID, Client Secret, Token URL, and Auth
URL from CloudFormation outputs. OAuth scopes (`openid email profile`) are
pre-configured by LMA — no manual scope changes needed.

**Quick Desktop:** Regenerate the API key and try again. Old keys are
revoked when a new one is generated (one key per user).

### Access denied / Permission denied

LMA enforces UBAC:
- **Non-admin users** can only access their own meetings — verify the meeting
  `Owner` matches the authenticated user
- **Admin users** can access all meetings — verify the user is in the
  Cognito **Admin** group (matches the `AdminEmail` stack parameter)

### Tool not found / Unknown tool

- **Quick Suite:** Refresh the integration — **Integrations** → select LMA →
  **Actions** → **Refresh tools**
- **Quick Desktop:** Disconnect and reconnect the MCP integration to
  rediscover tools
- Verify the `MCPServerAnalytics` Lambda has the expected env vars
  (`CALLS_TABLE`, `TRANSCRIPT_KB_ID`, `MODEL_ARN`) and check
  `/LMA/lambda/MCPServerAnalytics` CloudWatch logs

### Operation timeout / HTTP 424

Quick Suite has a 60-second tool-call timeout. For large result sets:
- Reduce `maxResults` in search queries
- Use date filters to narrow `list_meetings` results
- Request specific meeting IDs rather than broad searches

The underlying Lambda has a 900-second timeout, so if Quick Suite times out
the call may still complete server-side — but Quick won't see the result.

### No meetings found / empty results

- Confirm meetings exist via the LMA UI
- For semantic search, allow 15–30 minutes after a meeting ends for
  Bedrock Knowledge Base indexing
- Verify date filters use ISO 8601 with explicit timezone
  (e.g. `2026-05-01T00:00:00Z`)
- Re-check UBAC scope (see "Access denied" above)

### Invalid meeting ID format

Meeting IDs in LMA are UUIDs (`abc123-def456-ghi789`). Get them from:
- LMA UI meetings list — copy the `CallId`
- The `list_meetings` tool response

For `schedule_meeting` / `start_meeting_now`, the `meetingId` is the
**platform's numeric meeting ID** (e.g. Zoom `123456789`), not LMA's UUID. For
Zoom it may also be the meeting's full join link.

### Virtual participant tools fail

- Confirm the Virtual Participant stack is deployed (look for
  `LMA-VIRTUALPARTICIPANTSTACK` in CloudFormation)
- Supported platforms (case-insensitive): `Zoom`, `Teams`, `Chime`, `Webex`, `Google Meet`
- Numeric meeting ID only — no platform prefixes (for Google Meet, the meeting code such as `abc-defg-hij`, or the meeting link)

### Custom HTTP headers not supported (Quick Suite)

Quick Suite does not support arbitrary custom headers. LMA's MCP server
uses standard OAuth bearer tokens, which are supported — no custom-header
configuration is required.

---

## Manual OAuth testing (Quick Suite)

Useful when debugging the OAuth path:

```bash
# 1. Get authorization code (open in browser)
https://<lma-domain>.auth.<region>.amazoncognito.com/oauth2/authorize\
?client_id=YOUR_CLIENT_ID\
&response_type=code\
&scope=openid+email+profile\
&redirect_uri=YOUR_CALLBACK_URL

# 2. Exchange the code for tokens
curl -X POST https://<lma-domain>.auth.<region>.amazoncognito.com/oauth2/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code" \
  -d "client_id=YOUR_CLIENT_ID" \
  -d "client_secret=YOUR_CLIENT_SECRET" \
  -d "code=AUTHORIZATION_CODE" \
  -d "redirect_uri=YOUR_CALLBACK_URL"

# 3. Test the MCP endpoint with the access token
curl -X POST https://<gateway-id>.bedrock-agentcore.<region>.amazonaws.com/mcp \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## Manual API-key testing (Quick Desktop)

```bash
./utilities/test-mcp-api-key.sh <your-api-key> [api-gateway-url]
```

Tests `initialize`, `tools/list`, `tools/call` (`list_meetings`), `ping`,
and bad-key rejection over both `Authorization: Bearer` and `x-api-key`
headers.

---

## Security Best Practices

- **Protect secrets** — never commit `MCPServerClientSecret` or LMA API keys
  to version control
- **HTTPS only** — both OAuth and MCP transport must be HTTPS
- **Rotate** — periodically rotate Cognito client secrets and API keys
- **Limit scope** — keep OAuth scopes at `openid email profile` (LMA default)
- **Revoke when leaked** — API keys can be revoked instantly from the LMA UI
- **UBAC enforcement** — non-admin users can only see their own meetings;
  the JWT `sub` claim or API key user context drives this on every call

---

## Reference

- [MCP Servers Overview](mcp-servers.md)
- [MCP API Key Authentication](mcp-api-key-auth.md) — full architecture for
  the API-key path
- [Quick Desktop: LMA Workflows](amazon-quick-desktop-integration.md) —
  conversational workflows and scheduled agent recipes for Quick Desktop
- [User-Based Access Control](user-based-access-control.md)
- [Model Context Protocol Specification](https://modelcontextprotocol.io/)
- [Amazon Quick Suite MCP Documentation](https://docs.aws.amazon.com/quicksuite/latest/userguide/mcp-integration.html)
