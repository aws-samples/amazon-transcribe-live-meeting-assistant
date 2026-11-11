---
title: "On-demand ASR & Speaker Diarization (MicroVM)"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# On-demand ASR & Speaker Diarization (MicroVM)

> **The default transcription engine.** `EnableMicrovmAsr` defaults to `true`, and
> where the engine is deployed it transcribes Stream Audio, Chrome extension, Desktop
> Capture and Virtual Participant meetings unless an admin moves them onto Amazon
> Transcribe in the Transcription Engine section of the Configuration page. Amazon
> Transcribe is the alternative, and the fallback: it is used for every meeting where
> the engine is not deployed (`EnableMicrovmAsr=false`, or a Region without AWS Lambda
> MicroVMs, GovCloud included), and for any meeting whose MicroVM cannot start. A
> meeting whose Amazon Transcribe options use content redaction, a custom vocabulary,
> a custom language model, language identification or a language the deployed model
> bundle does not support, which this engine does not offer, stays on Amazon Transcribe
> whichever engine is selected (see [Feature trade-offs](#feature-trade-offs-versus-amazon-transcribe)).
>
> **Why it is the default, and what that does not mean.** It is the default because
> two features need it: [voice enrollment](voice-enrollment.md), whose speaker
> embeddings only this engine computes, and separating the several voices on one
> microphone or one attendee tile, which it does better than Amazon Transcribe in the
> measurement in [Virtual Participant](virtual-participant.md). It is not the default
> because its transcripts are better. Transcript quality here is below Amazon
> Transcribe's, its word error rate (WER) and diarization error rate have not been
> benchmarked against Amazon Transcribe (see
> [What is not yet measured](#what-is-not-yet-measured)), and its defaults may change
> between releases. Measure it in a dev stack before relying on it for production
> meetings. **To keep Amazon Transcribe**, set `EnableMicrovmAsr` to `false`, or
> choose Amazon Transcribe for each meeting source in the Transcription Engine
> section (see [Choosing the engine](#choosing-the-engine)).
>
> Nothing about it is tunable at deploy time: `EnableMicrovmAsr` is the only
> CloudFormation question, the model bundle ships with its diarization operating
> point already measured, and the only runtime settings are three switches in the
> Transcription Engine section of the Configuration page.

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
these Transcribe features are not available on it:

| Feature | On this engine |
|---|---|
| Content redaction / PII | Not available |
| Custom vocabulary | Not available |
| Custom language model | Not available |
| Language identification / multi-language | Not available |
| Languages | Those the deployed model bundle supports: 20 European languages with the default bundle, English only with some others (see below) |
| Detected language | Per final row, identified from the row's text, with a multilingual bundle; none on live rows or rows of one or two words (see [Row languages](#row-languages)) |
| Speaker labels | Per voice, per channel (the reason to use it) |

**These options keep a meeting on Amazon Transcribe.** When the effective
**Amazon Transcribe** section of the Configuration page turns on content redaction
(the Regulated presets do), names a custom vocabulary or a custom language model,
sets the language to identify-language or identify-multiple-languages, or sets a
language the deployed model bundle does not support (see
[Languages](#languages) below; with the default bundle, for example, `ja-JP` or
`el-GR`), a meeting that would otherwise use this engine is transcribed by Amazon
Transcribe instead, so the option is applied rather than silently dropped. Redaction
counts when its switch is on, whatever the language. The rule is applied each time a meeting starts, by the
WebSocket transcriber for Stream Audio, Chrome extension and Desktop Capture meetings
and by the Virtual Participant for its own, and each logs the reason at info level
(`[ASR] ... Amazon Transcribe options use content redaction, ...`). It also wins over
an explicit request for this engine (`--asr-engine microvm` from a Desktop Capture
app, or a Virtual Participant's `ASR_ENGINE=microvm`), which is then logged as a
warning: those options are the deployment's, and a client's engine request does not
switch them off. The Transcription Engine section of the Configuration page says
when the saved Amazon Transcribe options do this. To put meetings on this engine,
clear those options. The Virtual Participant has no custom language model setting,
so for it only the other options apply.

### Languages

The engine is given no language. A multilingual model identifies the language being
spoken on its own, and an English-only model decodes English whatever it hears. So
which meeting languages a deployment can put on the engine depends on the model
bundle it was built from, and is decided as follows.

- **A bundle's languages** are the languages every model that writes its text
  supports: the main model and, in a two-pass bundle, the live preview model. A
  language only one of them supports would get a wrong live row or a wrong final row.
  Each model's list is the `languages` entry in
  `lma-asr-microvm-stack/source/catalog.json`, taken from its model card. A model the
  catalog lists only as `multilingual`, without its languages, counts as English only,
  and a model decoded in one baked language (`decodeLanguage`) counts as that language.
- **The deployed bundle's list is published at deploy time.** The `AsrImageSource`
  custom resource computes it from the catalog. The ASR stack returns it as its
  `AsrModelLanguages` output, a comma-separated list of base language codes such as
  `bg,cs,da,de,en,es,...`. The main stack passes that output to the WebSocket
  transcriber and the Virtual Participant, which read it as the
  `ASR_MODEL_LANGUAGES` environment variable, and to the web UI through the
  `AsrModelLanguages` key of the LMA settings parameter.
- **A meeting's language is compared by its base language**, the part of the code
  before the first `-` (`es` from `es-US`, `pt` from `pt-BR`). English is always
  taken, because every bundle in the catalog supports it
  (`lma-asr-microvm-stack/source/tests/test_catalog_languages.py` holds the catalog to
  that). A deployment that publishes no list, such as one deployed from an earlier
  release, takes English only.
- **Language identification stays on Amazon Transcribe.** `identify-language` and
  `identify-multiple-languages` keep a meeting on Amazon Transcribe with every bundle.
  This engine names the language of each final row (see
  [Row languages](#row-languages)), but not of live rows or of rows of one or two
  words, so it is not used in place of Amazon Transcribe's identification.

| Bundle | Languages it takes |
|---|---|
| `parakeet-tdt-v3-live-nemotron35-sortformer` (default) and `parakeet-tdt-v3-live-nemotron35-titanet-small` | 20: Bulgarian, Croatian, Czech, Danish, Dutch, English, Estonian, Finnish, French, German, Hungarian, Italian, Polish, Portuguese, Romanian, Russian, Slovak, Spanish, Swedish, Ukrainian |
| `parakeet-tdt-v3-titanet-small` and `parakeet-tdt-v3-sortformer` | The 25 of Parakeet TDT 0.6B v3's model card: the 20 above and Greek, Latvian, Lithuanian, Maltese and Slovenian |
| `nemotron35-titanet-small` | The 28 base languages of the 32 locales Nemotron 3.5's model card lists as transcription-ready or broad-coverage, including Arabic, Chinese, Hindi, Japanese, Korean, Turkish and Vietnamese |
| `parakeet-tdt-v3-live-titanet-small` and `parakeet-tdt-v3-live-sortformer` | English only, because their live preview model (FastConformer) is English only |
| Every other bundle | English only |

The Transcription Engine section of the Configuration page lists the deployed
bundle's languages next to its name.

**What has been measured.** On 2026-11-10 the default bundle's engine was run
in-process on synthetic single-speaker clips (Amazon Polly neural voices, about 50
words or 20 seconds each), with no language configured:

| Clip | Final text WER | Live preview WER |
|---|---|---|
| en-US (baseline) | 0.0% | 16.4% |
| es-ES | 0.0% | 13.5% |
| es-US | 0.0% | 23.1% |
| fr-FR | 1.8% | 12.5% |
| de-DE | 6.2% | 31.2% |
| it-IT | 6.0% | 20.0% |
| pt-BR | 3.9% | 11.8% |
| nl-NL | 0.0% | 16.7% |
| English, then Spanish | 0.0% | 29.4–35.3% |

WER is word error rate after lowercasing, stripping punctuation and writing spoken
numbers as digits. The live preview figure is the last preview text shown for each
row before its final replaced it, so it counts words the preview had not yet caught
up with at either end of the row. The final text, from Parakeet TDT 0.6B v3, was
within 0–6% in all seven non-English locales and followed a switch from English to
Spanish within one meeting. The live preview, from Nemotron 3.5, is rougher in every
language, and the final replaces it on every row, so its errors do not reach the
stored transcript. Forcing a language on the preview model did not improve it and
dropped the other language's speech in a mixed-language clip, so the engine sets
none. Neither model reports the language it identified in time to label a row, so
the server identifies it from the final text (see [Row languages](#row-languages)).

These figures are optimistic. The audio was clean synthetic speech with one speaker
per clip and one clip per language. With about 50 words per clip, one wrong word
moves the WER by about 2 points. The run was on x86_64, not on the ARM64 MicroVM
image. The other 13 languages of the default bundle were not tested, and no real
meeting audio was. Measure the languages that matter to you on real meetings before
relying on them.

### Row languages

With a bundle of two or more languages, the engine names the language of each final
row. The `final` message carries it as `language`, a base ISO 639-1 code such as
`es`, and the WebSocket transcriber and the Virtual Participant write it on the
row's `ADD_TRANSCRIPT_SEGMENT` event as `LanguageCode`, the field Amazon Transcribe's
language identification fills. What the meeting page does with it is described in
[Detected languages](transcription-and-translation.md#detected-languages).

**Where the language comes from.** None of the bundle's speech models gives the
server a language for a row. The offline models that write the final text
(Parakeet TDT, for example) output none. Nemotron 3.5, the default bundle's live
preview model, does write a locale tag such as `<es-ES>` after each sentence, and
its vocabulary has 39 of them. sherpa-onnx 1.13.4 removes tokens of that shape from
every result and has no option to keep them, but renaming them in the model's
`tokens.txt` does bring them through, and on whole recordings the tags named the
right base language every time. They are not usable for rows, though: the model
writes a sentence's tag only once it has heard the next sentence begin or a long
pause, and the engine has sent the row's final by then. On the engine path, 1 of
34 final rows had a tag by the time its final was sent. Finishing the preview
stream when the utterance closes did not produce the missing tags. The region in
the tags was also wrong on both regional clips (es-US speech tagged `es-ES`, pt-BR
speech tagged `pt-PT`).

So the server identifies the language of the final text instead, which in a
multilingual bundle is already written in the spoken language:

- **The classifier** is [py3langid](https://github.com/adbar/py3langid) 0.4.0
  (BSD-3-Clause), an n-gram model of about 4.5 MB installed into the image. It is
  loaded when the server starts, so it is in the MicroVM snapshot, and the
  build-time warmup fails the image build if a multilingual bundle cannot load it.
- **It chooses only among the bundle's languages.** `AsrImageSource` writes the
  bundle's list into the image's `model.env` as `ASR_BUNDLE_LANGUAGES`, the same
  list it reports as `AsrModelLanguages`. A bundle with one language names none.
- **It names none for a row of one or two words, or below a probability of
  0.8.** On the evaluation set below, every misidentification was on a row of one
  or two words.
- **Only finals are identified.** A live row is the preview model's rougher text
  and is replaced by its final moments later.
- **The client chooses the region.** The engine reports `es`, not `es-US`. The
  transcriber and the Virtual Participant give a row in the meeting's own language
  the meeting's code, and any other language a fixed region (`microvmRowLanguageCode`
  in both `configuration-store.ts` files): `en-US`, `es-US`, `fr-FR`, `pt-BR`,
  `de-DE`, and so on.

**What has been measured.** On 2026-11-10 the classifier was evaluated on 185 rows.
34 were the final rows the default bundle's engine wrote for the synthetic clips
described above, run in-process on x86_64 through the same protocol adapter the
server uses. The other 151 were hand-written meeting utterances: 50 in English,
many of them short or carrying foreign names and product terms (*"Let's ping Juan
about the Kubernetes rollout."*), and 101 in Spanish, French, German, Italian,
Portuguese, Dutch, Polish and Swedish. The classifier was restricted to the default
bundle's 20 languages.

| Rows | Labelled | Correct | English rows named as another language |
|---|---|---|---|
| All 34 engine-path rows, with the rules above | 28 | 28 | 0 |
| The 143 rows of three or more words, probability 0.8 | 133 | 133 | 0 of 49 |
| The 143 rows of three or more words, any probability | 143 | 143 | 0 of 49 |
| All 185 rows, any probability (no word floor) | 185 | 168 | 2 of 61 |

The 6 unlabelled engine-path rows were three of one or two words (*Okay.*,
*Por favor.*) and three below the threshold (*The vendor contract renewal*, twice,
and one Portuguese sentence). Both mixed-language clips were labelled English and
then Spanish, row by row.

Other classifiers were compared on the same rows. lingua-language-detector 2.1.1
in its low-accuracy mode named 10 of the 49 English rows of three or more words as
another language, and its high-accuracy mode used about 950 MB of memory and
labelled 72 of the 143 rows at a probability of 0.8. py3langid 0.2.2, the last
release that declares support for numpy 1.x, named 4 of those English rows as
another language. fastText's `lid.176` model was not evaluated, because its licence
is CC-BY-SA-3.0 and its Python package publishes no official wheels. A spoken
language-identification model (sherpa-onnx can run Whisper for this) was not
evaluated either. It would add a Whisper model to the image and an encoder pass per
row, where the text classifier costs well under a millisecond per row.

**Cost.** The image grows by about 4.5 MB. Loading the classifier takes 0.5–1 s at
server start and adds about 105 MB of resident memory. Identifying a row took
0.14 ms on average (0.36 ms at most) on the x86_64 run above. py3langid 0.4.0
declares numpy 2 or later, while the image pins numpy 1.26.4, so the Dockerfile
installs it without its dependencies; its output on the 185 rows was identical on
numpy 1.26.4 and 2.4.6.

**Caveats.** The 151 hand-written rows were written for this test, and the 34
engine rows come from clean synthetic speech, so real meetings, with recognition
errors, disfluencies and code-switching inside a sentence, will do worse. A row
that mixes two languages gets one of them. The region is a convention, not a
detection. On ARM64 (Python 3.11 with numpy 1.26.4, under emulation) the
classifier installed without its dependencies, loaded and named sample rows
correctly, but it has not been run inside a built MicroVM image.

The two engines coexist in one deployment, and which one a meeting uses is chosen
per meeting source (see [Choosing the engine](#choosing-the-engine)).

## Deploying it

**`EnableMicrovmAsr`** on the main stack defaults to `true`. It creates the
`lma-asr-microvm-stack` nested stack: a MicroVM image, a session launcher Lambda,
the table the runtime switches were stored in before they moved into the shared
[configuration store](configuration-store.md) (kept so a deployment can be rolled
back), and the IAM roles they need.

**Region requirement.** AWS Lambda MicroVMs must be available in your Region: today
us-east-1, us-east-2, us-west-2, ap-northeast-1 and eu-west-1, and not GovCloud (the
same list as the `VPLaunchType` parameter's). The nested stack is created only in
those Regions, through the `IsMicrovmRegion` condition in `lma-main.yaml`, because
elsewhere `AWS::Lambda::MicrovmImage` is not a recognised resource type and the
deployment would fail. In any other Region `EnableMicrovmAsr` has no effect, the
Transcription Engine section shows no engine choice, and every meeting uses Amazon
Transcribe. When Lambda MicroVMs reach another Region, add it to that condition.

**Using Amazon Transcribe for every meeting.** Set `EnableMicrovmAsr` to `false`, or
keep the engine deployed and move both meeting sources onto Amazon Transcribe in the
Transcription Engine section (see [Choosing the engine](#choosing-the-engine)).

**Upgrading.** A stack update keeps the value the stack was deployed with unless you
change it: the console and `lma deploy` both reuse a parameter's previous value, so a
deployment created with `EnableMicrovmAsr=false` stays on Amazon Transcribe until
you set it to `true`. See [Choosing the engine](#choosing-the-engine) for what
happens to the engine switches on upgrade.

**First deployment builds the image**, which downloads the models (~1.1 GB for the
default bundle) and warms them. Expect several extra minutes on the first create and
on any later change of bundle. Build logs, and the logs of every ASR session's
MicroVM, land in `/aws/lambda-microvms/<stack-name>-asr-logs`, which the stack
declares with the retention you set in `CloudWatchLogsExpirationInDays` and the
deployment's customer-managed key. The launcher names that group on each
`RunMicrovm` call, because the image's logging setting covers only the build.
Releases before v0.4.0 logged to `/aws/lambda-microvms/<stack-name>-asr`; that
group keeps the logs it already
holds and is not removed on upgrade, so delete it yourself once you no longer
need them.

That is the whole deploy-time surface. Two more values are fixed in the `AsrDefaults`
mapping in `lma-main.yaml` rather than asked, because neither is a question a
deployer should have to answer:

| Mapping key | Value | Purpose |
|---|---|---|
| `ModelBundle` | `parakeet-tdt-v3-live-nemotron35-sortformer` | Which models the image is built from, with their measured diarization operating point |
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
| `fastconformer-titanet-small` | FastConformer streaming EN 480 ms | TitaNet-small | pyannote segmentation 3.0 | **0.5**, min utterance 2500 ms | CC-BY-4.0 + CC-BY-4.0 + MIT |
| `fastconformer-transcription-only` | FastConformer streaming EN 480 ms | — | — | — | CC-BY-4.0 |
| `nemotron35-titanet-small` | Nemotron 3.5 ASR streaming 0.6B, 560 ms (punctuated, multilingual) | TitaNet-small | pyannote segmentation 3.0 | 0.5, 2500 ms | OpenMDW-1.1 + CC-BY-4.0 + MIT |
| `nemotron-titanet-small` | Nemotron speech streaming EN 0.6B, 560 ms | TitaNet-small | pyannote segmentation 3.0 | 0.5, 2500 ms | NVIDIA Open Model License (not redistributable) + CC-BY-4.0 + MIT |
| `parakeet-tdt-v3-titanet-small` | Parakeet TDT 0.6B v3 (offline engine) | TitaNet-small | pyannote segmentation 3.0 | 0.5, 2500 ms | CC-BY-4.0 + CC-BY-4.0 + MIT + MIT (VAD) |
| `parakeet-tdt-v2-titanet-small` | Parakeet TDT 0.6B v2 (offline engine, English) | TitaNet-small | pyannote segmentation 3.0 | 0.5, 2500 ms | CC-BY-4.0 + CC-BY-4.0 + MIT + MIT (VAD) |
| `qwen3-asr-titanet-small` | Qwen3-ASR 0.6B (offline engine, LLM decoder, multilingual) | TitaNet-small | pyannote segmentation 3.0 | 0.5, 2500 ms; labels per utterance | Apache-2.0 + CC-BY-4.0 + MIT + MIT (VAD) |
| `qwen3-asr-transcription-only` | Qwen3-ASR 0.6B (offline engine) | — | — | — | Apache-2.0 + MIT (VAD) |
| `cohere-transcribe-titanet-small` | Cohere Transcribe 2B (offline engine, 14 languages, one baked) | TitaNet-small | pyannote segmentation 3.0 | 0.5, 2500 ms; labels per utterance | Apache-2.0 + CC-BY-4.0 + MIT + MIT (VAD) |
| `parakeet-tdt-v3-live-titanet-small` | Parakeet TDT 0.6B v3 (offline engine) with a FastConformer live preview | TitaNet-small | pyannote segmentation 3.0 | 0.5, 2500 ms | CC-BY-4.0 + CC-BY-4.0 + CC-BY-4.0 + MIT + MIT (VAD) |
| `parakeet-tdt-v3-live-nemotron35-titanet-small` | Parakeet TDT 0.6B v3 (offline engine) with a punctuated Nemotron 3.5 live preview | TitaNet-small | pyannote segmentation 3.0 | 0.5, 2500 ms | CC-BY-4.0 + OpenMDW-1.1 + CC-BY-4.0 + MIT + MIT (VAD) |
| `parakeet-tdt-v3-sortformer` | Parakeet TDT 0.6B v3 (offline engine) | Nemotron 3 Diarization (end to end) | built in | none to tune; up to 8 speakers per channel | CC-BY-4.0 + OpenMDW-1.1 + MIT (VAD) |
| `parakeet-tdt-v3-live-sortformer` | Parakeet TDT 0.6B v3 (offline engine) with a FastConformer live preview | Nemotron 3 Diarization | built in | none to tune; up to 8 speakers | CC-BY-4.0 + CC-BY-4.0 + OpenMDW-1.1 + MIT (VAD) |
| `parakeet-tdt-v3-live-nemotron35-sortformer` (default) | Parakeet TDT 0.6B v3 (offline engine) with a Nemotron 3.5 live preview | Nemotron 3 Diarization | built in | none to tune; up to 8 speakers | CC-BY-4.0 + OpenMDW-1.1 + OpenMDW-1.1 + MIT (VAD) |

The default is `parakeet-tdt-v3-live-nemotron35-sortformer`: Parakeet TDT 0.6B v3's finals, a punctuated Nemotron 3.5 preview
of the utterance still being spoken, and Nemotron 3 Diarization labelling every word. Text
appears while people speak, and short replies get their own rows and speakers with no
threshold to tune. It is the heaviest bundle and is `experimental` until vetted; it kept
up with two sessions in its live runs. Parakeet v3 with TitaNet-small
(`parakeet-tdt-v3-titanet-small`), the previous default, is the `vetted` choice for
lighter images: punctuated, cased, 25 languages, and in the live runs of 2026-09-22 the
most accurate model with the only per-turn speaker labels that were right throughout, but
rows appear only when an utterance closes. FastConformer, the default before that, is the
lightest streaming option: the same cache-aware FastConformer-RNNT
architecture as NVIDIA's Nemotron speech models, CC-BY-4.0, English only, no punctuation,
at a real-time factor of about 0.26 on a 4-core host.

The alternatives trade weight for transcript quality. Nemotron 3.5 emits punctuation
and casing and reads far better on the same meeting audio, but ran at a real-time
factor of 0.89 for a single session on a 4-core host; on a MicroVM it kept up with two
channels in the live runs, but it drops or merges more words than any other model in the
catalog. The earlier
Nemotron speech model is kept for deployments that accept the NVIDIA Open Model
License; the catalog marks it `redistributable: false` and it is never the default.
The Parakeet and Qwen3-ASR bundles run the offline engine described below. Qwen3-ASR
gave the best transcript of everything measured (real-time factor 0.38, punctuated,
"WACC" where every other model wrote "whack") but returns no word timings, so its speaker
labels are one per utterance rather than per turn. Every alternative reuses TitaNet-small,
so the operating point carries over unchanged; each is `calibrated` until vetted live.
The `-sortformer` bundles have no threshold to measure and are `experimental` until vetted;
a bundle with no status in the catalog reports `uncalibrated`.
Parakeet v3 with TitaNet-small is `vetted` after the live runs of 2026-09-22: text on a par with
Qwen3-ASR, every speaker turn of 2.5 s or more labelled correctly, and rows within about 20 s
of speech.

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

A model entry pins its weights in one of two forms. Most name one archive (`url`,
`sha256`, `archive: "tar.bz2"`) and list the file names inside it under `files`. A model
whose files are served individually at a fixed revision can instead set
`archive: "files"` and give each `files` entry as `{"path", "url", "sha256"}`, the way the
diarizer's model and data files are given, with an optional `warmWav` (`url`, `sha256`)
for the clip the image's warm-up decodes. Use a URL that names a commit (Hugging Face's
`/resolve/<40-character commit>/<file>`), never a branch, so the bytes behind each
checksum cannot change. Both forms produce the same files under the same names in the
image, so the runtime does not know which was used. The Parakeet TDT v3 and v2 entries use
the per-file form, pinned to the sherpa-onnx maintainer's Hugging Face repositories. A
Qwen3-ASR model needs its tokenizer directory and so must come from an archive.

The runtime also carries an offline (`accurate`) engine — VAD-segmented, one decode
per closed utterance, for transducer models that cannot stream — which the
Parakeet, Qwen3-ASR and Cohere Transcribe bundles use. It produces no interim text while somebody is speaking: a row
appears when the utterance closes, which is a 1.2 s pause, the first 300 ms dip once the
utterance has run 10 s, or 20 s of speech at the latest, so a row arrives within about
20 s of speech plus the decode time. It decoded the
test meeting at a real-time factor of 0.14 on a 4-core host with punctuated, cased text.
See *Not included: Whisper* below for why Parakeet TDT rather than Whisper is the
offline model offered.

**Two-pass bundles.** `parakeet-tdt-v3-live-titanet-small` and
`parakeet-tdt-v3-live-nemotron35-titanet-small` pair Parakeet with a streaming model that
previews the open utterance: the live row shows the streaming text as it is spoken, and
Parakeet's decode replaces it under the same segment when the utterance closes. Rows,
word timings and speaker labels are Parakeet's. The preview adds the streaming model's
CPU per session, about 0.26 real time for FastConformer and 0.6 to 0.9 for Nemotron 3.5,
so the Nemotron variant is the heavier one. Both ran live on 2026-09-22 and 2026-09-23 with
two sessions and no dropped audio, including a 49-minute meeting on the Nemotron variant.

**End-to-end diarization bundles.** The three `-sortformer` bundles replace the TitaNet
embedder, the pyannote turn detector and the threshold registry with NVIDIA's Nemotron 3
Diarization, a Sortformer model that labels every 10 ms frame with the activity of up to
eight speakers per channel and keeps identities through a speaker cache carried across the
session. The engine runs one diarizer step each time the offline engine closes an
utterance, over the audio since the previous step in pieces of at most 10 s, and gives each Parakeet word the speaker
active under it, so a row is split wherever the speaker changes, however short the turn. A
run of fewer than three words shorter than 0.5 s stays with its neighbour. There is no
threshold or minimum utterance to calibrate. Labels restart for every meeting, as with the
other bundles. The model is the int8 ONNX export published by onnx-community, pinned to a
commit. On a 4-core x86 host the diarizer adds about 0.09 real time to the diarized channel,
and its streamed labels agree with the full-precision offline reference on more than 99 % of
speech frames. The two live-preview variants ran live on 2026-09-28 and 2026-09-30 with
two sessions and kept up; the plain bundle has not run live.

## Choosing the engine

The engine is a **deployment** setting, chosen separately for the two kinds of
meeting in the **Transcription Engine** section of **Configuration ▸ Configuration** (admin only) and read at the start of
each meeting, so a change needs no redeploy:

| Setting | Covers | Default |
|---|---|---|
| Stream Audio, Chrome extension and Desktop Capture | Every meeting from those three sources | ASR MicroVM engine |
| Virtual Participants | Every Virtual Participant | ASR MicroVM engine |

Both settings ship on the ASR MicroVM engine. Choosing **Amazon Transcribe** for a
setting moves every meeting of that kind onto it. Upload Audio always uses Amazon
Transcribe batch transcription and has no engine setting.

The shipped values are part of the deployment's baseline configuration, so they reach
every deployment whose admin has not chosen an engine. A choice an admin saved is
kept on upgrade and keeps winning: one saved in the Transcription Engine section is
stored in the active configuration profile, and one saved on the Transcription
Engine page of a release before v0.4.0 is carried into that profile by the upgrade.
A deployment upgraded from such a release on which nobody saved that page moves onto
the ASR MicroVM engine wherever the engine is deployed; choose Amazon Transcribe in
the section to keep it. That happens in one of two ways on an upgrade from 0.3.x. If
the deployment's `custom` profile holds no settings at all, it is removed and the
deployment runs on the default profile, so it follows the shipped switches of this
and every later release. If it holds settings, it is converted into a full-copy
Custom profile, and every switch nobody saved is filled in from this release's
baseline, which puts those sources on the ASR MicroVM engine. From then on that
profile keeps its own copy of the switches, so a later release that changes the
shipped switches does not change it.

On a deployment without the engine (`EnableMicrovmAsr=false`, or a Region without
AWS Lambda MicroVMs) the settings are not shown and have no effect: the transcriber
and the Virtual Participant find no ASR launcher configured and use Amazon
Transcribe, logging that at info level. Diarization stays a per-meeting choice — the Stream Audio form still asks
which channels to identify speakers on, because only the person in the meeting knows
whether several people share their microphone. The Desktop Capture apps can force an
engine for one run with `--asr-engine`, which wins over the deployment setting.

A meeting that selects this engine and whose MicroVM cannot start falls back to Amazon
Transcribe by itself, so a failed launch costs a warning in the log rather than a
transcript.

## Runtime switches

The **Transcription Engine** section of **Configuration ▸ Configuration** (admin only)
has exactly three settings; on a deployment without this engine it shows only the
third, Virtual Participant voice separation. All are read at the start of each meeting,
so a change needs no stack update and no image rebuild. The
WebSocket transcriber reuses what it read for up to 30 seconds, so a streaming meeting that
starts within 30 seconds of a save may still use the previous engine; a Virtual Participant
reads them fresh each time. They are stored in the shared
[configuration store](configuration-store.md) as the `asr_engine` section, so they are changed
on the page rather than in the DynamoDB console.

| Setting | Default | Effect |
|---|---|---|
| Stream Audio, Chrome extension and Desktop Capture: engine | ASR MicroVM engine | Which engine those meetings use |
| Virtual Participants: engine | ASR MicroVM engine | Which engine Virtual Participants use |
| Virtual Participant voice separation | on | A VP asks for per-voice labels so several people behind one attendee tile come out as `Name (spk_0)`, `Name (spk_1)`. Honoured on **both** engines: the on-demand engine separates voices itself, and on Amazon Transcribe the VP recovers the turns from Transcribe's own per-item speaker labels. With it on, the VP also uses each voice to correct the roster name once the voice has been heard long enough alongside one attendee (see [Naming rows from the voice as well](virtual-participant.md#naming-rows-from-the-voice-as-well)); on a measured two-person Zoom meeting that cut wrongly named rows from 10 to 6 of 415, which is one meeting on one platform. A person the engine splits into two voices is still shown as **one person**: a name is shown as several people only when it reads as a room or its voices are given different names (see [One person or several people](transcription-and-translation.md#one-person-or-several-people)). Clear it to name VP rows from the roster alone; what it costs on this engine is under [Cost and sizing](#cost-and-sizing). Measured on four voices mixed onto one channel, the on-demand engine gave 3 labels at 79% purity against Amazon Transcribe's 2 at 57%, so prefer this engine for conference rooms, and for the voice correction, which acts only on a voice that is at least 70% one attendee. The voice assistant's own speech is never diarized on either engine, because it is transcribed on its own channel |

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
The recogniser's own forced cut, which resets the decoder and drops the word in flight,
comes only once an utterance reaches 60 s; the diarizer's 20 s row bound keeps rows
readable without a reset.

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

Partials now carry no speaker by default, so the row shows the plain channel name until the
label is actually known, and the `(spk_N)` suffix appears when there is something true
to put in it. This matches the Amazon Transcribe path, which never had speaker labels on
partials to begin with (they only arrive on final results).

**Provisional labels from the partial's own audio.** A client can ask for a label on
partials with `partial_speakers: true` in its `config`, alongside `diarize: true`. The
Sortformer diarizer labels audio frame by frame as it arrives, so it can say whose voice
is under the open row without waiting for the row to close. The label is the voice active
over the most recent labelled second of **this row's own audio**. It is never the last
speaker identified before the row, which is what made the old provisional label wrong. A
partial carries no label until at least 0.3 s of its row has been labelled. The diarizer
is stepped for partials only when its labels have fallen 1 s or more behind the open row,
with its usual lookahead, so a partial's label is read from the same kind of frames as a
final's and is at most about 1.3 s behind the audio. The final still carries the
authoritative labels, and still splits the row where the voice changes.

The bundles that use the TitaNet embedder do not support this: they cannot embed a
segment until it closes, so their partials stay unlabelled whatever the client asks. An
engine image from before this field ignores it, and its partials are unlabelled too.
The Virtual Participant asks for it on its participant session whenever it asks for
voice separation (see
[Live rows](virtual-participant.md#live-rows)). The WebSocket transcriber (Stream Audio
and the browser extension) does not ask for it yet.

What it costs has **not been measured**. Without it the diarizer steps once each time an
utterance closes and every 10 s through a long one. With it, it also steps about once a
second while someone is speaking, and each step also re-reads the diarizer's cached
context (up to about 42 s of speech features), so the diarizer's share of the CPU can be
several times higher than without it. Watch the `engine load` line, and any
`engine behind, dropped` lines, on the first meetings that use it.
A channel that asks for no labels, such as Stream Audio's microphone or the Virtual
Participant's assistant, gets none: the engine skips voice embedding for it, and its rows
still close at 20 s.

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

Qwen3-ASR has Whisper's shape — an LLM decoder, a different constructor, no word
timings — and it is offered anyway, on the offline engine, as a transcription-quality
bundle whose speaker labels are one per utterance. The runtime grew a second offline
model kind for it (`ASR_MODEL_KIND=qwen3_asr`), which is the route a Whisper bundle would
take if one were ever wanted.

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
- **What a meeting is billed for depends on its engine.** A meeting on this engine
  incurs Lambda MicroVM compute for the MicroVM's size and lifetime rather than
  Amazon Transcribe streaming per-second charges; one that falls back to Amazon
  Transcribe incurs the latter. The [meeting cost estimate](meeting-cost-estimate.md)
  itemises whichever applied.
- **The transcriber task is unchanged** (`256` CPU / `1024` MB, or `1024`/`2048`
  with video recording): inference happens in the MicroVM, not in the task.
- One MicroVM serves both audio channels of a meeting. An 8 GiB MicroVM exposed 16 CPUs
  in the live runs of 2026-09-30, which both channels share; the image still sizes its
  inference threads for 4.
- The engine logs its CPU use once a minute while a meeting is open, as
  `engine load: 5.57 of 16 cores (35%) over the last 60s, 2 session(s), rss=2340MB`,
  in `/aws/lambda-microvms/<stack>-asr-logs`. The size is billed whatever the load, so
  this line is what tells you whether a bundle would fit a smaller size.
  `ASR_LOAD_LOG_INTERVAL_S` changes the interval; `0` turns it off.
- **Virtual Participant voice separation is on by default, and on this engine it
  adds work but not billed size.** With it on, a VP's participant session also runs
  the bundle's speaker diarizer (Sortformer, in the default bundle); with it off the
  diarizer is not attached to the session. The MicroVM is the same one either way,
  sized by the bundle (8192 MiB for every bundle in the catalog) and billed for its
  size and lifetime, so the switch does not change what a meeting is billed. What it
  does to load has been measured only partly. On one 58-minute, two-person Zoom VP
  meeting with separation on, the load line read about 32% of 16 CPUs in every minute
  but one; that meeting was not repeated with separation off, so the diarizer's share
  of the load is **not measured**. In the one minute that differed, a single diarizer
  step took 34.1 s, the engine stopped returning text for about 35 s, and it dropped
  about 1.66 s of audio (see the backlog bullet below). Load that minute fell to 18%,
  so the engine was blocked rather than short of CPU. The cause is not known, and one
  occurrence in one meeting says nothing about how often it happens. Whether
  diarization delays rows reaching the transcript is not measured either. On Amazon
  Transcribe the switch changes nothing billed: the VP requests speaker labels on
  every session and the switch only decides whether it reads them. See
  [Cost of voice separation](virtual-participant.md#cost-of-voice-separation).
- When the engine falls behind the incoming audio, it holds a bounded backlog
  (`ASR_MAX_QUEUE` frames, default 64) and waits up to `ASR_MAX_WAIT_MS` (default
  2000) for room before dropping a frame, so memory stays fixed. Dropped audio is
  never transcribed, but it still counts on the transcript's timeline: rows after a
  drop carry the time their audio arrived, so they stay aligned with the client's
  clock and with the meeting's active-speaker changes. Each drop is logged as
  `engine behind, dropped 0.128s of audio at ... cumulative dropped ...`, the
  session's end-of-session line reports `dropped=` and `dropped_seconds=`, and the
  `termination` message carries `dropped_seconds`. A nonzero total means the engine
  could not keep up at some point in that meeting, whether from load or from a
  single slow step (look for `sortformer step ... took` nearby).
- `MaxMeetingSeconds` in the `AsrDefaults` mapping (default 4 h, service maximum 8 h)
  bounds what a single
  MicroVM can cost. The transcriber terminates the MicroVM on meeting end and on
  SIGTERM (deploys, scale-in); the ceiling is the backstop if the task dies
  without doing either.

## Local development

The transcriber can talk to an ASR server running on your machine, with no AWS
involved:

```bash
# 1. Run the ASR server on the bundle's weights. The catalog entry names the archive
#    (or, for a model pinned file by file, one URL per file: download each into models/);
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
- **Languages other than English have been measured only on synthetic clips**, in
  seven locales (see [Languages](#languages)). The default bundle's other 13
  languages, and every language on real meeting audio, have not been measured.
- **Diarization error rate has not been measured** end to end. The *operating point*
  for the shipped speaker model has been measured on real meeting audio (see
  `measured` in `catalog.json`, and
  [Calibrating a new bundle](#calibrating-a-new-bundle-developers) for the tool), but
  that is the threshold, not a DER figure.
- **The diarizer's share of a meeting's CPU load**, and whether it delays rows, have
  not been measured: no meeting has been run both with and without Virtual
  Participant voice separation (see [Cost and sizing](#cost-and-sizing)).
- **MicroVM launch time and the real-time factor** of two concurrent channel
  sessions on one MicroVM have not been measured on Graviton. If a meeting's first
  transcript is slow to appear, or transcripts lag live audio, raise
  the bundle's `baselineMemoryMiB` in `catalog.json` and re-run
  `scripts/sync_bundles.py`. Note the ceiling: the `al2023-1` base MicroVM image
  accepts only **512, 1024, 2048, 4096 or 8192 MiB**, so 8192 is as large as
  a MicroVM gets and there is no headroom above the default. The image resolver
  refuses anything else up front rather than letting the stack fail minutes later at
  `AWS::Lambda::MicrovmImage`.

Measure these in a dev stack before relying on the engine for production meetings,
and keep Amazon Transcribe until you have (see [Choosing the engine](#choosing-the-engine)).
Every number needed is in the MicroVM log group: per-session summaries carry audio seconds,
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

It also degrades well: a partial has no known speaker yet (unless the client asked for
provisional labels, see [Partials name nobody](#partials-name-nobody)), so it shows the
bare channel name and gains the suffix when the label arrives, rather than displaying
somebody else's name in the meantime.

Labels are per meeting and per channel, and they are not identities. Mapping them onto
real names is a separate step, and it works far better when every label is distinct.
The **Speakers** panel on the meeting page is where that mapping is made: it lists one
row per voice and applies a name to every segment that voice produced, past and future
alike. See
[Naming the Voices](transcription-and-translation.md#naming-the-voices) — it is
engine-independent, so it reads the same whether these labels came from here or from
Amazon Transcribe.

**A Virtual Participant with its voice assistant runs two sessions**, like Stream Audio's
two channels. Meeting audio is on `REMOTE`, and the assistant's own voice is on
`LOCAL` under the VP's name, never diarized. Wake phrases are detected on meeting rows
only, so the assistant cannot wake itself.

**A Virtual Participant or Chrome extension meeting-audio row is named from the
meeting page as well as the voice.** Both read the meeting page's active speaker. A
row is named after the attendee the page showed for most of the row's audio. With
voice ids on, once a voice has been heard long enough alongside one attendee, it is
named after that attendee instead, and its voice id appears only under that
attendee's name. The rules, their thresholds and what they did on a measured meeting
are in [Naming rows from the voice as well](virtual-participant.md#naming-rows-from-the-voice-as-well);
the extension's version is described in
[How meeting speakers are named](browser-extension.md#how-meeting-speakers-are-named).
A channel with no page to read, such as Stream Audio's, keeps the format above.

## Known limitations

- **The meeting languages it takes depend on the deployed bundle** (see
  [Languages](#languages)), and **a row's language is identified from its final
  text** (see [Row languages](#row-languages)): live rows and rows of one or two
  words carry none, and the region is the meeting's or a fixed one, not detected.
  `identify-language` and `identify-multiple-languages` stay on Amazon Transcribe.
  `language`, `punctuate` and `latency_mode` are accepted by the protocol but do not
  change behaviour.
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
streaming-meetings engine in the **Transcription Engine** section of **Configuration ▸ Configuration** is the on-demand engine. A
deployment without the engine (`EnableMicrovmAsr=false`, or a Region without AWS
Lambda MicroVMs) never shows it.

**Transcripts appear but are labelled by channel.** The image has no speaker model.
Look for `diarization was requested but this ASR image has no speaker model baked
in` in the transcriber log.

**Meetings fall back to Amazon Transcribe.** Look for `MicroVM ASR could not
start` in the transcriber log; the reason from the launcher is logged with it
(quota, region, image not ready). To make the failure loud instead, set
`ASR_FALLBACK_TO_TRANSCRIBE=false` on the transcriber task — meetings then produce
no transcript when the engine is unavailable.

**Removing the engine fails with "the bucket you tried to delete is not empty".** A
stack deployed before the image-source resource learned to empty its bucket left one zip
per image rebuild behind. Empty the bucket, then retry the delete:

```bash
B=<the AsrImageSourceBucket name from the failed nested stack>
aws s3api list-object-versions --bucket "$B" \
  --query '{Objects: [Versions[].{Key:Key,VersionId:VersionId}, DeleteMarkers[].{Key:Key,VersionId:VersionId}][], Quiet: `true`}' \
  --output json > /tmp/objects.json
aws s3api delete-objects --bucket "$B" --delete file:///tmp/objects.json
aws cloudformation delete-stack --stack-name <the nested ASR stack>
```

**The image build fails.** Check `/aws/lambda-microvms/<stack>-asr-logs`. A `SHA256
mismatch` means the pinned checksum does not match the download (for a model pinned
file by file, the label names the file); `model file ... is not in the archive` means
the file names in the catalog entry are wrong; `HTTP 404` means the URL is wrong.

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
| `py3langid` row-language classifier, with its bundled model (every bundle) | BSD-3-Clause |
| NVIDIA FastConformer streaming EN 480 ms (ASR, FastConformer bundles and the Parakeet v3 live preview) | CC-BY-4.0 |
| NVIDIA Nemotron 3.5 ASR streaming 0.6B (ASR, `nemotron35-titanet-small` and the Nemotron 3.5 live preview) | OpenMDW-1.1 |
| NVIDIA Nemotron speech streaming EN 0.6B (ASR, `nemotron-titanet-small`) | NVIDIA Open Model License, not redistributable |
| NVIDIA Parakeet TDT 0.6B v3 and v2 (ASR, offline bundles) | CC-BY-4.0 |
| Qwen3-ASR 0.6B (ASR, offline Qwen bundles; third-party ONNX export re-hosted by k2-fsa) | Apache-2.0 |
| Cohere Transcribe 03-2026 2B (ASR, `cohere-transcribe-titanet-small`) | Apache-2.0 |
| NVIDIA Nemotron 3 Diarization (diarization, `-sortformer` bundles; third-party ONNX export by onnx-community) | OpenMDW-1.1 |
| Silero VAD v5 (utterance segmentation, offline bundles) | MIT |
| NVIDIA TitaNet-small (speaker embedding) | CC-BY-4.0 |
| pyannote segmentation 3.0 (turn detection) | MIT |

Where a model's download ships a licence file, it is copied into the image alongside its weights, and the
resolved licences are reported in the ASR stack's `AsrModelLicense`, `AsrLicenceSummary`
and `AsrRedistributable` outputs. `AsrRedistributable` is `false` only for
`nemotron-titanet-small`. CC-BY-4.0 requires attribution, which
`THIRD-PARTY-LICENSES.txt` carries.

Every checksum in `catalog.json` was verified by downloading the artifact and hashing it.

## See Also

- [Stream Audio](stream-audio.md)
- [Desktop Capture App](desktop-capture-app.md)
- [Transcription & Translation](transcription-and-translation.md)
- [WebSocket Streaming API](websocket-streaming-api.md)
- [CloudFormation Parameters Reference](cloudformation-parameters.md)
