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
- [Speaker names](#speaker-names)
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
| `AsrModelBundle` | `nemotron-titanet-small` | A pre-vetted pairing of all three models plus its calibrated operating point |
| `AsrMaxMeetingSeconds` | `14400` | Hard lifetime ceiling per MicroVM, and the cost backstop |
| `AsrMaxSpeakers` | `0` | Optional cap per channel; 0 discovers as many as appear |

### Model bundles

One selection carries the whole configuration: the ASR model, the speaker embedder,
the turn-detection model, **and the diarization operating point measured for that
combination** — the similarity threshold and the minimum utterance length. Those two
are baked into the image, so a deployment gets a working setup without knowing any
numbers.

| Bundle | ASR | Embedder | Threshold | Redistributable |
|---|---|---|---|---|
| `nemotron-titanet-small` | Nemotron 560 ms | TitaNet-small | 0.4 | **No** (NVIDIA OML) |
| `permissive-zipformer-campplus` | Zipformer | WeSpeaker CAM++ | 0.68 | Yes (Apache-2.0 + MIT) |
| `transcription-only` | Nemotron 560 ms | — | — | No (NVIDIA OML) |

Bundles exist because a threshold is **not** a property of the embedder alone.
Utterance length moves it as much as the model does: CAM++ measured 0.30 on 1–2 s
utterances and 0.68 on 5–20 s ones, on the same voices. Choosing three models
separately therefore let a deployment assemble a pairing nobody had ever measured —
and it produced a real failure, where the threshold parameter defaulted to 0.2 while
the catalog's measured value for the default embedder was 0.4, nothing reconciled the
two, and two speakers were merged into one label on a live meeting.

A bundle with no calibrated threshold ships without one, and the engine then withholds
speaker labels until that deployment calibrates its own. That is deliberate: a guessed
threshold fragments one speaker into many or merges several into one, which is worse
than the channel labels it falls back to.

`AsrSpeakerThreshold` and `AsrMinSegmentMs` still exist on the transcriber stack as
**optional overrides**. Blank — the default — means "use the value calibrated for the
deployed bundle". Set one only to override a bundle deliberately; the ASR Config admin
page does the same thing without a 20-minute stack update.

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
| Split rows on a speaker change | Splits one utterance into a row per speaker turn when the segmentation model is baked in |

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
two-channel recording** takes a WAV you upload and derives the threshold from it.

**Why an upload rather than a meeting ID.** The ground truth is *channel
separation*: one speaker per channel, so pairs within a channel are the same person
and pairs across channels are definitely different people — exactly the comparison a
threshold has to get right. That property belongs to the *file*, not to the meeting,
so calibration takes the file directly. A sample can then come from a rehearsed
recording, a meeting you downloaded, or a public corpus, and the same page works
whether or not the deployment happens to have a suitable recorded meeting. The upload
is embedded in memory and discarded — it is never written to S3 or into a transcript.

**Requirements**

- **WAV, 16-bit PCM, two channels**, one speaker per channel. Any sample rate (it is
  resampled to 16 kHz); up to 64 MB, of which the first 20 minutes are read.
- Two to five minutes is plenty. Both speakers should talk several times and avoid
  talking over each other; cross-talk is excluded from the statistics, so heavy
  overlap just leaves less to measure.
- A mono file, or a stereo file with both voices in both channels, is **refused**
  rather than measured: it carries no ground truth, and a number derived from it
  would be confidently wrong.

**Making a sample on your own.** Play a recording of someone else through your laptop
speakers while you talk into the microphone, and capture it with **Stream Audio** —
its two channels are exactly system audio and microphone, so the two voices land on
separate channels without a second person in the room. Alternate: let the recording
talk for ~20 s, then you talk for ~20 s, four or five times each. End the meeting,
download the WAV, and upload it here.

**Making a sample from a public corpus.** Any dataset that ships **one file per
speaker** works — merge two speakers into the two channels:

```bash
ffmpeg -i speakerA.wav -i speakerB.wav \
  -filter_complex "[0:a][1:a]amerge=inputs=2" \
  -ac 2 -ar 16000 -sample_fmt s16 calib.wav
```

Note that this is the opposite of what most diarization pipelines do with a stereo
file: pyannote and friends **downmix to mono** before segmenting. This engine never
downmixes — it splits the channels and processes each independently, both for
calibration and for live meetings (one ASR session per channel) — which is what makes
channel identity usable as a label.

**How it works.** The transcriber de-interleaves the upload, resamples each channel to
16 kHz, finds stretches where one channel clearly dominates the other (dominance
rather than silence, so cross-talk is excluded), embeds up to 12 per channel on a
MicroVM in embed mode, and compares every pair.

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
| Narrow separation | A usable threshold, but sensitive to the audio it was measured on. Re-run on another sample before trusting it |
| No usable threshold | The distributions overlap: no threshold separates them on this audio. That is what a mismatched embedder looks like; it can also mean narrowband audio, heavy cross-talk, or a file whose channels are not actually speaker-separated |

Calibration also reports a **minimum utterance length** when pairs involving a short
segment score materially worse than long-only pairs — the measured cause of phantom
speakers, every one of which came from a 1.2–2.4 s utterance.

**Choose the two voices deliberately: the threshold can only be as demanding as the
hardest pair in your sample.** Measured on one real meeting, a man and a woman scored
-0.07 to 0.10 against each other while two similar-sounding women scored 0.246-0.307 -
and the same-speaker floor was 0.740. Calibrating on the easy pair returns about 0.36;
calibrating on a pair that is *further* apart than your real participants returns a
number too low to separate them. So use voices that resemble the meetings you actually
run - ideally the participants themselves, and at least two people of the same gender
and accent if that is what your meetings contain.

**Nothing is applied automatically.** A run reports; **Use these values** fills the
fields, and **Save** applies them to the next meeting that starts. A sample where only
one channel carries speech is refused before a MicroVM is even launched, since it
contains no different-speaker comparison to make.

**Who can run it.** Admin group only (it launches a MicroVM), and single-flight per
transcriber task.

**Unmeasured models are withheld, not guessed at.** Every speaker model in
`catalog.json` carries a `measured` note recording what was actually observed. When a
catalog entry carries no `measured` note — a model added but not yet
characterised — the stack reports `SpeakerModelMeasured=false` and the transcriber
transcribes with **channel labels instead of speaker labels**, logging why, until
either a calibration is applied or an admin sets a threshold. A guessed threshold
looks like working diarization while being wrong, which is worse than no diarization
at all.

### Splitting a segment on a speaker change

Endpointing closes an utterance on trailing silence, so when two people speak
without a gap between them they land in one segment and share one speaker label.
Comparing short embedding windows is the obvious fix and the wrong one: we measured
sub-2.5 s utterances embedding unreliably (that is why `AsrMinSegmentMs` exists), so
an embedding-only detector false-splits exactly where turns are shortest.

The detector is therefore **pyannote segmentation 3.0**, baked as a 6 MB ONNX model
(pyannote segmentation 3.0, MIT, redistributed by k2-fsa so no Hugging Face token is
needed). It is a specialist: 10-second windows, powerset output giving per-frame
speaker activity *and* overlap, at ~17 ms resolution — finer than the word timings a
`final` already carries, so a cut can snap to a word edge.

Three rules keep it from making transcripts worse:

- **It returns boundaries, not identities.** Its per-window speaker numbering is
  arbitrary and not comparable across windows; identity stays with the embedder and
  the per-session registry, so nothing has to stitch labels together.
- **A change must persist for `ASR_MIN_TURN_MS` (700 ms default)** to count, so a
  back-channel "mhm" does not turn one sentence into three rows.
- **Windows overlap by half and only the middle half of each is trusted** (the
  leading/trailing quarter for the first/last), so every instant is judged once, by
  the window with the most context around it, and a window seam is never mistaken
  for a turn.

Validated against the real model: k2-fsa's own `1-two-speakers-en.wav` (16 s, two
speakers) yields exactly one boundary, at 7.89 s, with a measured frame rate of
16.98 ms.

**How a split reaches the transcript.** At segment close the engine runs the detector
over the closed utterance, cuts the word list at the nearest word boundary to each
turn, embeds each part separately, and emits **one `final` per turn** with
consecutive segment numbers. Outbound numbering therefore runs ahead of the
recogniser's own by however many extra rows have been emitted, and partials are
renumbered with it, which is what keeps a partial and the first final of its
utterance on the same row. The transcriber needs no change: it already maps each
segment number to its own `SegmentId`.

Cutting the text requires word timings, so the streaming recogniser now reconstructs
them from sherpa's per-token `tokens` + `timestamps` (grouped on the SentencePiece
`▁` marker) and anchors them to the segment's own start. A segment with no word
timings is never split — guessing where the words divide would garble both rows — and
a cut that would leave a part too short to embed is merged back into its neighbour.

Measured end to end on k2-fsa's `1-two-speakers-en.wav`: the real model finds the turn
at 7.89 s, the split snaps it to 7.90 s, and the two rows come back as `spk_0`
(0.00–7.90) and `spk_1` (8.00–16.00) with the words divided 16/16.

**Still to come: the forward cut.** Today the split happens at segment close, so live
partials can briefly show the second speaker under the first speaker's label until
the utterance ends. Cutting forward the moment a change is detected is safe to add
because a correction *can* be applied afterwards — `addTranscriptSegment` is a
`PutItem` keyed on `PK=trs#<callId>` / `SK=s#<segmentId>` whose guard only stops a
*partial* from overwriting a final, so re-emitting a final for the same segment number
updates that row in place. A false forward split then degrades to two adjacent rows
carrying the *same* speaker rather than a wrong attribution, and no row ever needs
deleting.

Turn splitting is off for a bundle with no segmentation model, when no speaker model is
baked in (there would be no embedder to identify the turns it finds), or when
**Split rows on a speaker change** is unticked on the ASR Config page — that last one
takes effect on the next meeting, with no rebuild.

### Not included: Whisper, Distil-Whisper, and the WhisperX hybrid

Whisper-family models (Whisper large-v3-turbo, Distil-Whisper — both MIT) are not
selectable, and the reason is architectural rather than licensing. This stack is
**streaming-only by construction**: the image resolver refuses any catalog entry
whose `engine` is not `streaming`, and the streaming path builds a `sherpa-onnx`
`OnlineRecognizer` over an encoder/decoder/joiner transducer. Whisper is not a
frame-synchronous transducer; `sherpa-onnx` loads it only through
`OfflineRecognizer.from_whisper(encoder, decoder, tokens)` — no joiner — so it can
only be "streamed" by segmenting on VAD and decoding each closed utterance.

The runtime already contains that shape: `asr_server/vad.py` is a Silero VAD gate
(MIT) and `asr_server/offline_recognizer.py` is a VAD-segmented offline engine that
emits one `final` per utterance plus a synthetic `partial` at segment close. It is
unprovisioned — no pinned offline model, the Silero weights are not fetched by
`scripts/fetch_model.py`, and the resolver does not expose it. Adding Whisper
therefore means: a `from_whisper` backend branch, a joiner-less file set in the
catalog and in `fetch_model.py`, baking the Silero VAD weights, opening the offline
engine in the resolver, and accepting that live meetings get no true partials. The
real-time factor of large-v3-turbo for two concurrent channels on Graviton is also
unmeasured; Distil-Whisper small/medium is the more plausible candidate.

**Pyannote segmentation 3.0** is likewise absent (an earlier prototype branch used
pyannote + diart alongside Amazon Transcribe). Its code is MIT, but the published
weights are access-gated on Hugging Face, so a build-time download needs a token and
an accepted agreement — which is why the ungated, MIT Silero VAD is the segmentation
model this runtime integrates.

**The WhisperX hybrid** (Whisper for text, a separate diarizer for speakers, merged
by word-level forced alignment) is a good architecture — for **offline** audio. Its
accuracy comes from seeing the whole file: batched VAD chunks, wav2vec2 forced
alignment for word timestamps, and *global* speaker clustering, which decides how
many speakers there were only at the end. On a live meeting that means labels would
change retroactively for rows already displayed, and it reintroduces exactly the
two-timeline drift this engine exists to avoid — the streaming engine emits the
speaker label with the text it was derived from, so there is nothing to align.

Where WhisperX fits is the **Upload Media / batch** path, which today uses Amazon
Transcribe batch with `ShowSpeakerLabels`. There a second pass costs nothing, word
alignment is available, and global clustering is correct rather than premature. That
remains deferred (see the plan's *Deferred* section), and it is the natural home for
Whisper-quality transcription and elite DER — a deliberate split, with one engine and
one timeline live, two passes offline.

### Validating a model against a public corpus

Before adding a `measured` note to a catalog entry, measure the embedder on data
other than one deployment's meetings. Usable corpora:

| Corpus | Why it fits | Licence |
|---|---|---|
| [AMI Meeting Corpus](https://groups.inf.ed.ac.uk/ami/corpus/) | Real 3–5 person meetings with a **per-speaker headset mic**, which gives the same speaker-per-channel ground truth this calibration relies on, plus a far-field array for the harder case | CC BY 4.0 |
| [VoxConverse](https://www.robots.ox.ac.uk/~vgg/data/voxconverse/) | In-the-wild multi-speaker audio with diarization labels; good for overlap and noise | CC BY 4.0 |
| LibriSpeech-derived mixtures (LibriMix, Libri-CSS) | Synthetic, deterministic — useful as a regression test rather than a realism check | CC BY 4.0 |
| DIHARD, CALLHOME, NIST RT | The classic DER benchmarks | LDC licence: not redistributable, so not usable in CI |

Two cautions. **VoxCeleb is not a valid check for these models** — TitaNet and
WeSpeaker are trained on it, so measuring their threshold there is circular and
flattering. And a corpus result is a *prior*, not an operating point: microphone
gain, codec, room acoustics and language all move the distributions, which is why
per-deployment calibration stays the source of truth and why the guardrail asks for
a local measurement rather than trusting the catalog for an unknown embedder.

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
  the bundle's `baselineMemoryMiB` in `catalog.json` (and the matching `BundleMemory`
  entry in `template.yaml`) to `16384` (8 vCPU) and compare.

Measure these in a dev stack before offering the engine to users. Every number
needed is in the MicroVM log group: per-session summaries carry audio seconds,
segment counts and resident memory.

## Speaker names

The voice on the **microphone** keeps the name LMA already has for the signed-in
user. Every voice on the **tab** is numbered — `Speaker 1 (tab)`, `Speaker 2 (tab)` —
because the name LMA has for that side is a placeholder ("Other Participant"), not a
person. Numbering is shared across channels, so no two identities render the same
`Speaker N`.

This is deliberate, and was learnt the hard way. Handing the placeholder to the first
voice heard on the tab produced transcripts reading `Other Participant` beside
`Speaker 1 (tab)`, and a reviewer given such a transcript concluded the two tab
speakers had been merged into a leftover bucket — when they had in fact been separated
correctly and one had simply spoken seven times as much. Clustering that meeting's
embeddings independently of the labels found two voices 0.34 apart, with the labels
matching the clusters one-for-one. A placeholder that looks like a bucket gets read as
one, by people and by any model summarising the transcript.

Labels are per meeting and per channel, and they are not identities. Mapping them onto
real names — from the participant list, or by asking a model — is a separate step, and
it works far better when every label is distinct.

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
the `transcription-only` bundle. The UI reads `AsrDiarizationAvailable` from the LMA
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
lower the threshold on the ASR Config page, raise the minimum utterance length, and only then
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

**Calibration refuses the file.** "needs the two-channel recording" means the WAV is
mono or the header says one channel — remember the requirement is one speaker per
channel, not just a stereo file. "unsupported recording encoding" means it is not
16-bit PCM (an MP3, M4A or float WAV): convert it first with
`ffmpeg -i in.m4a -ac 2 -ar 16000 -sample_fmt s16 out.wav`, which will *not* create
channel separation on its own — the two channels have to have come from two separate
sources.

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
