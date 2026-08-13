---
title: "On-demand ASR & Speaker Diarization (MicroVM)"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# On-demand ASR & Speaker Diarization (MicroVM)

> **Status:** Experimental, opt-in, and off by default. Deploying it changes nothing
> on its own: meetings still use Amazon Transcribe unless a client asks for
> diarization. Accuracy (WER) and diarization error rate have not yet been
> benchmarked — see [What is not yet measured](#what-is-not-yet-measured).

## Table of Contents

- [Why this exists](#why-this-exists)
- [What it does](#what-it-does)
- [Feature trade-offs versus Amazon Transcribe](#feature-trade-offs-versus-amazon-transcribe)
- [Deploying it](#deploying-it)
- [Calibrating the operating point](#calibrating-the-operating-point)
- [Using it](#using-it)
- [How it works](#how-it-works)
- [Changing the model](#changing-the-model)
- [Cost and sizing](#cost-and-sizing)
- [Local development](#local-development)
- [What is not yet measured](#what-is-not-yet-measured)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Licences](#licences)

## Why this exists

By default a [Stream Audio](stream-audio.md) or [Desktop Capture App](desktop-capture-app.md)
meeting labels the transcript by **audio channel**: everything from your microphone
is one speaker, everything from the shared tab is another. That is correct when one
person sits on each side, and wrong when several people share a conference-room
microphone or several remote participants arrive through one tab.

This engine labels the transcript by **voice** instead, and does so by running
speech recognition and speaker diarization in the *same process on the same audio*.
That matters: pairing a separate diarizer with a separate ASR service gives you two
timelines, each with its own voice-activity detection and its own clock, which then
have to be aligned after the fact. Here the speaker label is derived from the exact
samples and segment boundaries the recognizer produced, so the label and the text
cannot drift apart.

It is also, simply, a transcription engine that is not Amazon Transcribe — useful
if you need a specific model for licensing reasons, or a CPU-only engine whose
weights live inside your account.

## What it does

- Runs `sherpa-onnx` streaming ASR plus online speaker diarization on an
  **AWS Lambda MicroVM**, launched per meeting and terminated when it ends.
- Speaks a WebSocket protocol: interim `partial` results and committed `final`
  results, each carrying a speaker label.
- Produces the same `ADD_TRANSCRIPT_SEGMENT` events as the Amazon Transcribe path,
  so the transcript, summaries, meeting assistant, sharing and search work
  unchanged.
- Falls back to Amazon Transcribe automatically if a MicroVM cannot be acquired.

## Feature trade-offs versus Amazon Transcribe

A meeting transcribed by this engine does **not** go through Amazon Transcribe, so
these Transcribe features do not apply to it:

| Feature | On this engine |
|---|---|
| Content redaction / PII | Not available |
| Custom vocabulary | Not available |
| Custom language model | Not available |
| Language identification / multi-language | Not available |
| Transcribe Call Analytics | Not available |
| Languages | The default model is **English only** |
| Speaker labels | Per voice, per channel (the reason to use it) |

Meetings that do not opt in are unaffected. The two engines coexist in one
deployment, chosen per meeting.

## Deploying it

Set **`TranscriptionEngine` = `MicrovmAsr`** on the main stack. This creates the
`lma-asr-microvm-stack` nested stack: a MicroVM image, a session launcher Lambda,
and the IAM roles they need.

**Region requirement.** AWS Lambda MicroVMs must be available in your region. It is
not available in GovCloud. Deploying with `MicrovmAsr` in an unsupported region
fails with `Unrecognized resource types: [AWS::Lambda::MicrovmImage]`.

**First deployment builds the image**, which downloads the model (~443 MB for the
default) and warms it. Expect several extra minutes on the first create and on any
later change to a model parameter. Build logs land in
`/aws/lambda-microvms/<stack-name>-asr`.

Relevant parameters (all under *On-demand ASR and Diarization* in the console):

| Parameter | Default | Purpose |
|---|---|---|
| `TranscriptionEngine` | `AmazonTranscribe` | `MicrovmAsr` deploys this engine |
| `AsrModelId` | `nemotron-streaming-en-0.6b-560ms-int8` | Model from the catalog, or `Custom` |
| `AsrSpeakerModelId` | `titanet-small` | Speaker-embedding model, or `none` for transcription only |
| `AsrBaselineMemoryMiB` | `8192` | Memory per MicroVM; CPU is 1 vCPU per 2 GiB, so this is 4 vCPU |
| `AsrMaxMeetingSeconds` | `14400` | Hard lifetime ceiling per MicroVM, and the cost backstop |
| `AsrMaxSpeakers` | `0` | Optional cap per channel; 0 discovers as many as appear |
| `AsrSpeakerThreshold` | `0.2` | Measured operating point for TitaNet; **specific to the speaker model** |
| `AsrMinSegmentMs` | `2500` | Shortest utterance worth embedding; shorter ones inherit the current speaker |

## Tuning it without a redeploy

The diarization operating point is empirical and specific to the speaker model, so
it lives in runtime configuration rather than only in stack parameters. **Configuration
▸ ASR Config** (admin only) edits it, and the next meeting to start picks it up — no
stack update, no image rebuild.

Every field is an override: leave it blank and the CloudFormation parameter applies.
There is no default record to keep in sync.

| Field | Effect |
|---|---|
| Speaker similarity threshold | The usual fix when one person fragments. Model-specific — [measure it](#calibrating-the-operating-point) rather than guessing |
| Minimum utterance for speaker ID | Raise it to stop short utterances minting speakers |
| Maximum speakers per channel | Hard cap; a client that knows its own meeting size overrides it |
| Endpointing silence | Trailing silence that closes an utterance |
| Diarize by default | Whether meetings request speaker labels when the client is silent on it |
| Use this engine for every meeting | Routes all streaming meetings here, with the Transcribe feature losses above |
| Require corroboration | Off by default; see the troubleshooting section |

Stream Audio also asks **Speakers on this side** when diarization is ticked. Only the
person in the meeting knows how many people share their microphone, which is why it is
asked for rather than inferred; blank means discover as many as appear, and a value
there overrides the deployment-wide cap for that meeting.

## Calibrating the operating point

The speaker threshold is a property of the speaker-embedding model, not a universal
constant, and getting it wrong is the single most visible failure this engine has.
Measured on real meeting audio with TitaNet, two *different* speakers never scored
above 0.107 while the *same* speaker scored 0.25–0.5 — so the value inherited from
the upstream prototype (0.5) split one person into eight identities, then twenty-two.
On the same audio, WeSpeaker ResNet293 scored **0.54 between two different people**,
where that same 0.5 would have merged them instead. There is no number that is right
for both.

So the deployment measures it. **Configuration ▸ ASR Config ▸ Calibrate from a
recorded meeting** takes a Meeting ID and derives the threshold from that meeting's
own audio.

**How it works, and why it needs no labelled data.** LMA already records two separate
channels: the microphone is the local participant and the tab is the remote side.
Utterances within one channel are (usually) the same person and utterances across
channels are definitely different people — which is exactly the comparison a threshold
has to get right. The transcriber streams the recording out of S3, finds stretches
where one channel clearly dominates the other (dominance rather than silence, so
cross-talk is excluded), embeds up to 12 per channel on a MicroVM in embed mode, and
compares every pair.

The threshold is then placed **inside the gap** between the two distributions, nearer
the different-speaker side, and additionally above the highest different-speaker score
actually observed — so the guarantee is concrete: no pair the calibration saw would
have been merged. Leaning towards splitting is deliberate. Both errors are real, but
fragmentation is the one that makes a transcript unreadable, and merging is partly
contained because channels are diarized separately (it can only ever merge people who
share one microphone).

**Reading the result.**

| Verdict | Meaning |
|---|---|
| Clear separation | Gap of 0.1 or more. Use the values |
| Narrow separation | A usable threshold, but sensitive to the audio it was measured on. Re-run on another meeting before trusting it |
| No usable threshold | The distributions overlap: no threshold separates them on this audio. That is what a mismatched embedder looks like; it can also mean narrowband audio or heavy cross-talk |

Calibration also reports a **minimum utterance length** when pairs involving a short
segment score materially worse than long-only pairs — the measured cause of phantom
speakers, every one of which came from a 1.2–2.4 s utterance.

**Nothing is applied automatically.** A run reports; **Use these values** fills the
fields, and **Save** applies them to the next meeting that starts. A recording where
only one side spoke is refused before a MicroVM is even launched, since it contains no
different-speaker comparison to make.

**Requirements**

- A meeting that was **recorded** (`EnableAudioRecording`), with both sides speaking
  and not talking over each other. Fifteen minutes is plenty; only the first 20 are read.
- Admin group membership. The route launches a MicroVM, so it is admin-only and
  single-flight.
- The engine deployed with a speaker model (`AsrSpeakerModelId` ≠ `none`).

**Unmeasured models are withheld, not guessed at.** Every speaker model in
`catalog.json` carries a `measured` note recording what was actually observed. When a
deployment supplies its own embedder (`AsrSpeakerModelUrl`), that note no longer
applies, so the stack reports `SpeakerModelMeasured=false` and the transcriber
transcribes with **channel labels instead of speaker labels** — logging why — until
either a calibration is applied or an admin sets a threshold. A guessed threshold
looks like working diarization while being wrong, which is worse than no diarization
at all.

## Using it

**Web UI.** On the **Stream Audio** page, tick **Enable speaker diarization**
before starting the meeting. The checkbox only appears when an ASR image with a
speaker model is deployed. Enabling it also makes the browser capture at 16 kHz —
the models' native rate — so no resampling happens anywhere in the chain.

**Desktop Capture App.** Pass `--diarization 1`, set
`LMA_ENABLE_DIARIZATION=1`, or set `"enableDiarization": "true"` in
`lma-config.json`. Add `--sample-rate 16000` to avoid a resample; other rates work
unchanged.

**Any custom client** of the [WebSocket Streaming API](websocket-streaming-api.md)
can send `"enableDiarization": true` (or `"asrEngine": "microvm"`) in its `START`
frame.

To route *every* streaming meeting to this engine regardless of what clients ask
for, set the transcriber stack's `AsrEngineDefault` to `microvm`.

### Reading the transcript

Speaker labels follow the names LMA already has. The first voice heard on a channel
keeps that channel's name — the meeting owner for the microphone, the announced
active speaker for the tab. Additional voices on the same channel become
`Speaker 1 (mic)`, `Speaker 2 (tab)`, and so on, numbered from a counter shared by
both channels so the two can never render the same number.

Labels are per meeting and are not identities: mapping them to participant names is
not implemented.

## How it works

```
Browser / Desktop app (mic + tab, 2-channel PCM)
      │
      ▼
WebSocket transcriber (Fargate)
      │  de-interleave + resample to 16 kHz mono
      ├──── ch_0 (tab) ──► ASR session A ─┐
      └──── ch_1 (mic) ──► ASR session B ─┤   one MicroVM per meeting
      │                                   │   (ASR + diarization in one process)
      │◄──── partial / final + speaker ───┘
      ▼
ADD_TRANSCRIPT_SEGMENT ──► Kinesis ──► DynamoDB ──► AppSync ──► UI
```

1. On `START`, the transcriber asks the launcher Lambda for a MicroVM, which runs
   one, waits for `RUNNING`, and mints an auth token scoped to port 8080.
2. The transcriber de-interleaves the stereo stream and opens **one WebSocket
   session per channel**. Channel identity stays authoritative: a voice on the
   microphone can never be labelled as a tab speaker, so each session only has to
   separate the voices *within* its channel.
3. Each session negotiates its config (`sample_rate`, `diarize`, `max_speakers`,
   `speaker_threshold`, `endpointing_ms`) and the server echoes what it will
   actually do. If the image has no speaker model, it reports `diarize: false`
   rather than silently returning empty labels, and the transcriber logs that.
4. On each endpointed utterance the engine embeds that segment's audio, assigns it
   to the closest speaker centroid in a per-session registry (or mints a new
   speaker), and emits the transcript with the label attached.
5. On `END` the transcriber flushes each channel (`eos`), then terminates the
   MicroVM.

Launch performance comes from the image's lifecycle hooks. `/ready` only returns
200 once the model is loaded **and** a decode has run through the real WebSocket
path — that is the instant Lambda takes the Firecracker snapshot, so the snapshot
contains a warm model. `/validate` replays the same decode from the snapshot,
which is how Lambda learns which pages to prefetch on later launches.

Reconnects are handled the way the Amazon Transcribe path handles them: the engine
restarts its segment numbering and its clock on a new connection, so the
transcriber carries a cumulative time offset and a generation counter in the
segment id, keeping the meeting timeline monotonic and segment ids unique.

## Changing the model

Models live in a catalog at
[`lma-asr-microvm-stack/source/catalog.json`](../lma-asr-microvm-stack/source/catalog.json).
Each entry pins a download URL, a SHA256, the filenames inside the archive, and the
`sherpa-onnx` / `onnxruntime` versions that can load it — model exports and runtime
versions are coupled, so they travel together.

`CreateMicrovmImage` accepts only a code artifact and has no build-argument
passthrough, so the selection has to live *inside* the artifact. At deploy time the
`AsrImageSource` custom resource resolves your parameters against the catalog,
rewrites `model.env` in the published source zip, and republishes it under a key
derived from the selection. Because the key changes, CloudFormation rebuilds the
image whenever you change a model parameter.

**To use a model that is not in the catalog** set `AsrModelId` to `Custom` and
supply:

- `AsrModelUrl` and `AsrModelSha256` — the archive and its checksum. The checksum
  is mandatory: the build fails rather than baking unverified weights.
- `AsrModelEncoderFile`, `AsrModelDecoderFile`, `AsrModelJoinerFile`,
  `AsrModelTokensFile` — the filenames inside the archive.
- `AsrSherpaOnnxVersion` and `AsrOnnxruntimeVersion` — the runtime versions that
  model needs.

The model must be a **streaming transducer** that `sherpa-onnx`'s
`OnlineRecognizer` can load, exported at 16 kHz. To add an entry to the catalog
instead, follow the same shape and verify the checksum against the download before
committing it.

A custom speaker-embedding model works the same way via `AsrSpeakerModelUrl` and
`AsrSpeakerModelSha256` on the ASR stack.

## Cost and sizing

- **No idle cost.** MicroVMs run per meeting. There is no warm pool and no
  always-on ASR capacity.
- **The transcriber task is unchanged** (`256` CPU / `1024` MB, or `1024`/`2048`
  with video recording): inference happens in the MicroVM, not in the task.
- One MicroVM serves both audio channels of a meeting. The default 8 GiB baseline
  gives 4 vCPU, which both channels share.
- `AsrMaxMeetingSeconds` (default 4 h, service maximum 8 h) bounds what a single
  MicroVM can cost. The transcriber terminates the MicroVM on meeting end and on
  SIGTERM (deploys, scale-in); the ceiling is the backstop if the task dies
  without doing either.

## Local development

The transcriber can talk to an ASR server running on your machine, with no AWS
involved:

```bash
# 1. Run the ASR server (from the upstream prototype, which has the local demo
#    tooling and model download script)
scripts/run_local_demo.sh          # serves ws://localhost:8080

# 2. Point the transcriber at it
cd lma-websocket-transcriber-stack/source/app
ASR_DIRECT_ENDPOINT=ws://localhost:8080 ASR_ENGINE_DEFAULT=microvm npm start
```

`ASR_DIRECT_ENDPOINT` bypasses the launcher entirely, so no MicroVM is created and
no auth token is needed.

Tests:

```bash
cd lma-websocket-transcriber-stack/source/app && npm test   # transcriber side
cd lma-asr-microvm-stack/source && .venv/bin/python -m pytest -q   # ASR runtime
```

## What is not yet measured

Honest state of validation, so nobody deploys this expecting known numbers:

- **Word error rate has not been benchmarked** against Amazon Transcribe on real
  meeting audio.
- **Diarization error rate has not been measured** end to end. The *operating point*
  for the shipped speaker models has been measured on real meeting audio (see
  `measured` in `catalog.json`, and
  [Calibrating the operating point](#calibrating-the-operating-point) to measure it on
  your own), but that is the threshold, not a DER figure.
- **MicroVM launch time and the real-time factor** of two concurrent channel
  sessions on one MicroVM have not been measured on Graviton. If a meeting's first
  transcript is slow to appear, or transcripts lag live audio, raise
  `AsrBaselineMemoryMiB` to `16384` (8 vCPU) and compare.

Measure these in a dev stack before offering the engine to users. Every number
needed is in the MicroVM log group: per-session summaries carry audio seconds,
segment counts and resident memory.

## Known limitations

- **English only** with the default model; `language`, `punctuate` and
  `latency_mode` are accepted by the protocol but do not change behaviour.
- **One speaker per utterance.** The engine labels each endpointed segment, so it
  does not split a single utterance mid-sentence, and overlapping speech resolves
  to the dominant speaker.
- **The first minute is the least accurate**, while the model is still learning
  each voice. There is no end-of-meeting correction pass.
- **Speaker identities are per session and per channel.** A reconnect mid-meeting
  starts new identities.
- **8-hour hard ceiling** per MicroVM (service limit).
- Applies to Stream Audio and the Desktop Capture Apps. The Virtual Participant
  and Upload Audio paths still use Amazon Transcribe.

## Troubleshooting

**The diarization checkbox is missing from Stream Audio.** The deployment either
does not have `TranscriptionEngine=MicrovmAsr` or built the image with
`AsrSpeakerModelId=none`. The UI reads `AsrDiarizationAvailable` from the LMA
settings parameter.

**Transcripts appear but are labelled by channel.** The image has no speaker model.
Look for `diarization was requested but this ASR image has no speaker model baked
in` in the transcriber log.

**Meetings fall back to Amazon Transcribe.** Look for `MicroVM ASR could not
start` in the transcriber log; the reason from the launcher is logged with it
(quota, region, image not ready). To make the failure loud instead, set
`ASR_FALLBACK_TO_TRANSCRIBE=false` on the transcriber task — meetings then produce
no transcript when the engine is unavailable.

**The image build fails.** Check `/aws/lambda-microvms/<stack>-asr`. A `SHA256
mismatch` means the pinned checksum does not match the download; `model file ... is
not in the archive` means the file names in the catalog entry are wrong; `HTTP 404`
means the URL is wrong.

A transient failure from the model host (`503`, `429`, a cut connection) is retried
five times with backoff before the build gives up — the log line reads
`retrying in 5s (1 of 5)`. If it still reports `download failed after 5 attempt(s)`,
the host is having a bad day: re-run the deployment. Nothing else needs undoing,
because a failed image build rolls the nested stack back to the previous image and
meetings keep using it.

**One person appears as several speakers.** This was the first real failure, and it
is now measured rather than guessed. A live single-speaker meeting produced **eight**
identities for one person; every hallucinated label was a 1.2–2.4 s utterance while
long speech clustered correctly.

The cause was the threshold, not the model. Replaying the production registry over
that meeting's real embeddings, with a second recording (two people, one per channel)
as the different-speaker control:

| Embedder | Same speaker | Different speakers | Operating point |
|---|---|---|---|
| TitaNet-small (default) | median 0.25–0.51, p5 0.099 | median −0.02, p95 0.074, **max 0.107** | **0.2** |
| WeSpeaker CAM++_LM | median 0.27 | median 0.30, p95 0.53 | distributions overlap |
| WeSpeaker ResNet293_LM | median 0.84–0.93 | median 0.54, p95 0.74 | ~0.8, tails still overlap |

TitaNet separates different people very well — they never exceeded 0.107 — but the
same speaker only scores 0.25–0.5, so the sherpa default of 0.5 split constantly.
Identities produced for that one-speaker meeting (ideal: 1):

| Threshold | Min segment | Identities |
|---|---|---|
| 0.5 (old default) | 1200 ms | 8 |
| 0.25 | 2500 ms | 2 |
| **0.2 (current default)** | **2500 ms** | **2** |

At 0.2 the same audio yields 2 identities instead of 8, and a two-speaker recording
resolves to exactly 2 with 100% attribution purity.

Three things follow, and they are worth internalising before changing anything:

1. **The threshold belongs to the embedder.** Cosine scales differ per model — see
   `recommendedThreshold` in `source/catalog.json`. Swapping the speaker model
   without re-measuring will either fragment or merge speakers.
2. **A same-speaker-only test is not evidence.** ResNet293 produced a perfect single
   identity on one-speaker audio, which looked like the winner until the
   different-speaker control showed it scoring 0.54 between two different people —
   it would have merged participants.
3. **`AsrRequireCorroboration` is off by default on purpose.** Withholding the first
   unmatched embedding cut 8 identities to 2 at the *wrong* threshold, but with the
   threshold right it dropped two-speaker purity to 80%, and at 0.5 it merged two
   people into one label. Reach for it only when embeddings are known to be noisy.

If a single person still fragments: run
[Calibrate](#calibrating-the-operating-point) against one of your own recorded
meetings — it performs exactly the measurement above, automatically. Failing that,
lower `AsrSpeakerThreshold` toward 0.15, raise `AsrMinSegmentMs`, and only then
consider `AsrMaxSpeakers` — a cap bounds the symptom but cannot fix a mis-set
operating point. Sample sizes behind the numbers above are small (11 utterances, one
voice pair), which is the reason calibration exists rather than a bigger table of
defaults.

**Calibration says "No usable threshold".** The same-speaker and different-speaker
scores overlap on that recording, so no threshold separates them. In order of
likelihood: heavy cross-talk (both sides talking at once, so segments contain both
voices), narrowband or heavily processed audio, a recording where one channel is a
conference bridge carrying several people, or a speaker model that does not suit this
audio. Try another meeting first; if two clean meetings both overlap, the model is the
problem — the shipped `measured` notes in `catalog.json` show what a good and a bad
embedder look like on the same audio.

**Calibration says "give either a callId or a recordingKey" or 404s.** The Meeting ID
must be one that was *recorded*; the key is derived from it the same way the recorder
wrote it. Copy the ID from the Meetings list rather than retyping it.

**Too many speakers detected on genuinely multi-speaker audio.** Lower
`AsrSpeakerThreshold`, or set `AsrMaxSpeakers` to the room size.

## Licences

The image downloads model weights at build time. **You are responsible for
complying with their licences.**

| Component | Licence |
|---|---|
| `sherpa-onnx` runtime | Apache-2.0 |
| NVIDIA Nemotron streaming EN 0.6B (default ASR model) | NVIDIA Open Model License |
| NVIDIA TitaNet-small (speaker embedding) | CC-BY-4.0 |

Each model's licence file is copied into the image alongside its weights, and the
resolved licence is reported in the ASR stack's `AsrModelLicense` output.

## See Also

- [Stream Audio](stream-audio.md)
- [Desktop Capture App](desktop-capture-app.md)
- [Transcription & Translation](transcription-and-translation.md)
- [WebSocket Streaming API](websocket-streaming-api.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
