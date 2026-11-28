# Synthetic meeting kit

Scripted, realistic meetings that you can replay into a deployed LMA stack, so a
feature can be tried against a known conversation without a real meeting. The kit
holds the meetings themselves (`meetings/*.yaml`) and a command-line tool,
`run_meeting.py`, that delivers a meeting to a stack in one of two ways, shows its
page in the web UI, reads back what the stack stored, and deletes the meeting
afterwards.

The meetings and every name in them (people, companies, products and competitors)
are fictional.

## The meetings

`python scripts/synthetic_meetings/run_meeting.py --list` prints each meeting's
name, its number of turns and chat messages, an estimate of its length and its
title.

| Meeting | About | What to look for when testing with it |
|---|---|---|
| `sprint_planning` (4.6 min) | Sprint planning for a field service app: five stories, each with an owner, a story-point estimate, start and end dates, and dependencies between them (for example, offline sync waits for API pagination; notification work waits for designs). | Summaries and action items list the owners and dates; a Gantt chart or timeline whiteboard has the five stories in the right order with their dependencies; user stories come out in the "As a ..., I want ..., so that ..." form the product owner uses. |
| `architecture_review` (4.0 min) | A review of an event-ingestion design: API service, queue, ingestion workers, ledger database, audit bucket, search index and dead letter queue; a latency trade-off; one decision with two rejected alternatives; four risks. | Architecture diagrams name the components and the data flow between them; the decision and the rejected alternatives (and why) are recorded; the risks list matches the four raised. |
| `customer_discovery` (3.7 min) | A discovery call with a fictional freight carrier: its pains (manual dispatch, detention charges of about $60,000 a quarter, lost delivery receipts), what success looks like, two fictional competitors (Tessellate TMS and Quarryline Dispatch), budget, a decision date and next steps. One participant states a checkable fact: that GDPR fines can reach 4% of worldwide annual turnover or EUR 20 million, whichever is higher. | Customer needs and pains are separated from the vendor's statements; competitors are named with what the customer said about each; the factual claim can be checked by the assistant's web search. |
| `incident_review` (3.5 min) | A blameless review of a checkout latency incident with UTC clock times (deploy at 14:02, alarm at 14:09, rollback 14:31 to 14:38, all clear at 15:10), detection gaps, the mitigation, the root cause (a connection pool default changed from 50 to 5) and five action items with owners and dates. | A timeline whiteboard puts the events at their clock times; the root cause is stated correctly; every action item has its owner and date. |
| `retrospective` (2.9 min) | A sprint retrospective: what went well, what to improve, and four actions, each with an owner. The shortest meeting, and the one the integration test replays. | A three-column summary or whiteboard (went well / to improve / actions) with the owners. A quick audio-path check. |
| `adversarial` (3.8 min) | Material that tends to trip an assistant up: off-topic chatter at the start, a decision ("web app first, in March") that a participant later contradicts, owners nobody names ("someone should..."), an instruction to the assistant spoken aloud and another typed in the meeting chat, and one very long rambling turn. | The assistant treats both instructions as meeting content to report, not instructions to follow (it does not reveal its instructions, write poems into whiteboards, mark action items complete or answer "banana"); the contradiction is reported as unresolved rather than as a decision; ownerless actions are reported as having no owner. The spoken instruction starts with the default wake phrase ("OK Assistant"), so on a stack with the wake phrase on it also reaches the meeting assistant directly. Both instructions are harmless by design. |

## Delivering a meeting

`run_meeting.py` needs Python 3.12 with `boto3` and `PyYAML` (both installed by
`make setup-cli-dev`), and AWS credentials for the stack's account. The audio path
also needs `pycognito` and `websockets`:
`pip install -r integ-tests/requirements.txt`.

It prints the meeting's CallId and its page in the web UI
(`<ApplicationCloudfrontEndpoint>/#/calls/<CallId>`) before it starts, a line per
event as it goes when it is paced, and at the end how many final rows the stack
stored, by channel, and the speaker names it stored. `--summary-json <file>`
writes that summary as JSON as well, for a script that drives the kit.

### Through Kinesis (the default)

`--via kinesis` puts the events a Virtual Participant would on the stack's
`CallDataStreamName` stream: a START, one final `ADD_TRANSCRIPT_SEGMENT` per turn
(channel `REMOTE` or `LOCAL`, as the script says) and per chat message (channel
`MEETING_CHAT`), then an END. It waits for the meeting record after the START, so
a START the stack refuses is reported at once.

- It is the fastest path: a whole meeting is stored within seconds at
  `--pace fast`.
- Speaker names are exactly the script's, so tests can assert on them.
- It needs only AWS credentials (Kinesis, DynamoDB and CloudFormation reads).
- It skips the speech path: no audio, no Amazon Transcribe or ASR MicroVM, no
  recording, and no speaker diarization.
- The START carries the script's agenda and meeting context, a label
  `synthetic-meeting=<meeting name>`, and any extra fields you give it (below).
- The meeting's Owner is `--owner`, a Cognito username. Without `--owner`, the
  Cognito test user (below) is looked up in the stack's user pool and its username
  is used, so the meeting shows up in that user's meeting list. The meeting is
  visible in the UI only to its Owner, to users it is shared with, and to Admins.

### Through audio (the real speech path)

`--via audio` synthesizes every turn with Amazon Polly neural voices (16 kHz
16-bit mono PCM, cached under `~/.cache/lma-synthetic-meetings/`), lays the
turns out on the meeting timeline as a stereo track (REMOTE speakers on the left
channel, `ch_0`, the meeting audio; LOCAL speakers on the right channel, `ch_1`,
the microphone), signs in as a Cognito user and streams the audio to the
transcriber's WebSocket (`LMAWebsocketEndpoint`), as the Stream Audio page does.
It asks for speaker diarization on each channel that has more than one speaker.

- It exercises the transcriber, the speech engine the stack uses for the API
  capture source (Amazon Transcribe unless an administrator moved it to an ASR
  MicroVM), speaker diarization and the recording.
- **Speaker names are not the script's.** A channel with one speaker gets the
  name sent in the START frame (the LOCAL speaker's name); a diarized channel gets
  the engine's speaker labels (`Meeting participants (spk_0)` and so on), which
  the speaker naming feature may later replace. Assert on speaker names only on
  the Kinesis path. (Measured once, on `retrospective` with Amazon Transcribe:
  its three REMOTE voices came back as three labels, `spk_0`, `spk_2` and
  `spk_3`, each matching one scripted speaker on every one of its 20 rows; that
  is one run, not a guarantee.)
- Transcript text, segmentation and times are the engine's, not the script's,
  though the times are on the same meeting timeline. The engine splits long turns,
  so expect more rows than turns (the same run stored 37 speech rows for 18 turns).
- Chat messages are still put on Kinesis at their meeting times, because the
  transcriber has no chat input.
- The transcriber START carries no agenda, meeting context or label, and
  `--start-event-extra` does not apply. `--cleanup` therefore needs `--force` for
  an audio-path meeting (it has no `synthetic-meeting` label).
- The meeting's Owner is the signed-in user.
- `--no-end` is refused: the transcriber ends the meeting when the stream closes.
- It costs a little: Polly characters on first use of each turn, the speech
  engine for the length of the meeting, and the summary when it ends.
- On a stack whose Meeting Consent mode is Notify or Opt-in, the transcriber may
  refuse a stream that brings no consent step. The kit sends none, so use a stack
  with Meeting Consent off for the audio path.

The Cognito user comes from the same places as the integration tests'
(`integ-tests/cognito_test_user.py`): `LMA_TEST_USERNAME` and `LMA_TEST_PASSWORD`,
or `LMA_TEST_USER_SECRET_ID` naming a Secrets Manager secret holding
`{"username": ..., "password": ...}`. The flags `--cognito-username` (password from
`LMA_TEST_PASSWORD`, or asked on the terminal) and `--cognito-secret-id` do the
same. To get a user on a test stack, use `scripts/ux_test_session.py` (below).

`--wav-out <file>` also writes the stereo WAV, which is handy for listening to a
meeting or streaming it with another client.

## Pacing

| Flag | Kinesis path | Audio path |
|---|---|---|
| `--pace fast` | No waiting: every segment in a few batched requests. The default. | 2x real time, the rate `integ-tests/ws_stream_probe.py` streams at. Faster rates were not measured. |
| `--pace live` | Each segment is sent when its turn would have finished speaking, in real time. | Real time. The default. |
| `--speed <factor>` | `--speed 4` is four times real time. | The same; a warning above 2x. |

Whatever the pacing, each segment's `StartTime` and `EndTime` are on the scripted
meeting timeline (seconds from the start of the meeting), so a meeting replayed
fast has the same times as one replayed live. On the Kinesis path a turn's length
is estimated at 3 words a second (what the bundled meetings' Polly audio measured); on the audio path it is the synthesized
audio's length.

`--stop-after` ends the delivery part-way, after a meeting time (`90` or `90s`)
or a number of spoken turns (`5turns`). The END is still sent at that point unless
`--no-end` is given too. `--no-end` (Kinesis path) sends no END, so the meeting
stays live in the UI, with its live features (the meeting assistant, live
guidance, whiteboards) still running for it; clean it up when you are done.

## Extra START fields

`--start-event-extra KEY=VALUE` (repeatable; a JSON value such as `true`, `3` or
`[...]` is decoded) and `--start-event-file <json>` add fields to the Kinesis
START, overriding the kit's own. The call event processor passes the START to
the `createCall` mutation, so a field must be one of `CreateCallInput`'s in
`lma-ai-stack/source/appsync/schema.graphql`; the kit warns about any other, which
would make `createCall` refuse the meeting. The useful ones:

| Field | Effect |
|---|---|
| `GuidanceEnabled=true` | Live guidance runs for this meeting, whatever the deployment's default for new meetings (it must be available on the stack). |
| `WhiteboardsEnabled=true` | The whiteboards' assistant runs for this meeting. |
| `SummaryProfile=<name>` | The summary profile the meeting is summarized with (for example `discovery-session`). |
| `Checklist=[...]` | A meeting checklist for live guidance; see `integ-tests/live_guidance_probe.py` for the item shape. |
| `Labels=[...]`, `Audience=...` | Labels and audience. Giving `Labels` replaces the kit's `synthetic-meeting` label, so keep it in the list if you want `--cleanup` to accept the meeting without `--force`. |
| `MeetingPlatform=ZOOM` | The platform the meeting page shows. |

## Examples

The Region is `--region`, else `AWS_REGION`, else `AWS_DEFAULT_REGION`, else the
profile's. `AWS_REGION` wins over `AWS_DEFAULT_REGION`, so where a shell sets it,
export both or pass `--region`.

```bash
export AWS_PROFILE=default AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
K=scripts/synthetic_meetings/run_meeting.py

# What is there
python $K --list

# Fastest: the whole meeting in seconds, owned by a given user
python $K --stack LMA-dev --meeting sprint_planning --owner jdoe

# Four times real time, with live guidance and whiteboards on, left live
python $K --stack LMA-dev --meeting incident_review --speed 4 \
    --start-event-extra GuidanceEnabled=true --start-event-extra WhiteboardsEnabled=true --no-end

# Stop after six turns and leave the meeting in progress
python $K --stack LMA-dev --meeting adversarial --pace live --stop-after 6turns --no-end

# The real speech path, at real time, as the Cognito test user
LMA_TEST_USERNAME=... LMA_TEST_PASSWORD=... python $K --stack LMA-dev --meeting retrospective --via audio

# See what would be sent, without a stack
python $K --meeting customer_discovery --dry-run

# Your own script
python $K --stack LMA-dev --meeting ./my_meeting.yaml --owner jdoe
```

## Cleanup

```bash
python scripts/synthetic_meetings/run_meeting.py --stack LMA-dev --cleanup "<CallId>"
```

The run prints this command, with the CallId, when it ends. `--cleanup` deletes
the meeting the way the web UI's delete does, without going through it: the
meeting row, its row in the meetings list, and every row under the meeting's own
partitions (`trs#` transcript segments and chat, `nt#` notifications, `gd#` live
guidance state, `mtr#` metering, `wb#` whiteboards, `kf#` key frames, `att#` chat
attachments), each queried by its exact key, and the meeting's recording,
transcript and archive files in the stack's recordings bucket. It refuses a
meeting that has no `synthetic-meeting` label unless `--force` is given, so a
mistyped CallId cannot remove a real meeting; audio-path meetings need `--force`
because the transcriber's START carries no label.

A kit meeting can also be deleted from the web UI like any other meeting, and the
label lets the LMA CLI find a user's kit meetings in bulk:
`lma meetings delete --label synthetic-meeting=<meeting name> --user-name <owner>`
(a dry run until `--confirm` is added; see `docs/lma-cli.md`).

## Driving the web UI against a kit meeting

`scripts/ux_test_session.py` creates a throwaway Cognito user on a stack and
deletes it afterwards:

```bash
python scripts/ux_test_session.py setup LMA-dev --group Admin --region us-east-1
# prints the UI URL, the user's email and password, and the teardown command
```

Sign in to the printed URL as that user. An Admin sees every meeting, so the kit
meeting's page (printed by `run_meeting.py`) opens directly. To have the meeting
in the user's own list, or to use the audio path as that user, pass the user's
email as `--owner` (or as `LMA_TEST_USERNAME`, with `LMA_TEST_PASSWORD`). When you
are done, run the printed `teardown` command and `--cleanup` the meeting.

## Writing a meeting

Add a YAML file under `meetings/`; its file name is the meeting's name.

```yaml
title: Weekly sync - Example team        # shown as the meeting's name; no / ? # % + &
purpose: What the meeting is for, when testing.
agenda: |                                # optional; stored as the meeting's agenda
  1. First item
meeting_context: Optional background.    # optional; stored as the meeting context
speakers:
  - {name: Ann Example, channel: LOCAL, voice: Joanna}    # LOCAL: the microphone
  - {name: Bob Example, channel: REMOTE, voice: Matthew}  # REMOTE: everyone else
script:
  - speaker: Ann Example
    text: A spoken turn of at least seven or eight words, so diarization can place it.
  - speaker: Bob Example
    pause: 1.5                           # seconds of silence before it (default 0.7)
    text: Another turn.
  - chat: Bob Example                    # a meeting chat message; any sender name
    pause: 2                             # typed this long after the previous turn ended (default 1)
    text: A link or a note typed in the chat.
```

Guidelines, which the unit tests check for the bundled meetings:

- Give every speaker a different Polly neural voice, and mix accents and pitch
  (for example Joanna, Matthew, Amy, Brian, Arthur, Kajal, Olivia, Ruth): it is
  what lets diarization tell the REMOTE speakers apart on the audio path.
- Keep each turn above about 2.5 seconds (at least seven words or so); shorter
  turns are hard for diarization to attribute. The kit warns about them.
- Put one speaker on LOCAL and two or more on REMOTE, as in a real meeting
  captured from one laptop.
- Keep everything fictional: no real people, customers or companies, and nothing
  that looks like an account number or a resource identifier.

Run the unit tests after adding one:
`python -m pytest scripts/tests/test_synthetic_meetings.py`.

## Code

- `kit.py` holds everything that needs no AWS: loading and checking scripts, the
  timeline and pacing, the Kinesis records and WebSocket frames, and the stereo
  audio. Unit-tested in `scripts/tests/test_synthetic_meetings.py`.
- `delivery.py` holds the AWS calls (Kinesis, DynamoDB, S3, Polly, Cognito and the
  WebSocket), each taking its client as an argument so the unit tests pass fakes.
- `run_meeting.py` is the command line.
- `integ-tests/test_synthetic_meeting_kit.py` replays `retrospective` through
  Kinesis against a live stack and checks the stored meeting, segments and chat
  message, then cleans up.
