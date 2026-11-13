---
title: "Upload Audio"
---

# Upload Audio

## Table of Contents

- [Overview](#overview)
- [When to Use Upload Audio](#when-to-use-upload-audio)
- [Supported File Formats](#supported-file-formats)
- [Step-by-Step Walkthrough](#step-by-step-walkthrough)
- [Speaker Diarization](#speaker-diarization)
- [Transcription Language](#transcription-language)
- [What Happens After the Upload](#what-happens-after-the-upload)
- [Limits & Best Practices](#limits--best-practices)
- [Troubleshooting](#troubleshooting)
- [See Also](#see-also)

## Overview

The **Upload Audio** tab in the LMA web UI lets you drop in an existing audio
or video recording and have it transcribed and summarised by the same LMA
pipeline that runs for live meetings. The file is uploaded directly from your
browser to Amazon S3 (never transiting a Lambda function), Amazon Transcribe
runs a batch job, and the resulting transcript and Bedrock-generated summary
appear on the normal meeting detail page — indistinguishable from a
live-streamed meeting.

## When to Use Upload Audio

- You recorded a meeting locally (Zoom cloud recording, Chime download,
  OBS capture, a phone voice memo, etc.) and want LMA to transcribe + summarise
  it after the fact.
- You have legacy recordings that predate LMA and you want them searchable
  alongside new meetings.
- You're running LMA in a browser that doesn't support screen-audio capture,
  or on a device where the microphone / screen-share permissions aren't
  available.
- You want to experiment with different diarization settings against the same
  recording (re-upload with different Max Speakers values).

## Supported File Formats

Any audio or video format Amazon Transcribe accepts for batch jobs, including:

| Type   | Extensions                           |
| ------ | ------------------------------------ |
| Audio  | `wav`, `mp3`, `m4a`, `flac`, `ogg`, `amr` |
| Video  | `mp4`, `webm` (audio track only is transcribed) |

The file is selected from the user's device via the standard `<input type="file">`
picker; no drag-and-drop of URLs and no cloud-storage browsing.

## Step-by-Step Walkthrough

1. In the LMA UI, click **Upload Audio** in the sidebar (under *Sources*).

2. Fill in the meeting form:

   - **Meeting Topic** — prefix for the unique meeting identifier (appended
     with a timestamp).
   - **Participants** — label for the other side of the conversation.
   - **Meeting owner** — your name (or any label you want used for the
     `Owner` of the meeting record; defaults to your email).

3. Click **Choose file** and pick the recording. A preview of the filename /
   size / last-modified time is shown below the picker.

4. *(Optional)* Check **Diarize speakers** if the recording contains multiple
   voices mixed into a single channel, and set **Max speakers** to the expected
   number (2–30). LMA maps the resulting `spk_N` labels onto the transcript so
   the detail page shows distinct speakers.

5. Click **Upload & Transcribe**. The progress bar shows:

   - *Preparing secure upload…* — the UI is asking AppSync for a 15-minute
     presigned PUT URL.
   - *Uploading file to Amazon S3… (N%)* — the file streams directly from
     your browser to S3.
   - *Upload complete. Transcription will begin shortly.* — hand-off to the
     backend pipeline.

6. Once the upload finishes, a green alert appears with the **Meeting ID**, the
   transcription language, and a link **Open meeting detail page**. Initially
   the status is *In progress* while Transcribe runs; when the batch job
   finishes (typically a few seconds for short clips, a few minutes for long
   recordings), the status flips to *Ended* and the transcript + summary render
   on the detail page.

## Speaker Diarization

If your recording is a single-channel mix (e.g. a phone call captured from a
single microphone), enable **Diarize speakers** and set **Max speakers** to the
expected number of voices (2–30). Amazon Transcribe uses the
`ShowSpeakerLabels` feature to assign each utterance a label like `spk_0`,
`spk_1`, `spk_2`, etc.

On the meeting detail page, the transcript lines are labelled with those
speaker IDs. Every speaker in an uploaded recording is on the `REMOTE` channel,
since an upload has no local microphone; the `spk_N` label is what tells the
speakers apart.

Diarization is requested with every language setting, including language
identification. Amazon Transcribe documents that
[batch language identification can be combined with any other feature](https://docs.aws.amazon.com/transcribe/latest/dg/lang-id-batch.html#lang-id-batch-features);
an upload with language identification and diarization together has not been
run against a deployment.

## Transcription Language

An upload is transcribed in the language set in the Amazon Transcribe section
of the Configuration page, the same setting that streaming meetings and Virtual
Participants use. Uploads always run as Amazon Transcribe batch jobs: the ASR
MicroVM engine (`EnableMicrovmAsr`, on by default) is not used for uploads, even
on a deployment where it transcribes the streaming meetings. The settings are read
when the upload starts. A change saved later applies to the next upload, and an
upload already started keeps the language it started with.

| Amazon Transcribe › Language | What the upload uses |
| ---------------------------- | -------------------- |
| A language, such as `ja-JP` | That language |
| `identify-language` | Amazon Transcribe identifies the one language of the recording among the **Language options** |
| `identify-multiple-languages` | Amazon Transcribe identifies every language spoken in the recording among the **Language options** |

When the upload starts, it is refused with the reason shown on the page if the
settings combine options an upload cannot use. With language identification,
an upload needs:

- **Language options** with at least two languages, at most one dialect of each
  language (for example not both `en-US` and `en-GB`). Batch language
  identification does not accept a single candidate language.
- No **Custom vocabulary** and no **Custom language model**.

**Preferred language** does not apply to uploads: batch transcription has no
preferred language. **Redact personal information**, **Custom vocabulary** and
**Custom language model** are not applied to uploads.

The **Upload complete** message names the language, for example
*Transcription language: Japanese (Japan), ja-JP*. The **Details** tab of the meeting
shows the same language setting. With language identification it reads
*Identified by Amazon Transcribe (one language)* or *(several languages)*, and
once the job finishes, the languages Amazon Transcribe identified are listed
under **Detected languages**, as for a streaming meeting. With
`identify-language` every transcript line is in the one language identified;
with `identify-multiple-languages` each line takes the language most of its
words were identified in, and lines are marked with a language code when more
than one language was heard.

A client of the `createUploadMeeting` GraphQL mutation can set `languageCode`
to a language code such as `de-DE`: any language Amazon Transcribe batch
transcription supports, including ones the Configuration page does not offer.
A code it does not support is refused when the upload starts. That language is
used, and the Configuration page's settings are neither read nor checked. The response's
`transcriptionLanguage` is the applied language: a language code, or
`identify-language` / `identify-multiple-languages`.

## What Happens After the Upload

```
Browser PUT (presigned URL)
      │
      ▼
S3 bucket (lma-uploads-pending/<callId>/)
      │
      ▼ (S3 ObjectCreated notification)
Upload Meeting Processor Lambda
      │  • Emits START event onto CallDataStream (Kinesis)
      │    — the meeting appears in Meetings List as "In Progress"
      │  • Starts Amazon Transcribe batch job
      ▼
Amazon Transcribe (async)
      │
      ▼ (EventBridge "Transcribe Job State Change")
Upload Meeting Finalizer Lambda
      │  • Reads the transcript JSON
      │  • Emits one ADD_TRANSCRIPT_SEGMENT per utterance
      │  • Promotes media → lma-audio-recordings/ and emits ADD_S3_RECORDING_URL
      │  • Emits END → triggers the existing summary orchestrator
      ▼
Bedrock summary + meeting detail page rendered
```

The entire server-side pipeline is shared with live Stream Audio meetings — the
only thing that differs is that the transcript comes from Transcribe's batch
API instead of the streaming API.

## Limits & Best Practices

- **Max file size: 5 GB.** Enforced client-side and by the presigned S3 PUT.
- **Presigned URL TTL: 15 minutes.** Start the upload promptly after clicking
  the button; for very large files on slow networks, consider splitting or
  re-running.
- **Language:** the language set in **Amazon Transcribe › Language** on the
  Configuration page when the upload starts, or language identification among
  the **Language options** (at least two) when it is set to identify the
  language, unless the `createUploadMeeting` request sets `languageCode`.
- **Pending uploads expire after 7 days.** Files left in
  `lma-uploads-pending/` are auto-cleaned by an S3 lifecycle rule, so a
  failed or abandoned upload will not accumulate storage cost.
- **Existing meetings list permissions apply** — a user can only see an
  uploaded meeting if they are the owner or it has been shared with them
  (standard LMA UBAC rules).

## Troubleshooting

**Upload succeeds but the meeting shows "In Progress" forever.**  
Check the `UploadMeetingFinalizer` Lambda logs in CloudWatch for a condition
failure on `updateCallStatus`. If the Transcribe batch job completed but the
finalizer never ran, verify the EventBridge rule
`<stack>-UploadMeetingFinalizer` is enabled.

**Transcription job fails with "Unable to access the S3 URI".**  
Confirm that both the `AllowTranscribeRead` and `AllowTranscribeWrite`
statements are present in the recordings bucket policy (they are installed by
`lma-main.yaml` on deploy). Deploys older than v0.3.2 did not include these;
redeploy to the latest version.

**File type rejected in the UI.**  
The UI restricts selection to `audio/*` / `video/*` MIME types as reported by
the browser. If the browser doesn't detect a MIME type (e.g. a `.opus` file
presented as `application/octet-stream`), re-encode to a supported format or
rename the file extension.

**Speaker labels look wrong.**  
Diarization accuracy depends on audio quality, channel separation, and how
similar the voices are. For single-speaker recordings disable diarization
entirely; for multi-speaker recordings make sure **Max speakers** matches the
actual number of distinct voices.

## See Also

- [Stream Audio](stream-audio.md) — live streaming variant of this feature
- [Web UI Guide](web-ui-guide.md) — general UI walkthrough
- [Transcription & Translation](transcription-and-translation.md) — language
  options, custom vocabulary, PII redaction settings
- [Embeddable Components](embeddable-components.md) — how to embed the
  Upload Audio page in your own app via
  `/#/embed?component=upload-audio`
