---
title: "On-demand ASR & Speaker Diarization (MicroVM)"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# On-demand ASR & Speaker Diarization (MicroVM)

> **Status: EXPERIMENTAL — not production ready.** Opt-in and off by default.
> **Amazon Transcribe remains the recommended engine for production meetings**;
> transcript quality here is below it and defaults may change between releases.
> Deploying it changes nothing on its own: meetings still use Amazon Transcribe until
> an admin switches streaming meetings or Virtual Participants onto it on the ASR
> Config page. Accuracy (WER) and diarization error rate have not yet
> been benchmarked — see [What is not yet measured](#what-is-not-yet-measured).
>
> Because it is experimental, nothing about it is tunable at deploy time:
> `EnableMicrovmAsr` is the only CloudFormation question, the model bundle ships
> with its diarization operating point already measured, and the only runtime
> settings are three switches on the Transcription Engine page.

## Table of Contents

- [Why this exists](#why-this-exists)
- [What it does](#what-it-does)
- [Feature trade-offs versus Amazon Transcribe](#feature-trade-offs-versus-amazon-transcribe)
- [Deploying it](#deploying-it)
- [Choosing the engine](#choosing-the-engine)
- [Runtime switches](#runtime-switches)
- [How speaker labels are produced](#how-speaker-labels-are-produced)
- [Calibrating a new bundle (developers)](#calibrating-a-new-bundle-developers)
- [Cost and sizing](#cost-and-sizing)
- [Local development](#local-development)
- [What is not yet measured](#what-is-not-yet-measured)
- [Speaker names](#speaker-names)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Licences](#licences)
- [See Also](#see-also)


## Why this exists

By default a [Stream Audio](stream-audio.md), [Chrome extension](browser-extension.md) or
[Desktop Capture App](desktop-capture-app.md) meeting labels the transcript by **audio
channel**: everything from your microphone
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
- Speaks a WebSocket protocol: interim `partial` results, whose end is the audio
  decoded so far, and committed `final` results, each carrying a speaker label.
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

Set **`EnableMicrovmAsr` = `true`** on the main stack. This creates the
`lma-asr-microvm-stack` nested stack: a MicroVM image, a session launcher Lambda,
the runtime-switch table the Transcription Engine page edits, and the IAM roles they need.

**Region requirement.** AWS Lambda MicroVMs must be available in your region. It is
not available in GovCloud. Deploying with `MicrovmAsr` in an unsupported region
fails with `Unrecognized resource types: [AWS::Lambda::MicrovmImage]`.

**First deployment builds the image**, which downloads the models (~150 MB for the
default bundle) and warms them. Expect several extra minutes on the first create and
on any later change of bundle. Build logs land in `/aws/lambda-microvms/<stack-name>-asr`.

That is the whole deploy-time surface. Two more values are fixed in the `AsrDefaults`
mapping in `lma-main.yaml` rather than asked, because neither is a question a
deployer should have to answer:

| Mapping key | Value | Purpose |
|---|---|---|
| `ModelBundle` | `fastconformer-titanet-small` | Which models the image is built from, with their measured diarization operating point |
| `MaxMeetingSeconds` | `14400` | Hard lifetime ceiling per MicroVM, and the cost backstop |

To change either, edit the mapping and update the stack. `scripts/sync_bundles.py
--check` verifies that `ModelBundle` names a bundle the catalog ships.

### Model bundles

One selection carries the whole configuration: the ASR model, the speaker embedder,
the turn-detection model, **and the diarization operating point measured for them** —
the similarity threshold and the minimum utterance length. Those are baked into the
image (`model.env`), so nothing has to be tuned and nothing can be mis-set.

| Bundle | ASR | Embedder | Turn detection | Threshold | Licences |
|---|---|---|---|---|---|
| `fastconformer-titanet-small` (default) | FastConformer streaming EN 480 ms | TitaNet-small | pyannote segmentation 3.0 | **0.5**, min utterance 2500 ms | CC-BY-4.0 + CC-BY-4.0 + MIT |
| `fastconformer-transcription-only` | FastConformer streaming EN 480 ms | — | — | — | CC-BY-4.0 |

Both are permissively licensed and **redistributable** (`AsrRedistributable` is a stack
output, and is `true` for both). The ASR model is the same cache-aware streaming
FastConformer-RNNT architecture as NVIDIA's Nemotron speech models, but CC-BY-4.0
rather than the NVIDIA Open Model License, and trained on NeMo ASRSET — LibriSpeech,
**Fisher**, **Switchboard**, WSJ, MLS-EN and Common Voice — so it has seen thousands
of hours of spontaneous conversational speech, which is what a meeting is. English
only.

**Why the threshold travels with the embedder, not the ASR model.** A speaker
embedding is computed from raw audio samples; the ASR model contributes nothing to
those samples, and under a fixed endpointing configuration nothing to where a segment
starts and ends either. TitaNet-small's 0.5 comes from real meeting audio under
exactly the segmentation policy this bundle runs (1200 ms endpointing, 2500 ms
minimum utterance): different speakers scored at most 0.107 in one meeting and 0.307
in another, while the same speaker at utterances of 2.5 s or longer scored 0.74–0.91,
and the midpoint of that gap is 0.52. A synthetic same-gender control (two male neural
TTS voices) corroborated it — different-speaker max 0.402, same-speaker floor 0.70,
recommendation 0.539 — and showed the earlier 0.4 would have merged that pair's
closest utterances. Its `status` is `calibrated` — the
operating point is measured — and becomes `vetted` once this exact pairing has been
validated end to end on a live multi-speaker meeting. A different *embedder* always
needs its own measurement; see
[Calibrating a new bundle](#calibrating-a-new-bundle-developers).

### Adding a bundle

Edit `source/catalog.json` (pin every checksum, and record the measured operating
point on the speaker model and the bundle), then run:

```bash
python3 lma-asr-microvm-stack/scripts/sync_bundles.py
```

That regenerates the `AsrModelBundle` allowed values and the `BundleMemory` mapping in
this stack's `template.yaml`, and validates that `lma-main.yaml`'s `AsrDefaults`
mapping names a bundle the catalog ships. `--check` reports drift without writing,
and unit tests assert the same thing, so a hand-edit that updates one file and forgets
the other fails at commit time rather than 20 minutes into a deploy. The memory
duplication cannot be removed — `MinimumMemoryInMiB` needs a CloudFormation-typed
number and a Mapping cannot be keyed on a value a custom resource resolved.

The runtime also carries an offline (`accurate`) engine — VAD-segmented, one decode
per closed utterance, for transducer models that cannot stream — and the catalog
schema supports a `vadModels` section for it. No offline bundle ships: it produces no
interim text while somebody is speaking, and its real-time factor on a real meeting is
unmeasured. See *Not included: Whisper* below for why Parakeet TDT rather than
Whisper would be the offline model to try first.

## Choosing the engine

The engine is a **deployment** setting, chosen separately for the two kinds of
meeting on **Configuration ▸ Transcription Engine** (admin only) and read at the start of
each meeting, so a change needs no redeploy:

| Setting | Covers | Default |
|---|---|---|
| Stream Audio, Chrome extension and Desktop Capture | Every meeting from those three sources | Amazon Transcribe |
| Virtual Participants | Every Virtual Participant | Amazon Transcribe |

Deploying the engine changes nothing on its own: both settings start on Amazon
Transcribe. Diarization stays a per-meeting choice — the Stream Audio form still asks
which channels to identify speakers on, because only the person in the meeting knows
whether several people share their microphone. The Desktop Capture apps can force an
engine for one run with `--asr-engine`, which wins over the deployment setting.

A meeting that selects this engine and whose MicroVM cannot start falls back to Amazon
Transcribe by itself, so a failed launch costs a warning in the log rather than a
transcript.

## Runtime switches

**Configuration ▸ Transcription Engine** (admin only) has exactly three settings. All are read at
the start of each meeting, so a change needs no stack update and no image rebuild.

| Setting | Default | Effect |
|---|---|---|
| Stream Audio, Chrome extension and Desktop Capture: engine | Amazon Transcribe | Which engine those meetings use |
| Virtual Participants: engine | Amazon Transcribe | Which engine Virtual Participants use |
| Virtual Participant voice separation | off | On the on-demand engine, a VP asks for per-voice labels so several people behind one attendee tile come out as `Name (spk_0)`, `Name (spk_1)`. A VP already names speakers from the meeting roster, which is the better label for a normal attendee, hence off |

There is deliberately nothing else. The similarity threshold, minimum utterance
length, turn-cut behaviour and speaker cap that earlier versions exposed here are the
bundle's measured operating point (or the engine's built-in defaults), baked into the
image. Stream Audio still asks **Speakers per channel** when diarization is ticked:
only the person in the meeting knows how many people share their microphone, and that
per-meeting cap is the one number a user can usefully supply.

## How speaker labels are produced

The engine embeds each utterance with the bundle's speaker model and assigns it to the
closest known voice whose cosine similarity clears the bundle's threshold, minting a
new voice when none does. Utterances shorter than the bundle's `minSegmentMs` inherit
the current speaker instead of being embedded: a one- or two-word clip embeds
unreliably, and with the speaker count unbounded every unreliable embedding that
misses the threshold invents a person. Three mechanisms below then decide *where* a
row is cut.

### Splitting a segment on a speaker change

Endpointing closes an utterance on trailing silence, so when two people speak
without a gap between them they land in one segment and share one speaker label.
Comparing short embedding windows is the obvious fix and the wrong one: we measured
sub-2.5 s utterances embedding unreliably (that is why the bundle carries a `minSegmentMs`), so
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
a cut that would leave a part too short to embed is merged into its neighbour: a middle or
trailing fragment joins the part before it, an opening fragment joins the part after it, so
a fragment is never labelled by a speaker it was not embedded against.

Measured end to end on k2-fsa's `1-two-speakers-en.wav`: the real model finds the turn
at 7.89 s, the split snaps it to 7.90 s, and the two rows come back as `spk_0`
(0.00–7.90) and `spk_1` (8.00–16.00) with the words divided 16/16.

Turn splitting is off for a bundle with no segmentation model, or when no speaker model
is baked in (there would be no embedder to identify the turns it finds).

### Cutting a row before the utterance ends

The split above is retroactive: it happens when the utterance closes, so until then a
live row holds both speakers. With live turn cutting (on by default in the
engine; `ASR_LIVE_TURN_CUT=0` in the image environment turns it off) the speaker
change becomes the *primary* boundary and
endpointing silence is only a backstop — which is the right way round, because a pause
is not what separates people, taking turns is.

While a segment is open the engine re-runs the detector over the audio since the last
cut, about once per second of audio (`ASR_TURN_CUT_INTERVAL_MS`), and closes a row as
soon as a boundary is **confirmed**: at least the bundle's `minSegmentMs` of audio before it, and at
least `ASR_MIN_TURN_MS` (700 ms) after it. That second condition is what stops a
back-channel or a model flicker from producing one-word rows, and it is also why the cut
lands a fraction of a second late — a change cannot be confirmed until some audio has
followed it.

**Why this is done in the diarization layer, not by endpointing the recogniser early.**
Forcing the recogniser to finalize would reset its decoder mid-utterance and cost it the
left context it is using to decode. Text quality is already the weaker half of this
engine, so cutting here leaves the decode completely untouched: the recogniser keeps
running and never knows a row was closed underneath it.

Three pieces of bookkeeping make that work, and each has a test that fails without it:

- The recogniser keeps reporting the **whole** open segment, so the committed prefix is
  subtracted from every later partial. Without it the settled words appear twice, once
  on a finished row and once in the live one.
- A cut consumes a wire segment number, so outbound numbering shifts by one. Without it
  two rows collide on a number and the later silently overwrites the earlier in
  DynamoDB, losing a labelled turn.
- The eventual real `final` emits only the words after the last cut.

**Bounded rows.** With no speaker change at all, a row still closes after
20 s (the engine's `max_open_segment_ms`; 0 disables) so a monologue does not sit in the live
transcript as one unlabelled block. This mirrors the equivalent bound on the Amazon
Transcribe path, which exists there because Transcribe caps a result near 30 s and never
labels a partial; this engine has no such cap of its own, so without the bound a row
could stay open for as long as somebody kept talking.

**Revisions are corrected, not lost.** A streaming decoder can revise earlier text as
more audio arrives, so a row committed early was written from a hypothesis the decoder
was still free to change. Waiting long enough to be certain would hand back the latency
the cut exists to save, so instead each committed row is **re-emitted when the utterance
closes**, once the word list is authoritative — and only if its text actually changed, so
a meeting with no revisions costs no extra writes at all.

This is safe because of how the write already works: `addTranscriptSegment` is an
unconditional `PutItem` keyed on `PK=trs#<callId>` / `SK=s#<segmentId>`, with a condition
that only stops a *partial* overwriting a final. Re-emitting a final for the same segment
number therefore updates that row in place rather than adding one. The corrected row keeps
its span, so nothing reorders (the UI sorts transcript rows by end time), and it keeps its
**original speaker** — the audio behind it did not change, and re-assigning would fold the
same embedding into a centroid twice.

Two edges are handled deliberately: a revision that would leave a row *empty* is ignored,
because blanking a transcript row loses text rather than correcting it; and the remainder
after a cut is partitioned by **time** on the word list, not by stripping a text prefix,
because a real recogniser's `text` is punctuated and capitalised while its word timings are
bare tokens — so the committed text is never a literal prefix of the closing text, and
prefix-matching duplicated every settled word onto the remainder's row.

### Partials name nobody

A partial used to carry the last identified speaker as a provisional label. On a real
call that meant the person who had just *stopped* talking was named against the words of
whoever started, until the final corrected it a second or two later — reviewers watching
the live transcript reported that the label "transforms" mid-sentence, and read it as
the diarization being unstable when the final labels were in fact correct.

Partials now carry no speaker at all, so the row shows the plain channel name until the
label is actually known, and the `(spk_N)` suffix appears when there is something true
to put in it. This matches the Amazon Transcribe path, which never had speaker labels on
partials to begin with (they only arrive on final results).

### Not included: Whisper, Distil-Whisper, and the WhisperX hybrid

Whisper-family models (Whisper large-v3-turbo, Distil-Whisper — both MIT) are not
offered, and the reason is architectural rather than licensing. The live path is a
`sherpa-onnx` `OnlineRecognizer` over an encoder/decoder/joiner transducer, decoding
frame by frame. Whisper is not a frame-synchronous transducer; `sherpa-onnx` loads it
only through `OfflineRecognizer.from_whisper(encoder, decoder, tokens)` — no joiner —
so it can only be "streamed" by segmenting on VAD and decoding each closed utterance.

The runtime does carry that shape: `asr_server/offline_recognizer.py` is a
VAD-segmented offline engine (Silero VAD, MIT) that emits one `final` per utterance,
the image resolver accepts a catalog entry with `"engine": "accurate"`, and
`scripts/fetch_model.py` fetches a bundle's VAD weights. No offline bundle ships, for
two reasons that apply to Whisper doubly: an offline engine produces **no interim text
while somebody is speaking**, and sherpa's Whisper export provides **no token
timestamps**, so turn splitting and live cutting — both of which need word timings —
would silently degrade to one speaker per VAD segment. If an offline model is ever
wanted, Parakeet TDT (CC-BY-4.0, a transducer, reports timestamps) is the one to try;
adding Whisper would additionally need a `from_whisper` backend branch and a
joiner-less file set in the catalog.

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
remains future work, and it is the natural home for
Whisper-quality transcription and elite DER — a deliberate split, with one engine and
one timeline live, two passes offline.

## Calibrating a new bundle (developers)

Every `speakerThreshold` in `catalog.json` was measured, not chosen, and the tool that
measures it ships with the runtime:

```bash
cd lma-asr-microvm-stack/source
.venv/bin/python -m scripts.calibrate --wav two-speakers.wav \
    --speaker-model /path/to/speaker-model.onnx
```

Input is a **two-channel 16-bit WAV with one speaker per channel** (any sample rate;
resampled to 16 kHz). That channel separation is the ground truth: pairs within a
channel are the same person, pairs across channels are different people, and a
threshold has to sit between those two distributions. The tool finds stretches where
one channel clearly dominates the other (dominance, not silence, so cross-talk is
excluded), embeds up to 12 per channel spread across the recording, compares every
pair, and places the threshold at the midpoint of the gap between the same-speaker
5th percentile and the different-speaker 95th percentile — then raises it above the
highest different-speaker score actually observed, so no measured pair would have
merged. Overlapping distributions produce `"confidence": "unusable"` and no number,
which is more useful than a wrong one. It also reports whether utterances under
2.5 s embed materially worse than longer ones, which is where the 2500 ms minimum
utterance floor came from.

**Choose the two voices deliberately: the threshold can only be as demanding as the
hardest pair in the sample.** On one real meeting a man and a woman scored −0.07 to
0.10 against each other while two similar-sounding women scored 0.246–0.307, and the
same-speaker floor was 0.740. Calibrating on the easy pair returns about 0.36 — too
low to keep the two women apart. Use two people of the same gender and accent, and
real meeting audio rather than synthetic speech where you can; a synthetic pair is a
useful smoke test of the tool, not a measurement to ship.

**Making a sample.** Play a recording of someone else through your laptop speakers
while you talk into the microphone and capture it with **Stream Audio** — its two
channels are exactly system audio and microphone, so the two voices land on separate
channels. Alternate for ~20 s each, four or five times. Or merge two single-speaker
files from a public corpus:

```bash
ffmpeg -i speakerA.wav -i speakerB.wav \
  -filter_complex "[0:a][1:a]amerge=inputs=2" \
  -ac 2 -ar 16000 -sample_fmt s16 calib.wav
```

Usable corpora, with one caution: **VoxCeleb is not a valid check** for TitaNet or
WeSpeaker models, which are trained on it, so a measurement there is circular.

| Corpus | Why it fits | Licence |
|---|---|---|
| [AMI Meeting Corpus](https://groups.inf.ed.ac.uk/ami/corpus/) | Real 3–5 person meetings with a **per-speaker headset mic**, the same speaker-per-channel ground truth this tool relies on | CC BY 4.0 |
| [VoxConverse](https://www.robots.ox.ac.uk/~vgg/data/voxconverse/) | In-the-wild multi-speaker audio with diarization labels; good for overlap and noise | CC BY 4.0 |
| LibriSpeech-derived mixtures (LibriMix, Libri-CSS) | Synthetic, deterministic — a regression test rather than a realism check | CC BY 4.0 |

Record the result on the speaker model (`recommendedThreshold`, `measured`) and on
its bundle (`speakerThreshold`, `minSegmentMs`), run `scripts/sync_bundles.py`, and
validate the pairing on a live multi-speaker meeting before marking it `vetted`.

## Cost and sizing

- **No idle cost.** MicroVMs run per meeting. There is no warm pool and no
  always-on ASR capacity.
- **The transcriber task is unchanged** (`256` CPU / `1024` MB, or `1024`/`2048`
  with video recording): inference happens in the MicroVM, not in the task.
- One MicroVM serves both audio channels of a meeting. The default 8 GiB baseline
  gives 4 vCPU, which both channels share.
- `MaxMeetingSeconds` in the `AsrDefaults` mapping (default 4 h, service maximum 8 h)
  bounds what a single
  MicroVM can cost. The transcriber terminates the MicroVM on meeting end and on
  SIGTERM (deploys, scale-in); the ceiling is the backstop if the task dies
  without doing either.

## Local development

The transcriber can talk to an ASR server running on your machine, with no AWS
involved:

```bash
# 1. Run the ASR server on the bundle's weights. The catalog entry names the archive;
#    the k2-fsa export ships int8 files, so point the server at them explicitly.
cd lma-asr-microvm-stack/source
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
mkdir -p models && curl -sSL <url from catalog.json> | tar xj -C models --strip-components=1
ASR_MODEL_DIR=$PWD/models ASR_MODEL_ENCODER=$PWD/models/encoder.int8.onnx \
  ASR_MODEL_DECODER=$PWD/models/decoder.int8.onnx ASR_MODEL_JOINER=$PWD/models/joiner.int8.onnx \
  .venv/bin/python -m asr_server.ws_server   # serves ws://localhost:8080; no speaker model = no labels

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
  for the shipped speaker model has been measured on real meeting audio (see
  `measured` in `catalog.json`, and
  [Calibrating a new bundle](#calibrating-a-new-bundle-developers) for the tool), but
  that is the threshold, not a DER figure.
- **MicroVM launch time and the real-time factor** of two concurrent channel
  sessions on one MicroVM have not been measured on Graviton. If a meeting's first
  transcript is slow to appear, or transcripts lag live audio, raise
  the bundle's `baselineMemoryMiB` in `catalog.json` and re-run
  `scripts/sync_bundles.py`. Note the ceiling: the `al2023-1` base MicroVM image
  accepts only **512, 1024, 2048, 4096 or 8192 MiB**, so 8192 (4 vCPU) is as large as
  a MicroVM gets and there is no headroom above the default. The image resolver
  refuses anything else up front rather than letting the stack fail minutes later at
  `AWS::Lambda::MicrovmImage`.

Measure these in a dev stack before offering the engine to users. Every number
needed is in the MicroVM log group: per-session summaries carry audio seconds,
segment counts and resident memory.

## Speaker names

A row's speaker is the channel's own name plus the engine's voice id:
`Other Participant (spk_0)`, `Other Participant (spk_1)`, `alex@example.com (spk_0)`.
Both engines format it the same way (`appendSpeakerLabel` in
`calleventdata/diarization.ts`), so a transcript reads identically whichever one
produced it.

**The suffix is not cosmetic.** Each channel runs its own session and both restart
numbering at `spk_0` for different people, so the base name is what keeps them apart —
and the suffix is what keeps two people on the *same* channel apart. Without it the
first voice heard on the tab took that channel's placeholder name ("Other Participant")
on its own, and a reviewer given such a transcript concluded the two tab speakers had
been merged into a leftover bucket. They had in fact been separated correctly; one had
simply spoken seven times as much. Clustering that meeting's embeddings independently of
the labels found two voices 0.34 apart, matching the labels one-for-one. A placeholder
that looks like a bucket gets read as one, by people and by any model summarising the
transcript.

It also degrades well: a partial has no known speaker yet, so it shows the bare channel
name and gains the suffix when the label arrives, rather than displaying somebody else's
name in the meantime.

Labels are per meeting and per channel, and they are not identities. Mapping them onto
real names — from the participant list, or by asking a model — is a separate step, and
it works far better when every label is distinct.

## Known limitations

- **English only** with the default model; `language`, `punctuate` and
  `latency_mode` are accepted by the protocol but do not change behaviour.
- **Overlapping speech resolves to one speaker.** The engine detects overlap but
  attributes the span to a single voice rather than emitting both.
- **A live cut lands slightly late**, because a speaker change is only acted on once
  `ASR_MIN_TURN_MS` of audio has confirmed it. Its text is corrected when the utterance
  closes, but the boundary itself is never moved.
- **The first minute is the least accurate**, while the model is still learning
  each voice. There is no end-of-meeting correction pass.
- **Speaker identities are per session and per channel.** A reconnect mid-meeting
  starts new identities.
- **8-hour hard ceiling** per MicroVM (service limit).
- Applies to Stream Audio, the Chrome extension, the Desktop Capture Apps and the
  Virtual Participant. The
  Upload Audio path still uses Amazon Transcribe batch.

## Troubleshooting

**The "Speakers per channel" field is missing from Stream Audio.** It is shown only
when speaker identification is ticked for at least one channel and the
streaming-meetings engine on **Configuration ▸ Transcription Engine** is the on-demand engine. A
deployment without `EnableMicrovmAsr=true` never shows it.

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

**One person appears as several speakers.** This was the engine's first real
failure and it is now measured rather than guessed. A live single-speaker meeting
produced **eight** identities for one person; every hallucinated label was a
1.2–2.4 s utterance, while long speech clustered correctly. Two causes, both now
handled by the bundle: short utterances embed unreliably (hence the 2500 ms minimum
utterance floor — shorter ones inherit the current speaker), and the inherited sherpa
default threshold of 0.5 was being applied to those short, noisy embeddings. With the
floor in place the same speaker scores 0.70–0.91 and different speakers at most 0.40 on
every recording measured, which is where the bundle's 0.5 sits. If a single person
still fragments on real meetings, that is a finding worth reporting with the MicroVM
log group's per-session summary; do not expect to tune it away, because there is no
knob — the fix is a re-measurement with `scripts/calibrate.py`.

**Two people share one label.** The reverse failure: their voices score above the
threshold against each other. On the measured data the hardest real pair (two
similar-sounding women) scored 0.307 and a synthetic same-gender pair 0.402, both under
0.5 — but a closer pair is possible. Stream Audio's **Speakers per channel** cannot
separate them (a cap only bounds splitting); the answer is again a measurement on a
recording of those voices.

**Too many speakers on genuinely multi-speaker audio.** Set **Speakers per channel** in
Stream Audio to the number of people actually sharing that microphone or tab. It is the
one per-meeting number a user can supply that the engine cannot infer.

**`scripts/calibrate.py` reports "unusable".** The same-speaker and different-speaker
scores overlap on that recording, so no threshold separates them. In order of
likelihood: heavy cross-talk (both sides talking at once, so segments contain both
voices), narrowband or heavily processed audio, a channel that is a conference bridge
carrying several people, or a speaker model that does not suit this audio. Try another
recording first; if two clean recordings both overlap, the model is the problem.

**`scripts/calibrate.py` refuses the file.** "needs a two-channel recording" means the
WAV is mono — the requirement is one speaker per channel, not just a stereo file. "is
not 16-bit PCM" means it is an MP3, M4A or float WAV: convert it with
`ffmpeg -i in.m4a -ac 2 -ar 16000 -sample_fmt s16 out.wav`, which will *not* create
channel separation on its own — the two channels have to have come from two sources.

## Licences

The image downloads model weights at build time. **You are responsible for
complying with their licences.**

| Component | Licence |
|---|---|
| `sherpa-onnx` runtime | Apache-2.0 |
| NVIDIA FastConformer streaming EN 480 ms (ASR model, both bundles) | CC-BY-4.0 |
| NVIDIA TitaNet-small (speaker embedding) | CC-BY-4.0 |
| pyannote segmentation 3.0 (turn detection) | MIT |

Each model's licence file is copied into the image alongside its weights, and the
resolved licences are reported in the ASR stack's `AsrModelLicense`, `AsrLicenceSummary`
and `AsrRedistributable` outputs. `AsrRedistributable` is `true` for both bundles: every
weight is permissively licensed. CC-BY-4.0 requires attribution, which
`THIRD-PARTY-LICENSES.txt` carries.

Every checksum in `catalog.json` was verified by downloading the artifact and hashing it.

## See Also

- [Stream Audio](stream-audio.md)
- [Desktop Capture App](desktop-capture-app.md)
- [Transcription & Translation](transcription-and-translation.md)
- [WebSocket Streaming API](websocket-streaming-api.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
