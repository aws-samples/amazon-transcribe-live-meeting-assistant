# LMA Integration Tests

End-to-end tests that run against a **live deployed LMA stack** (not mocks).
They validate the runtime surfaces that unit tests and static analysis can't
reach — in particular the areas touched by the dependency-upgrade batch
(PR #400): the Fastify 5 WebSocket transcriber, the AppSync GraphQL data plane,
and the Virtual Participant launch path (Zoom SDK 6).

## Prerequisites

1. A deployed LMA stack. To create a throwaway one from local code:
   ```bash
   AWS_PROFILE=default lma deploy --stack-name lma-integtest1 \
       --from-code . --admin-email you@example.com --wait
   ```
2. The LMA SDK + test deps installed in the repo venv:
   ```bash
   make setup-cli-dev
   # for the opt-in WebSocket streaming test only:
   .venv/bin/pip install -r integ-tests/requirements.txt
   ```
3. AWS credentials for the account the stack lives in (`AWS_PROFILE=default`).

## Running

```bash
# Default (read-only + self-cleaning VP registry lifecycle) — safe to repeat:
make integ-tests STACK=lma-integtest1

# Include a REAL Virtual Participant join into a live meeting (Zoom SDK 6 path):
make integ-tests-live STACK=lma-integtest1 PLATFORM=ZOOM \
    MEETING_ID=1234567890 MEETING_PASSWORD=secret

# Include the live WebSocket streaming test (needs a Cognito user's creds):
LMA_TEST_USERNAME=you@example.com LMA_TEST_PASSWORD=... \
    make integ-tests STACK=lma-integtest1

# Or take that user from a Secrets Manager secret instead of the environment
# ({"username": ..., "password": ...}) — how the scheduled run does it:
LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    make integ-tests STACK=lma-integtest1
```

### On a schedule

`make integ-tests-nightly` is what the `nightly_integ_tests` GitLab job runs. It
differs from `make integ-tests` in two ways that matter when nobody is watching:
a **skipped test fails the run** (`--no-skips`), so a lapsed secret or a dropped
dependency cannot report green having streamed no audio; and it writes a JUnit
report for the CI UI. It tests an existing stack and does not deploy one.

Afterwards the job summarises its JUnit report with `junit_summary.py` and posts
the result to Slack, on a pass as well as a failure.

See [Scheduled Integration Tests](../docs/scheduled-integration-tests.md) for the
one-time GitLab schedule, CI role, test-user and Slack-webhook setup it needs.

The machinery those behaviours rest on — `cognito_test_user.py`, the `--no-skips`
hook in `conftest.py`, and `junit_summary.py` — is unit-tested in
`test_ci_plumbing.py`, which needs no AWS and runs in the fast pipeline via
`make test-integ-plumbing`.

Or invoke pytest directly:
```bash
AWS_PROFILE=default .venv/bin/python -m pytest integ-tests/ \
    --stack-name lma-integtest1 -m "not live"
```

Stack resolution order: `--stack-name` / `STACK=` → `$LMA_STACK_NAME` → `LMA`.

## Access checks and security tests

`test_role_access.py` and `test_security_access.py` call the stack as throwaway
users: every role at every GraphQL operation, altered and expired tokens, one
meeting as its owner, a user it is shared with and an unrelated user, and the
transcriber, MCP, viewer, identity pool and sign-up entry points. They run in
the nightly suite, or alone with `make stacktest-rbac STACK=<name>`, and write
`scratch/api-test-results/report.md`. `make stacktest-zap` runs the OWASP ZAP
scan. What each checks, what they create and change, and how to read their
reports: [Security Testing](../docs/security-testing.md). Their helpers are
unit-tested in `test_security_helpers.py` (no AWS, `make test-integ-plumbing`).

## What each test covers

| Test | Validates | Batch risk it guards |
|------|-----------|----------------------|
| `test_stack_status_is_complete` | stack is in a `*_COMPLETE` (non-rollback) state | deploy sanity |
| `test_required_output_present[*]` | key CFN outputs exist & non-empty | UI/extension wiring |
| `test_transcriber_alb_targets_healthy` | transcriber ALB target group has a HEALTHY target (server passing `/health/check`) | **Fastify 3→5 boot** |
| `test_transcriber_ecs_service_running` | transcriber ECS service has running tasks, not crash-looping | **Fastify 5 runtime stability** |
| `test_appsync_reachable` | IAM-signed GraphQL query succeeds | AppSync data plane |
| `test_kds_pipeline_creates_meeting` | synthetic START on Kinesis → CallEventProcessor → meeting row in DynamoDB | **gql/AppSync introspection (the gql 4 regression)** |
| `test_meeting_events.py` *(module; every test in it)* | a temporary rule and SQS queue on the meeting event bus receive Meeting Started, Summary Ready and Meeting Ended for a synthetic meeting driven through Kinesis, and Virtual Participant Status Changed for a synthetic Virtual Participant record moved to `MANUAL_ACTION_REQUIRED`; every event carries only the contract's fields and no meeting content, and a write that leaves the status alone publishes nothing. Skips when `EnableMeetingEvents` is `false`. Self-cleaning | meeting lifecycle events (docs/meeting-events.md) |
| `test_meeting_chat_segment_is_stored_read_and_deleted` | a synthetic MEETING_CHAT segment on Kinesis is stored with an expiry, returned by `getTranscriptSegments`, left out of FetchTranscript, and removed by the transcript deletion function | meeting chat in the transcript (enum, processor gates, readers, deletion) |
| `test_synthetic_meeting_kit.py` | the synthetic meeting kit (`scripts/synthetic_meetings/`) replays its `retrospective` meeting through Kinesis at fast pace, left live: the meeting row has the script's title (as the start of its CallId), agenda, meeting context, Owner and `synthetic-meeting` label; every turn is stored as a final segment with the script's speaker, channel, text and times, and the chat message on `MEETING_CHAT`; the kit's `--cleanup` then leaves nothing of the meeting. No Cognito user, no model call | the kit other tests and manual testing drive meetings with; the START/segment shapes it shares with the Virtual Participant |
| `test_ws_stream_connects` *(opt-in)* | stream audio over the live `wss://.../api/v1/ws` and stay OPEN | **@fastify/websocket 11 upgrade ordering (the WS 500 regression)** |
| `test_vp_registry_lifecycle` | VP create → get → list → end via AppSync (no meeting) | VP CRUD surface |
| `test_vp_live_join` *(opt-in)* | VP actually joins a real meeting and leaves `INITIALIZING` | **@zoom/meetingsdk 4→6** |
| `test_vp_live_meeting.py` *(opt-in, `LMA_TEST_VP_LIVE_MEETING=1`)* | a VP launched as `lma vp create` launches one joins a real meeting nobody else attends, starts transcribing, and gets a meeting record; left alone it leaves by itself as `COMPLETED` (`attendees-left`); ended from LMA it shows the end reason and a clean status; the meeting record ends both times | the VP's join, its empty-meeting exit, and the end path, against a real platform |
| `test_meeting_preparation.py` | a VP scheduled a day ahead with labels, audience, meeting context and checklist stores them, and an edit before it starts changes them (one input over a limit refused; the VP is cancelled afterwards); a synthetic START carrying them creates a meeting that has them; a status set for a user survives a checklist replace and cannot be changed by the assistant; the transcript deletion function removes each evidence quote and keeps its segment id and time. With `LMA_TEST_VP_LIVE_MEETING=1` it also schedules a VP six minutes ahead, edits it, and checks the meeting it creates in the real meeting has the edits | the meeting preparation resolver, the VP create and scheduled-edit pipelines, the START path, and transcript deletion |
| `test_browser_extension.py` *(opt-in, `LMA_TEST_BROWSER_EXTENSION=1`)* | the extension the stack serves, loaded into headless Chromium (Playwright; `pip install -r lma-browser-extension-stack/e2e/requirements.txt` and `python -m playwright install --no-shell chromium`), signs in on the Cognito hosted page as a throwaway user from `scripts/ux_test_session.py` (deleted afterwards), captures a local stand-in meeting tab and a fake microphone, and the meeting it creates is owned by that user with transcript on both channels; the owner then continues it with `reopenMeeting` and a second session, whose segments land after the session's offset; the served manifest's version is one Chrome accepts. The meeting is deleted afterwards unless `LMA_TEST_KEEP_MEETING=1` | the extension's sign-in, tab and microphone capture, and continuing a meeting it created |
| `test_vp_video_setting.py` *(launch case opt-in, `LMA_TEST_VP_VIDEO_LAUNCH=1`)* | with Meeting Recording › Record Virtual Participant meeting video off (set in the active profile, put back afterwards): the setting read the Create Virtual Participant form makes answers false; a VP scheduled a day ahead that asks for video is stored with `videoRecordingOffReason` `configuration` (one that declines video is stored without one; both are cancelled); and a VP created while the setting is on, then launched against a meeting ID that does not exist after it is turned off, writes `configuration` itself when it starts (opt-in: a few minutes, and it places a VP task; the VP is ended afterwards) | the createVirtualParticipant pipeline's setting check, the form's read, and the VP's own check at start |
| `test_desktop_video.py` *(opt-in, `LMA_TEST_DESKTOP_VIDEO=1`)* | a narrated synthetic slideshow streamed as the desktop capture app streams it → one key frame per slide, at the right times, with both images in S3; the meeting assistant reads the last slide | desktop video key frames and the assistant's shared-screen tool |
| `test_browser_video.py` *(opt-in, `LMA_TEST_BROWSER_VIDEO=1`)* | with Meeting Recording and Meeting Consent changed for the run and put back afterwards: the slideshow streamed as the Stream Audio/Video page and as the browser extension (`clientType`) → the settings answer allows video, each stream is acknowledged with `VIDEO_STARTED`, one key frame per slide under each client's `Source`, a video recording and the capture source on the meeting; a meeting continued for a second session records each session's video and places its key frames after the first's; a meeting refused for its consent admits no video, a granted opt-in one does once the grant arrives, and an objection closes the video connection and keeps the video so far; with the setting off, `START_VIDEO` from either client is refused and the audio carries on | the transcriber's browser video admission, key frame source, consent and continued-meeting paths |
| `test_stream_page_video.py` *(opt-in, `LMA_TEST_STREAM_PAGE_VIDEO=1`; Chromium and a display)* | signs in to the web UI in Chromium, switches on **Also record screen video** on the Stream Audio/Video page and streams a canvas slideshow tab for 60 seconds through the page's own capture → the meeting has `CaptureSource` `STREAM_AUDIO`, a video recording, and about one key frame per slide under `Source` `STREAM_AUDIO`, with no slow-key-frame warning | the page's screen capture, MediaRecorder recipe and video connection |
| `test_stream_page_audio.py` *(opt-in, `LMA_TEST_STREAM_PAGE_AUDIO=1`; Chromium and a display)* | signs in to the web UI in Chromium as a throwaway user (deleted afterwards), leaves **Also record screen video** off, and streams a stand-in meeting tab (one channel of a recorded call) and a fake microphone (the other) for about 30 seconds through the Stream Audio/Video page → the START names `STREAM_AUDIO` and no `START_VIDEO` is sent, transcript segments arrive on both channels (`LOCAL` and `REMOTE`), and the meeting ends with an audio recording and no video; a second meeting in Notify mode shows the notice, and the meeting's consent record keeps the notice the page showed. With Meeting Recording › Record meeting audio and Meeting Consent changed for the run and put back afterwards; the meetings are deleted unless `LMA_TEST_KEEP_MEETING=1` | the page's tab and microphone capture, its audio-only start, and its Notify consent step |
| `test_meeting_consent.py` *(opt-in, `LMA_TEST_CONSENT=1`)* | with the Meeting Consent setting changed for the run and put back afterwards: an unconfirmed opt-in stream is refused and captures nothing; an attested one is transcribed, with its attestation, evidence image and evidence check stored; a notify stream stops, and is recorded as objected and ended, on an objection | the transcriber's consent gate on the Stream Audio/Video path |
| `test_hosting_presets.py` | the `team-hosted-external` and `customer-account` Managed profiles hold every value their presets set; synthetic meetings with a unique label, owned by a temporary user and by another owner, one with another label value and one still in progress, are found by `lma meetings delete` run through the CLI as the temporary user (their own) and as a temporary Admin (every user's); the dry run deletes nothing, the meeting in progress is skipped, and `--confirm` removes each matching meeting's record, list entry, transcript items and files in the recordings bucket and nothing else (the two temporary Cognito users, created with no password and no invitation message, in the default run, and the meetings are removed afterwards). With `LMA_TEST_HOSTING_ASSISTANT=1` it also sets **Participant use: who may ask** to Off for the run (put back afterwards) and checks a meeting-chat request is refused while the meeting page's is answered | the configuration seeder's Managed profiles; the SDK's meeting search and the per-meeting delete path; the meeting assistant's requester check |
| `test_meeting_data_point_in_time_recovery.py` | read-only: each meeting content table of the AI stack (meetings, Virtual Participant, Virtual Participant task registry, troubleshooting jobs, and voice enrollment where deployed) reports point-in-time recovery off when `MeetingDataPointInTimeRecoveryDays` is 0 and on with that recovery period otherwise, and the configuration table, the voice consent log and the skills table keep 35 days (skips on a stack that predates the parameter) | the `MeetingDataPointInTimeRecoveryDays` wiring from the main stack to the tables |
| `test_meeting_participants.py` | four throwaway users (an Admin, the owning User, a second User and a Participant), created with a permanent password and no invitation message, sign in with SRP; a START with a participant list, owned by the first User, creates a meeting that carries the list; `getMeetingParticipantAccounts`, as the owner, answers Admin, User, Participant and none; the second User is refused by the resolvers and the Participant by AppSync for all three participant list operations; `createParticipantAccounts`, as the owner, creates a Participant account (each run sends one Cognito invitation, to a throwaway `example.invalid` address, to the stack's allowed domain, or to `LMA_TEST_PARTICIPANT_EMAIL` with a per-run `+tag`) for the one listed address without one, answers `exists` when asked again and refuses an unlisted address; `updateCallParticipants` replaces the list, and a person added to the existing meeting's list is shared with by Meeting Sharing; named by `VirtualParticipantId`, it changes a never-launched Virtual Participant's own list, then, once its row names the meeting, the meeting's list; an Admin's `createUser` with `inviteToMeeting` adds the new address to the list (a second Cognito invitation per run, to an address chosen the same way); a scheduled Virtual Participant's list is read before its meeting exists (the users, the created accounts, the meeting, its list row, the daily count and the Virtual Participant row are removed afterwards; needs `pycognito`) | the participant list from the VP's START through the call event processor; the meeting preparation and user management resolvers' owner, list and Participant-only checks |
| `test_participant_access.py` | three throwaway users (the owning User, a Participant on the meeting's participant list and one not on it), created with a permanent password and no invitation message, sign in with SRP; a START with a participant list creates a meeting, whose transcript is seeded through the call data stream (a spoken line and a meeting chat line) and which is given an owner-only meeting context, label and meeting assistant answer directly; Meeting Sharing shares it with the listed Participant (recorded in `AutoSharedWith`), not the owner; `getParticipantMeeting` and `getParticipantMeetingTranscript`, as the listed Participant, answer the view's fields only, without the owner-only material, and `listParticipantMeetings` lists the meeting; `onAddParticipantTranscriptSegment` delivers a new line and refuses a selection beyond the view and a Participant the meeting is not shared with; the unlisted Participant is refused every view operation, both Participants the member operations at the field, and the owner the view at the field; a share saved through `shareMeetings` on condition of the list read gives every transcript line (forty more seeded) the new list and clears the meeting's `SharingFanout` marker, and one whose expected list is stale is refused with `SharingListChanged` and changes nothing; a share made by hand stays when the listed Participant is taken off the list, whose own share goes (the users, the meeting record, its list row and transcript items are removed afterwards; needs `pycognito` and `websockets`) | Meeting Sharing from the meetings table's stream through the meeting controls function; the Participant view resolver, its allowlist and its sharing check; the Participant transcript subscription's sharing filter; the share dialog's conditional save and its item fan-out |
| `test_account_names.py` | five throwaway users (an Admin, the owning User and three Participants listed on the meeting by address only, with no names), created with a permanent password and no invitation message, sign in with SRP; a Participant sets their own display name and short name (`updateMyAccountNames`, read back from `listMyAdvanceConsents`) and is refused six short names, the Admin sets another Participant's (`setUserAccountNames`), and the third sets a short name the second also goes by; the owner finds the first by the start of their display name, the second by a short name in another case and the third by the start of their address (`searchAccounts`: each with display name and address only), the middle of a name finds nothing, two characters are refused, and a Participant is refused at the field; with the runner's credentials an assumed role (the consent API's backend role), each Participant gives a consent for the meeting, its opt-in consent is started, an attendee showing the shared short name is not matched, and attendees shown by the first one's display name and the second one's own short name are recorded as consented under their consents (`MatchBasis` `display_name`, `MatchedNameSource` `display_name` and `short_name`, no address on the record), which allows capture (the names are cleared and the users, their consents, the names and their search entries, the meeting record and its list row are removed afterwards, by key; needs `pycognito`). The matching part is skipped where Meeting Consent › Allow consent given in advance is off | the names accounts go by in meetings (advance consent resolver and user management), the account search, and the meeting consent resolver's matching by those names |
| `test_admin_consents.py` | throwaway users (an Admin, the owning User and three Participants), created with a permanent password and no invitation message, sign in with SRP; each Participant gives a consent for every meeting on the deployment and the first sets a display name; a START lists the first Participant on a meeting; the Admin finds each consent by the start of the address with the display name (`listAllAdvanceConsents`), filters by scope and state, and pages with `NextToken`; the three admin operations are refused at the field to the User and a Participant; with the runner's credentials an assumed role, the meeting counts the Participant's consent for it, the Admin withdraws it with a note (`withdrawAdvanceConsentAsAdmin`), the Participant's My consents shows `withdrawn_by_admin` and the note, and the meeting records an objection within a minute; the Admin erases the first Participant's consent records (`deleteAdvanceConsentRecords`): none is left, and the audit entry names the consent ids and count without their content; deleting the second Participant keeps their consent, withdrawn (`account_deleted`), deleting the third with `deleteConsentRecords` removes theirs with an audit entry, and an account created again with the second one's address sees none of the earlier account's consents (the users, every item under their addresses, the audit entries and the meeting record and list row are removed afterwards; needs `pycognito`). Skipped where Meeting Consent › Allow consent given in advance is off | the advance consent resolver's Admin operations, the `TypeDateIndex` entries of consents, user management's erasure on deletion, and the account binding of consents |
| `test_pre_meeting_consent.py` | three throwaway users (the owning User, a Participant on the meeting's participant list and one not on it), created with a permanent password and no invitation message, sign in with SRP; a START with a participant list creates a meeting, given a label directly; the listed Participant reads the meeting's consent page (`getAdvanceConsentInvitation`: the meeting, its organizer, its labels and the exact text with its SHA-256), is refused a consent for any other text, gives a consent for the meeting (stored under their address with their account's sub, the scope, the times, the text and no device details unless the setting keeps them), is refused a standing consent beyond the deployment's longest period, gives one for the organizer's meetings with the label until a date 30 days ahead, lists it on My consents and withdraws it (the record stays, marked withdrawn, and no longer covers the meeting), while the other Participant can neither see nor withdraw it; the unlisted Participant is told only that they are not listed, is refused a consent, and asks to be added, which publishes one `participant_list_request` notification on the meeting that the owner reads, and is not published again when asked again; a request kept on a never-launched Virtual Participant before its meeting exists is published to the meeting by the START that creates it, and the deployed `getVirtualParticipant` resolver, the read that START makes, is a consistent read; nothing is stored about the unlisted Participant's consent; with the runner's credentials an assumed role (the consent API's backend role, as the Virtual Participant's), the meeting's opt-in consent is started and the attendee listed by a display name matching the list, which records the consent given in advance for them (source `pre_meeting`, basis `display_name`) and allows capture, an unlisted attendee withholds it again, and withdrawing the consent records an objection on the meeting within a minute; last, the unlisted Participant, with no meeting named, gives a consent for every meeting on the deployment until a date (refused past the longest period) and one for the owner's meetings with the label by the address typed, sees both on My consents and withdraws one, which the other Participant cannot (the users, their consents, the request, its daily count, the meeting record, its list row and its notifications are removed afterwards, by key; needs `pycognito`). Skipped where Meeting Consent › Allow consent given in advance is off | the advance consent resolver's list check, text hash check, period and label checks, standing consents given with no link, own-records rule and request to be added; the meeting notification it publishes |
| `test_user_last_sign_in.py` | a temporary Admin and a temporary user, created with a permanent password and no invitation message, each sign in with SRP as the web UI does; `listUsers` (as the Admin) returns a last sign-in time for both from this run, with their roles, and `lma users list --format json` and `--format csv`, run through the CLI, list the same times; `deleteUser` removes the user's sign-in item with the user (both users and their items are removed afterwards; needs `pycognito`) | the Cognito stack's PostAuthentication trigger and sign-in table; the user management resolver's last sign-in join and delete; the SDK's `client.users` |
| `test_user_last_active.py` | a temporary Admin signs in with SRP; the sign-in table has a last active time from the sign-in; with it moved an hour back, a refresh-token renewal sets it again, and `listUsers` and `lma users list --format json` report that time; with it moved back again, one MCP call with a newly generated API key sets it again and `listMCPApiKeys` reports the key's `lastUsedAt` (the MCP part skips on a stack without the API key endpoint; the user, their item and the key are removed afterwards; needs `pycognito`) | the pre token generation trigger's last active write; the MCP API key authorizer's last use writes; the user management resolver's and the key manager's new fields; the SDK's `client.users` |
| `test_configuration_profile_log.py` *(part of the default run, not opt-in: it changes the deployment's active profile for a few seconds, to a copy holding the same configuration)* | a temporary Admin, created with a permanent password and no invitation message, signs in with SRP and, through the GraphQL API, copies the active profile, activates the copy, activates the original again and deletes the copy; `listConfigurationProfiles` reports the copy's activation (`ActivatedBy`, `ActivatedAt`) and the profile log's newest entries (the activation, then the reactivation and the deletion) naming the Admin's email address. The copy holds the same configuration as the profile it replaces; the original is reactivated, the copy deleted and the Admin removed afterwards (needs `pycognito`) | the configuration resolver's activate and delete paths and the store's profile log |
| `test_minimised_content_preset.py` *(about five minutes, three model calls)* | the `minimised-content` Managed profile holds every value its preset sets, **Meeting Assistant › Leave out sensitive personal information** on among them; the default and Managed profiles hold the `minimised-content` summary profile, every section carrying the rule; *model-dependent* (marker `model_dependent`): a synthetic meeting started with that profile, in which a participant names a health condition and its treatment as the reason for an action item, is summarized with its sections and the summary names neither (a failure prints the sections). Changes no deployment-wide setting; removes its meeting, transcript items and files | the configuration seeder's Managed profiles and shipped summary profiles; the summarizer's profile selection; the model following the profile's instruction |
| `test_assistant_profile.py` *(opt-in, `LMA_TEST_ASSISTANT_PROFILE=1`)* | with the Meeting Assistant section changed for the run and put back afterwards: the profile's assistant instructions shape an answer; under Selected tools, an installed MCP server flagged off for in-meeting requests is not offered to a meeting-chat request and is to the owner's (read from the assistant's log); the MCP server library export holds no credential, re-importing it changes no server, and an imported server is created waiting for its credentials (its probe row removed afterwards). Needs an installed, active MCP server for the per-server check | the profile's assistant instructions and tool selection; the MCP server library export and import |
| `test_assistant_skills.py` *(opt-in, `LMA_TEST_ASSISTANT_SKILLS=1`)* | creates a probe skill with `client.skills` (deleted afterwards) and, with the Meeting Assistant section changed for the run and put back afterwards: an answer to a question its description matches follows its instructions, which the assistant has to load; under Selected tools with the skills switch on, the skill flagged off for in-meeting requests is not offered to a meeting-chat request and is to the owner's (read from the assistant's `Skills: offered=` log line); the skills shipped with LMA, with no per-skill entries, are not offered to a meeting-chat request while the probe skill is, and are offered to the owner's; the probe skill's library entry, imported back, is found unchanged and not written | the meeting assistant's skills: progressive disclosure, the per-skill selection, the default skills' Participant use default, and the skill library export and import |
| `test_default_skills.py` | read-only: the five default skills the configuration seeder stores are listed by the skills API as shipped with LMA, and one's instructions read back. It expects the stack's default skills as the seeder left them: on a stack where an Admin deleted, renamed or replaced one, it fails by design | the default skills' seeding (`configuration_seeder/skill_seeder.py`), the seeder's access to the skills table |
| `test_mcp_api_key_logging.py` *(needs a Cognito test user)* | generates an MCP API key as the test user, calls the API-key MCP endpoint with it (`x-api-key` and `Authorization: Bearer`) and with an unknown key, revokes it; the authorizer log names the matched key by its stored prefix and holds neither key, AppSync logging excludes request content, and its log for the `generateMCPApiKey` call holds no key and no resolver context or template; the answers to the accepted and the unknown key carry the endpoint's security headers | what the MCP API key path writes to CloudWatch Logs |
| `test_response_headers.py` | read-only, no credentials: the web UI's Content-Security-Policy (scripts from its own origin, no whole-scheme sources, and `connect-src` admitting this stack's transcriber, AppSync hosts and regional services but not another Region's), its other security headers and `Server: CloudFront`, the chat frame served with the same policy, and API Gateway's refusal of a keyless MCP call carrying the endpoint's security headers | `SecurityHeadersPolicy` on the distribution; the MCP API's gateway responses |
| `test_meeting_export.py` *(needs a Cognito test user, about a dozen model calls)* | a synthetic meeting started with the `discovery-session` summary profile and a checklist, with a short conversation and a chat link, is summarised at its end with that profile's sections and structured action items whose owners were named in it; `get_meeting_export` matches `docs/schemas/meeting-export-v1.schema.json` (participants, links, summary, action items, `include`); `list_summary_profiles` lists the shipped profiles, `update_meeting_metadata` sets and refuses a profile, `regenerate_meeting_summary` with `healthcare` writes that profile and clears the action items; Download > Meeting export (.json) gives one JSON file linked for 600 seconds; a meeting with more than 6 MB of transcript is answered with a link of at most ten minutes (opt-in with `LMA_TEST_LARGE_EXPORT=1`, which the scheduled `nightly_integ_tests` job sets); after the transcript deletion function runs, the export leaves the transcript out; a spoken consent reply put through the call data stream and recorded with its transcript line by the backend role (an assumed role; skipped otherwise) gives that speaker's `speakerId` and `granted` in `get_meeting_consent` and the export, a reply under another name or typed in the chat is not linked, a rename keeps it, and after transcript deletion the link is kept without a speaker; lines carrying a platform participant id and a chat reply linked by that id give that speaker's `speakerId` and `granted`, a reply with an id no line of that speaker carries is not linked, neither answer holds the id, and transcript deletion removes it from the consent record; another user's meeting is refused. Set `LMA_TEST_MEETING_EXPORT_OUT` to keep the export. Touches only its own meetings and revokes its key | summary profiles, the summariser's structured action items, the meeting archive function's export, the MCP export, consent and profile tools, consent replies linked to their speaker |
| `test_mcp_meeting_tools.py` *(needs a Cognito test user)* | creates a synthetic meeting owned by the test user; a read-scope key is offered only read tools and refused `push_meeting_guidance` and `delete_meeting` with nothing changed; with a read-write key, `get_meeting_transcript` (`format: json`) returns each segment once across its `nextToken` pages, pushed guidance is returned by `list_meeting_guidance` and, as a `guidance` notification, by `list_meeting_notifications`, the agenda, context, checklist (an item status set as the user's survives a replace), labels and audience are written and read back, `list_meetings` finds the meeting only when all its labels match, consent reads `off`, `share_meeting` adds and removes, `get_meeting_record` returns a recording link signed for 3600 seconds that downloads, and `delete_meeting` changes nothing without `confirm` and with it deletes the meeting and its guidance; Virtual Participants scheduled two days ahead (one with meeting preparation) are changed with `update_scheduled_meeting` and cancelled with `end_virtual_participant`, the second call changing nothing; touches only that meeting and revokes each key | the hosted MCP server's key scopes and meeting tools |
| `test_meeting_notifications.py` *(needs a Cognito test user)* | creates a synthetic meeting owned by a synthetic user and publishes to it over IAM with `lma_notifications.publish_meeting_notification`; the test user is refused before the meeting is shared with them, reads both earlier notifications once it is shared through the meeting controls function, and is refused again after the share is removed; a second publish with the same DedupKey updates the first; the test user's dismissals and mutes are kept (an every-meeting mute is put back); the 121st publish in a minute is refused; the transcript deletion function removes the guidance only, and deleting the meeting removes every notification row; over the AppSync real-time endpoint, with the web UI's own subscription for the meeting and for every meeting, a publish reaches the test user on the shared meeting and on a second meeting the test user owns; where Playwright's Chromium is installed, the web UI signed in as the test user raises the top navigation's count for a publish while the meeting list is open, and opens the Notifications drawer for the owned meeting's first notification once its page is opened by a change of route; touches only those two meetings | meeting notifications: access from the meeting's current sharing, live delivery to the web UI, dedup, the rate limit and retention |
| `test_live_guidance.py` *(needs a Cognito test user)* | creates two synthetic meetings owned by the test user with the recorded 8-item checklist (one item set by a user) and sends both the recorded transcript in two waves 100 seconds apart: the meeting with `GuidanceEnabled: true` gets at least one run, a summary on its state row and a *LiveGuidance* metering record, an item marked covered or partial by the assistant with evidence from its own segments, guidance of source `live-guidance` that cites a checklist item, and a digest (`requestMeetingDigest` as the test user); the user-set item is unchanged; the control meeting with `GuidanceEnabled: false` gets no state row, no metering and no guidance. Prints the guidance produced. Takes about five minutes, makes a few model calls, and touches only those meetings | live guidance: off by default per meeting, the checklist update path, publishing, the digest and metering |
| `test_meeting_whiteboards.py` *(needs a Cognito test user)* | creates two synthetic meetings owned by the test user and sends both a recorded planning conversation in three waves 200 seconds apart: the meeting with `WhiteboardsEnabled: true` gets the default whiteboard made and filled by the assistant and a *MeetingWhiteboards* metering record; a change from a stale version is refused; the Decisions panel the test user rewrites between the first and second runs is unchanged by the second run and stays the user's; versions carry their source; a read key is offered only the two read tools and a read-write key all four; `get_meeting_whiteboard` with `ifNewerThan` answers `notModified`; the export holds the whiteboards; the transcript deletion removes every whiteboard row. The control meeting with `WhiteboardsEnabled: false` gets a whiteboard made by hand, no state row and no metering, and a Project plan (Gantt chart) whiteboard that starts with its three panels and takes a Gantt chart in its Plan panel, with a diagram style that fetches anything refused. Prints what the whiteboards held. Takes about twelve minutes, makes a few model calls, and touches only those meetings | meeting whiteboards: manual boards, the assistant's runs, user edits win, versions, MCP, the export and deletion with the transcript |
| `test_assistant_whiteboards.py` *(needs a Cognito test user; about three minutes, four meeting assistant answers)* | creates a synthetic meeting owned by the test user with the recorded planning conversation, plus a spoken line and a meeting chat message that ask the assistant to write into the whiteboards, and asks the meeting assistant four questions the way the meeting page's chat does (the hosted MCP server's `ask_meeting_assistant`, read-write key): a summary, after which the meeting still has no whiteboard and nothing the injected lines asked for appears; a comparison whiteboard of the pricing options, made by the test user, its table filled; a Gantt chart of the launch plan (a `gantt` diagram with `dateFormat`); and a question added to an Open questions panel of the comparison whiteboard, the table kept. Prints what the whiteboards held. Touches only that meeting | the meeting assistant's whiteboard tools: making, filling and changing whiteboards on request, and meeting content treated as data |
| `test_whiteboard_links.py` *(opt-in, `LMA_TEST_WHITEBOARD_LINKS=1`; needs a Cognito test user and Playwright's Chromium; about two minutes, no model call)* | publishes a guidance item to a synthetic meeting owned by the test user (`push_meeting_guidance`, read-write key) and drives its page in headless Chromium: the guidance card has **Ask the assistant** and **Add to whiteboard**; Ask the assistant shows the card above the assistant's composer; Add to whiteboard makes a whiteboard whose "From guidance" panel holds the card's title and text, the test user's; the panel has **Ask the assistant about**. Touches only that meeting | the meeting page's links between guidance, the assistant chat and whiteboards |

`test_vp_live_join` is marked `live` and **skipped** unless `--vp-meeting-id`
(`MEETING_ID=`) is provided.

`test_kds_pipeline_creates_meeting` puts a synthetic `START` event on the
CallDataStream (empty AccessToken → the processor uses `AgentId` as Owner, the
documented Virtual-Participant service-call path) and polls the EventSourcing
DynamoDB table for the resulting call record, then deletes it. It is the
regression guard for the gql 4 → AppSync incompatibility: the CallEventProcessor
introspects the AppSync schema to build its DSL mutations, gql 4's introspection
query is rejected by AppSync, so `createCall` never ran and meetings never
appeared in the UI. The probe is reusable standalone:
`AWS_PROFILE=default python integ-tests/kds_pipeline_probe.py <stack-name>`.

## Notes

- `/health/check` is the **ALB target-group** health path and is deliberately
  NOT routed through the public CloudFront distribution (which only forwards
  `/api/v1/ws`), so it can't be probed over the internet. The transcriber health
  test therefore asserts on ALB **target health** (via `elbv2`), not an HTTP GET.
- The default run is **idempotent and self-cleaning**: the VP registry test ends
  every row it creates in a `finally` block.
- These are intentionally dependency-light (stdlib `urllib` + `boto3`/`lma_sdk`),
  so they add no new packages to the repo.
- For the manual/observational Zoom checklist (active-speaker, chat commands,
  DOM selectors), see `docs/dependency-upgrade-validation-runbook.md`.
- To drive a realistic meeting into a stack by hand (sprint planning, an
  architecture review, an incident review and others, through Kinesis or as real
  audio over the WebSocket), use the synthetic meeting kit in
  [`scripts/synthetic_meetings/`](../scripts/synthetic_meetings/README.md).
  `test_synthetic_meeting_kit.py` is its live check.

## Voice enrollment consent lifecycle

`test_voice_enrollment_consent_lifecycle` grants consent, enrolls a voice from
Polly-synthesised speech, and withdraws — then reads DynamoDB to check what was
actually stored, because the stored records are what the consent terms promise.
It exists as an integration test rather than more unit tests precisely because
no amount of review can establish that a consent row lands in the table, that a
MicroVM really computes a voiceprint from real audio, or that a withdrawal
really destroys it.

Requires `LMA_TEST_USERNAME` / `LMA_TEST_PASSWORD` like the streaming probe, and
a stack with `EnableVoiceEnrollment=true` **and** the ASR MicroVM engine deployed
(`EnableMicrovmAsr=true`, the default, in a Region with AWS Lambda MicroVMs) —
enrolling needs that engine, because that is what computes speaker
embeddings. It skips cleanly on a deployment without the feature, which is the
default. Self-cleaning: it withdraws at the end, which deletes the voiceprint it
created.

## Desktop video key frames

`test_desktop_video.py` streams a short narrated slideshow into the stack the
way the desktop capture app does, and checks that the transcriber keeps one key
frame per slide. It is opt-in, because it takes about two minutes and calls
Polly and Bedrock:

```bash
.venv/bin/pip install -r integ-tests/requirements.txt -r integ-tests/requirements-video.txt
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    make integ-tests STACK=lma-integtest1 LMA_TEST_DESKTOP_VIDEO=1

# Only this file:
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user LMA_TEST_DESKTOP_VIDEO=1 \
    .venv/bin/python -m pytest integ-tests/test_desktop_video.py --stack-name lma-integtest1 -s
```

`desktop_video_probe.py` builds the media and does the streaming:

- **Slides.** Five 1920 × 1080 slides with large, distinct text, rendered with
  Pillow, each held eight seconds. Polly (Matthew, neural) narrates each one on
  the left (meeting) channel; the microphone channel is silent. The last slide,
  "Launch codename: BLUE HERON / Ship date: March 14", is never spoken, so an
  answer that quotes it can only come from the stored picture.
- **Encoding.** As `VideoCapture.swift` and `VideoCapture.cs` encode: H.264 Main
  at 5 fps and about 1.5 Mbit/s, a key frame every two seconds, as a fragmented
  MP4 (init segment, then one `moof`/`mdat` fragment per two seconds). The ffmpeg
  is `LMA_TEST_FFMPEG`, else `ffmpeg` on PATH, else the one `imageio-ffmpeg`
  bundles; it needs libx264.
- **Streaming.** The narration goes over the Stream Audio/Video socket in real time,
  as `ws_stream_probe.py` sends it. Half a second in, a second socket to the
  same endpoint, with the same token, sends `START_VIDEO` with
  `videoTimeOffsetMs: 1000`, then each fragment when the app's encoder would
  have finished it (the init segment with the first), then `END_VIDEO`, as
  `VideoSocket.swift` and `VideoSocket.cs` do. The audio `END` follows.

What it asserts:

| Test | Asserts |
|------|---------|
| `test_desktop_video_streams_alongside_audio` | both sockets stayed open until their end events, every fragment was sent, and the meeting has transcript segments |
| `test_desktop_video_keeps_one_key_frame_per_slide` | exactly five `kf#<CallId>` items, all `Source=DESKTOP_CAPTURE`, timestamps strictly increasing and each within 2.5 s of its slide's start on the transcript's clock (1, 9, 17, 25 and 33 s with the current narration), each with a `VideoOffset` |
| `test_desktop_video_key_frame_images_are_stored` | both JPEGs of every frame exist in the recordings bucket, under `lma-video-keyframes/<meeting>/` |
| `test_assistant_reads_the_last_slide` | invokes `STRANDS-<stack>-MeetingAssist` with "What did the last slide say?" and expects "heron" in the answer. Skips, even with the flag, when the deployed function's package has no `meeting_frames.py`: the shared-screen tool comes with pull request #891, which is not merged yet |

With the flag set, a missing dependency, ffmpeg or test user fails the run
rather than skipping. Without it the four tests are reported as skipped,
including under `make integ-tests-nightly`, because they carry the
`optional_feature` marker. Zero key frames usually means the transcriber image
predates desktop key frames or the stack has `EnableVideoRecording=false`.
`--media-only` (below) prints the key frame times of the generated video, which
are where the slides should land, offset by one second.

The meeting (`desktop-video-probe-<UTC time>`) is not deleted: it expires with
the deployment's transcript retention, and its Shared screen strip can be
looked at on the meeting page. To build only the media, for a look at the slides
and the fragment sizes, without a stack:
`AWS_PROFILE=default python integ-tests/desktop_video_probe.py --region us-west-2 --media-only /tmp/slides`
(this calls Polly).

## Browser video

`test_browser_video.py` streams the same slideshow as the Stream Audio/Video
page and the browser extension send screen video: the same second connection
as the desktop app, with the START's `clientType` naming the client
(`browser_video_probe.py`). It is opt-in, because it takes about eight minutes,
calls Polly, and changes two deployment-wide settings for the run:

```bash
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    make integ-tests STACK=lma-integtest1 LMA_TEST_BROWSER_VIDEO=1
```

It turns **Meeting Recording › Record screen video from streaming clients** on
and Meeting Consent off, and later to opt-in and notify, on the active profile
(in a temporary Custom copy of it when the active profile is read-only, as
`test_meeting_consent.py` does), and puts both sections back afterwards. It
needs a stack with `EnableVideoRecording=true`; on one without it the first
fixture fails, saying so. The two continued-meeting tests call `reopenMeeting`
through the SDK, which accepts an assumed role, and skip with an IAM user's
keys. The meetings (`browser-video-probe-<leg>-<UTC time>`) are not deleted;
they expire with the deployment's retention.

The browser clients' own capture is covered separately: `test_stream_page_video.py`
(opt-in, `LMA_TEST_STREAM_PAGE_VIDEO=1`) signs in to the web UI in Chromium under a
display (`xvfb-run -a` on a machine without one), switches on **Also record
screen video** on the Stream Audio/Video page, and streams a canvas slideshow tab
through the page's own getDisplayMedia and MediaRecorder for 60 seconds
(`stream_page_probe.py`). It needs `playwright` from `requirements-video.txt` and
`python -m playwright install chromium`.

The page's audio-only path has its own test, `test_stream_page_audio.py`
(opt-in, `LMA_TEST_STREAM_PAGE_AUDIO=1`, with the same Chromium, display and
`requirements-video.txt` needs). It signs in as a throwaway user from
`scripts/ux_test_session.py` (deleted afterwards), shares a local stand-in
meeting tab that plays one channel of a recorded call while Chromium's fake
microphone plays the other, leaves screen video off, and streams for about 30
seconds. It turns **Meeting Recording › Record meeting audio** on for the run
and sets the Meeting Consent mode, first off and then Notify, on the active
profile (in a temporary Custom copy when the active profile is read-only), and
puts both sections back afterwards:

```bash
AWS_PROFILE=default xvfb-run -a \
    make integ-tests STACK=lma-integtest1 LMA_TEST_STREAM_PAGE_AUDIO=1
```

The meetings are deleted afterwards unless `LMA_TEST_KEEP_MEETING=1`.

## Transcript deletion after the summary

`test_transcript_deletion.py` checks that a meeting's transcript is deleted once
it is summarized (see [Transcript and Recording Deletion After the Summary](../docs/transcript-deletion.md)).
It is opt-in: while it runs, it sets the deployment's Retention › "Delete the
transcript after the summary" to Immediately, so **every meeting that ends on
the stack in that time has its transcript deleted**. It streams one meeting,
waits for its summary (up to five minutes), and checks the segments, the
transcript file's versions, the meeting record's deletion fields and the
scheduled sweep. The setting is saved first and put back in the module's
teardown.

```bash
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user LMA_TEST_TRANSCRIPT_DELETION=1 \
    .venv/bin/python -m pytest integ-tests/test_transcript_deletion.py --stack-name lma-integtest1 -s
```

## Recording deletion after the summary

`test_recording_deletion.py` checks that a meeting's audio and video recordings
are deleted once it is summarized, and that its key frames are kept (see the
same page). It is opt-in: while it runs, it sets the deployment's Retention ›
"Delete the recordings after the summary" to Immediately, so **every meeting
summarized on the stack in that time has its recordings deleted**, and turns
Meeting Recording's audio and screen video on if they are off. It streams one
meeting with audio and a slideshow as screen video, as
`test_desktop_video.py` does (so it needs ffmpeg, the packages in
`requirements-video.txt` and `EnableVideoRecording=true`), waits for its
summary, and checks that no version of its recordings is left in the bucket,
including one stored after the deletion ran, that the meeting record links to
none and records the deletion, and that the key frames, their images, the
summary and (while its own setting is off) the transcript are kept. Both
settings are saved first and put back in the module's teardown.

```bash
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user LMA_TEST_RECORDING_DELETION=1 \
    .venv/bin/python -m pytest integ-tests/test_recording_deletion.py --stack-name lma-integtest1 -s
```

## A meeting archive (Download > Everything)

`test_meeting_archive.py` writes a synthetic meeting of its own (a meeting
record with a summary, one transcript segment and one key frame with its
picture), asks for its archive with `prepareMeetingArchive`, reads
`getMeetingArchive` until it is ready, downloads the ZIP through the signed link
and checks its files and `README.md` manifest. It does this for an Everything
archive and, for a second synthetic meeting, a Key frames archive, which holds
only the key frame and `README.md`. It deletes the meetings' items, the key
frames and the archives afterwards. It is opt-in, and needs an assumed
role, which AppSync treats as the backend.

```bash
AWS_PROFILE=default LMA_TEST_MEETING_ARCHIVE=1 \
    .venv/bin/python -m pytest integ-tests/test_meeting_archive.py --stack-name lma-integtest1 -s
```

## A Virtual Participant in a Google Meet meeting

`test_meet_end_to_end.py` starts a scripted guest in a real Google Meet meeting
(`scripts/meeting_guest`) and sends a Virtual Participant to it; the guest then
speaks a short two-voice conversation and sends a chat message with a marker.
It passes when the speech and the message reach the meeting's transcript.
About ten minutes; it calls Amazon Polly, Amazon Transcribe and Bedrock.

It needs `LMA_TEST_MEET_URL`, the link of a meeting with open access that
admits guests with nobody hosting it (see
[Scheduled Integration Tests](../docs/scheduled-integration-tests.md#optional-a-google-meet-meeting)).
Without it, or when Google Meet turns the guest away after its retries, the
test skips.

```bash
AWS_PROFILE=default LMA_TEST_MEET_URL=https://meet.google.com/abc-defg-hij \
    .venv/bin/python -m pytest integ-tests/test_meet_end_to_end.py --stack-name lma-integtest1 -s
```

## A Virtual Participant in a live meeting, with nobody else there

`test_vp_live_meeting.py` needs no scripted guest. It launches a Virtual
Participant with `client.vp.create`, the path `lma vp create` uses, into a real
meeting that nobody else attends, and runs two tests with one VP each. In both,
the VP must be admitted, start transcribing (`ACTIVE`), and get a meeting record
`c#<CallId>`. In the first, the VP is left alone and must leave by itself, as
`COMPLETED` with the `attendees-left` trigger. In the second, the test ends it with
`endVirtualParticipant` and the row must carry the test's end reason. Under
`VPLaunchType` `FARGATE` or `EC2` it must be `COMPLETED`; under `MICROVM` it may be
`ENDED`. Both tests then require the meeting record to reach `ENDED`. It takes
about five minutes.

It cannot see what the VP posts in the chat, because the VP keeps its own messages
out of its transcript, and there is no speech to transcribe. The guest test above
covers both. Every VP it starts is ended in a `finally`.

It is opt-in with `LMA_TEST_VP_LIVE_MEETING=1`, and reads the meeting from the
Secrets Manager secret `lma/integ-tests/live-meeting`, or the one
`LMA_TEST_LIVE_MEETING_SECRET_ID` names. The secret holds a bare Google Meet link,
or JSON such as `{"url": "https://meet.google.com/abc-defg-hij"}` or
`{"platform": "ZOOM", "meeting_id": "...", "password": "..."}`. Without the flag
it skips, including under `make integ-tests-nightly`, because it carries the
`external_meeting` marker. With the flag set, a missing or malformed secret, a VP
that does not join, and a VP that does not leave are failures. The meeting's
settings, for Google Meet and for the Zoom and Teams alternatives, are in
[Scheduled Integration Tests](../docs/scheduled-integration-tests.md#optional-a-virtual-participant-in-a-live-meeting).

```bash
AWS_PROFILE=default LMA_TEST_VP_LIVE_MEETING=1 \
    .venv/bin/python -m pytest integ-tests/test_vp_live_meeting.py --stack-name lma-integtest1 -s
```

Its gate, its reading of the secret and its pass/fail rules are unit-tested in
`test_ci_plumbing.py` (`make test-integ-plumbing`), with no AWS.

## Meeting consent on the Stream Audio path

`test_meeting_consent.py` checks how the WebSocket transcriber gates a streamed
meeting on its [consent](../docs/meeting-consent.md) step. It is opt-in: it
changes the deployment's Meeting Consent setting while it runs, takes about
three minutes, and calls Amazon Transcribe and Bedrock.

```bash
.venv/bin/pip install -r integ-tests/requirements.txt -r integ-tests/requirements-video.txt
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user \
    make integ-tests STACK=lma-integtest1 LMA_TEST_CONSENT=1

# Only this file:
AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user LMA_TEST_CONSENT=1 \
    .venv/bin/python -m pytest integ-tests/test_meeting_consent.py --stack-name lma-integtest1 -s
```

Only Pillow is needed from `requirements-video.txt`, to draw the evidence
image; the ffmpeg it also installs is not used.

**The setting.** The test first asks the transcriber for its consent settings
(`CONSENT_SETTINGS`) and fails, without changing anything, if it gets no
answer. It then saves the active configuration profile's `meeting_consent`
section, and writes the section through the `lma_config` configuration store
with the caller's AWS credentials. That is the store and the profile the
Configuration page saves into, so each change appears in the configuration
history, by `integ-tests/test_meeting_consent.py`. The page's own save is open
only to the Admin group, which the test user is not in. After each write the
test waits until the transcriber reports the new mode. In the module's
teardown, whether the tests passed or not, it puts the section back exactly as
saved, or removes it again if the profile had none, and keeps any other section
saved in the meantime. Only a killed process skips that teardown; then restore
the section on the Configuration page, or from its history. The test's writes
are not checked against a concurrent edit of the Meeting Consent section
itself: an Admin's change to that section during the run is overwritten, and
the teardown then puts back the value saved before the run, so the change is
lost (it remains in the configuration history). Run it against a test stack.

**The meetings.** Each is streamed as `ws_stream_probe.py` streams, from the
same WAV fixture, at real-time pace, with a `consent` step in `START`
(`docs/websocket-streaming-api.md`, section 6.1.5):

| Test | Setting | Asserts |
|------|---------|---------|
| `test_opt_in_meeting_without_confirmation_is_refused` | `opt_in`, `owner_attests` | a `START` whose step is not confirmed is answered with `CONSENT_STATE` `CONSENT_NOT_CONFIRMED`, `captureAllowed: false`, and the connection is closed with 1008 |
| `test_refused_opt_in_meeting_captures_nothing` | the same | 20 seconds later the call has no meeting row and no transcript segments |
| `test_attested_opt_in_meeting_is_transcribed` | the same | with the step confirmed (method `verbal`) the server reports `granted`, keeps the connection open, and the meeting has transcript segments |
| `test_attested_opt_in_meeting_records_the_attestation` | the same | the meeting's `Consent` record is `opt_in` / `owner_attests`, `granted`, with an attestation by the meeting's owner (the test user) by `verbal`, and the notice posted on channel `voice` with the text sent |
| `test_attested_opt_in_meeting_stores_the_evidence_image` | the same | the synthetic chat screenshot sent straight after `START` is the record's one evidence entry, and is stored as a JPEG under `lma-consent-evidence/<meeting>/` in the recordings bucket |
| `test_attested_opt_in_meeting_evidence_is_checked` | the same | within two minutes the entry has a stored vision-model check with a verdict, a model id and a time. Any verdict passes; this asserts that the check ran |
| `test_objection_closes_the_connection` | `notify` | `CONSENT_OBJECTION` 18 seconds into a 36-second stream is answered with `OBJECTION_RECEIVED`, `captureAllowed: false`, and the server closes the connection within ten seconds, before the client's `END` |
| `test_objection_stops_the_transcript` | the same | the meeting has segments from before the objection, the count is the same 20 and 40 seconds afterwards, and no segment ends more than four seconds after it |
| `test_objection_is_recorded_and_ends_the_meeting` | the same | the record is `objected`, with `CaptureAllowed` false and one objection, by the name sent; the meeting's status is `ENDED` |

With the flag set, a missing dependency, a missing test user, a stack without
the consent resolver, or a transcriber that does not answer `CONSENT_SETTINGS`
fails the run rather than skipping. Without the flag the tests are reported as
skipped, including under `make integ-tests-nightly`, because they carry the
`optional_feature` marker. A missing check in the last opt-in test usually means
the evidence check model (**Evidence check model** in the Meeting Consent
settings, Amazon Nova Lite by default) is not enabled in Bedrock; the image is
then kept unchecked, as documented.

The meetings (`consent-probe-<refused|granted|objected>-<UTC time>`) are not
deleted: they expire with the deployment's retention, and the two that were
created can be opened on the meeting page to see their Consent section. The
refused one is never created.

To see what a deployment answers without changing anything (the active
profile, the resolved `meeting_consent` section and the transcriber's
`CONSENT_SETTINGS` answer):
`AWS_PROFILE=default LMA_TEST_USER_SECRET_ID=lma/integ-tests/cognito-user python integ-tests/meeting_consent_probe.py lma-integtest1 --region us-west-2`
