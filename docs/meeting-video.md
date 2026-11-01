---
title: "Meeting Video and Key Frames"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Video and Key Frames

When a meeting's video is recorded, LMA also keeps **key frames**: a picture of
the shared screen each time it changes to something new and holds still — each
slide of a deck, each page of a document, each new view of an application. Key
frames are stored on the meeting beside the transcript, with times on the
transcript's clock, so they can be lined up with what was said while each one
was on screen. The meeting page shows them as a strip of thumbnails, below.

## On the meeting page

A meeting with key frames has a **Shared screen** section on its page, below the
summary: a strip of thumbnails in time order, each labelled with the time it
went up.

- **Selecting a thumbnail** shows the picture large and moves the transcript to
  that time, highlighting the segment that was under way. During a live meeting
  this pauses following the newest segment, as scrolling up does; **Resume** in
  the transcript returns to it.
- **After the meeting**, when it has a video recording, selecting a thumbnail
  also plays the recording from that frame. **Play recording from here** does
  the same for the frame already selected, and **Open full size** opens the
  picture in a new tab.
- **During the meeting**, new frames are added to the end of the strip as they
  are taken, and the strip follows them unless it has been scrolled back.
- **Long meetings** load a hundred frames at a time; the next hundred load as
  the strip is scrolled to its end (or with **Load more**).
- **Find text on the shared screen** filters the strip to the frames whose
  [slide text](#slide-text) holds every word typed, in any order, ignoring case
  and accents, with the line that matched under each thumbnail. It searches the
  whole meeting: the frames not loaded yet are loaded first, and the frames
  shown are read again if any of them was loaded before its text was read
  (the text follows its frame by a few seconds). A selected frame shows its
  text under the picture.
- **Chapters**: a frame that begins one of the meeting's
  [chapters](#chapters) is labelled with the chapter's title.
- **Export slides (PDF)** opens every frame as a printable deck; see
  [Exporting the slides](#exporting-the-slides).

The pictures are shown through links signed by the server for whoever opened
the page, after the same access check as the transcript, and they last an
hour; the strip renews them when one has expired. A meeting without key frames
shows no section at all.

## Slide text

Each key frame's text is read once, a few seconds after the frame is stored: a
vision model (the `KeyframeTextModelId` stack parameter, Amazon Nova Lite by
default) transcribes the readable text, top to bottom, and adds a line in
brackets for a chart, diagram or picture, for example
`[Bar chart of revenue by quarter]`. It is told not to describe or name people.
The text is stored on the frame (`SlideText`; empty when the frame holds none)
and is what:

- the [meeting assistant](#asking-the-assistant-about-the-shared-screen) reads
  first, before it looks at any picture;
- the meeting page [searches](#on-the-meeting-page);
- the meeting-history knowledge base indexes, so the **meeting history** tool
  and the MCP `search_meetings` tool find a meeting by what was shown as well as
  by what was said. The text is added to the meeting's transcript document, as a
  **SHARED SCREEN** section with each frame's time, when the meeting's summary
  is written (stacks that summarize with Bedrock, which write that document).

A vision model was chosen over Amazon Textract by measurement, on the synthetic
meeting's frames with their true text known:

| Method | Words read (recall / precision), 4 slides | Dense document page (22 lines) | Cost per frame |
|---|---|---|---|
| Amazon Nova Lite | 100% / 100% | 100% / 100% | about $0.0002 |
| Amazon Nova 2 Lite | 100% / 100% | 100% / 100% | about $0.0002 |
| Claude Haiku 4.5 | 100% / 100% | 100% / 100% | about $0.0019 |
| Amazon Textract (`DetectDocumentText`) | 100% / 97% | 100% / 99.6% | $0.0015 |

Every method read every word; the model costs a tenth of Textract and also says
what a chart shows, which Textract does not. The text read is metered against
the meeting under **Slide text** in its [cost estimate](meeting-cost-estimate.md).

## Slides presented in the summary

A meeting with key frames gets a **SLIDES PRESENTED** section in its
[summary](transcript-summarization.md): one entry per slide, in the order shown,
with the time it went up, its title, its thumbnail and a sentence or two on what
was said while it was on screen. The prompt asks the model to merge
consecutive frames of one slide (the same slide twice, or a slide built up
point by point) into one entry, and to leave out frames with nothing on them,
such as an empty desktop; how well it does so depends on the model (see the
measurements below). A meeting without
key frames gets no such section, as a meeting without an agenda gets no agenda
section.

How it is built:

- The fetch-transcript function gives each key frame **what was said** while it
  was on screen: the transcript segments that began between it and the next
  frame, with the speaker names the transcript shows.
- The section's prompt names the `{slides}` placeholder, which the summarizer
  fills with each frame's time, its picture reference
  (`![Slide at 12:30](keyframe:<FrameId>)`), its [slide text](#slide-text) and
  what was said, and the prompt asks the model to copy the picture reference
  exactly. The transcript itself is not sent with this section. At most 60
  frames are listed (past that, the ones on screen for the shortest time are
  left out), with each frame's text cut at 1,500 characters and its speech at
  3,000 (the fetch-transcript function sends no more than that).
- The meeting page and the embeddable summary panel show each picture
  reference as the frame's thumbnail, read through `getVideoKeyframes` for that
  meeting with the same access check as the strip; selecting it opens the full
  picture. A frame that cannot be read, because the meeting's retention has
  removed it, shows as its alt text. Copy, download, Word, email, the meetings
  list's summary preview and the MCP `get_meeting_summary` tool show the alt
  text too, and so does the summary document for the meeting-history knowledge
  base.

The section is a prompt in the `summary_prompt_templates` section like any
other, so it can be edited, replaced or removed (set it to `NONE`) on the
**Transcript Summary** page. A prompt of your own can use `{slides}` as well.

Measured with the shipped prompt on the synthetic meeting (14 key frames: ten
slides, one of them taken twice, and three pages of a document), every model
copied every picture reference exactly, and none copied the `Image:` label
with it:

| Model | Entries | Input / output tokens | Cost for the section |
|---|---|---|---|
| Claude Sonnet 5.5 (the stack's default summary model) | 11 | 2,895 / 1,001 | about $0.016 |
| Claude Haiku 4.5 | 13 | 1,943 / 816 | about $0.006 |
| Amazon Nova 2 Lite | 11 | 1,753 / 598 | about $0.002 |
| Amazon Nova Lite | 13 | 1,893 / 757 | about $0.0003 |

A reasonable count is 11 to 13, depending on whether the three document pages
are taken as one item or three, with the repeated slide merged; every result
falls in that range. The cost is metered with the rest of the
summary, under **Summary**.

## Chapters

The summary has a **CHAPTERS** section: one line per main topic of the
meeting, `- [MM:SS] Title`, at the time the topic was first discussed or a new
slide started it. The meeting page reads that section and shows a **Chapters**
list below the summary. Selecting a chapter moves the transcript to its time
and, after the meeting, plays the video recording from there, as selecting a
key frame does. In the **Shared screen** strip, the frame that went up within
30 seconds of a chapter's start is labelled with the chapter; a chapter that
began without a change of slide labels no frame.

How it is built:

- The fetch-transcript function returns, when asked (`IncludeTimes`), the
  transcript with each line's time and a line at each change of the shared
  screen with the start of the new frame's text:
  `[04:00] (The shared screen changed: Q3 Budget - Spend is 4% under plan)`.
- The section's prompt names the `{timed_transcript}` placeholder, which the
  summarizer fills with that text. The prompt asks for one line per chapter,
  with each time taken from a line of the transcript. Chapters therefore work
  for any meeting with a transcript, with or without video; the slide changes
  only help place them.
- Chapter times are on the transcript's clock. To play the recording from a
  chapter, the page converts the time to the recording's clock using the key
  frames, which carry both. A meeting without key frames plays from the
  transcript time, which can differ from the recording by the moments between
  the start of the recording and the start of transcription.
- The page reads the section from a generated summary, or from under its
  **CHAPTERS** heading once the summary has been edited by hand. It accepts the
  usual variations of list mark, brackets and bold, and leaves out any line
  without a readable time.

Measured with the shipped prompt on a synthetic 24-minute meeting with five
topics (two started by a new slide, three by discussion only), every model
returned only lines the page reads and took every time from the transcript:

| Model | Chapters | Seconds from each true topic start | Input / output tokens | Cost |
|---|---|---|---|---|
| Claude Sonnet 5.5 (the stack's default summary model) | 5 | 0, 0, 0, 0, 0 | 1,795 / 82 | about $0.004 |
| Claude Haiku 4.5 | 5 | 0, 0, 0, 0, 0 | 1,340 / 65 | about $0.002 |
| Amazon Nova 2 Lite | 5 | 0, 210, 0, 2, 0 | 1,297 / 50 | about $0.0005 |
| Amazon Nova Lite | 6 | 0, 2, 0, 2, 0 | 1,383 / 96 | about $0.0001 |

The prompt asks for the line where a topic is first discussed (not where an
agenda mentions it) and for at least two minutes between chapters, because
without those instructions the smaller models split the meeting into far more
chapters or placed them at the agenda's mention of each topic. One synthetic
meeting is a small sample, so expect chapter boundaries from the smaller models
to be less reliable on real meetings.

## Exporting the slides

**Export slides (PDF)**, in the **Shared screen** section, opens the meeting's
key frames in a new tab as a deck, one landscape page per frame in time order,
each with its picture at full size (1568 pixels wide), its time, the chapter it
begins, and its [slide text](#slide-text). It then opens the browser's print
dialog, where **Save as PDF** saves it. All of the meeting's frames are read
again for the export, so their links are fresh, and the dialog opens once
the pictures have loaded. The tab opens as soon as the button is selected, and
shows that the deck is being prepared until then.

The deck is printed by the browser rather than built as a PDF file in the page,
so any language or script on the slides prints as it shows on screen. A PDF
library embeds only the fonts it ships, which leaves characters outside them
blank. The trade-off is that the user takes the last step in the print dialog.
The browser must allow the page to open a new tab; if it blocks the tab, the
section says so.

## Asking the assistant about the shared screen

The meeting assistant has a `get_meeting_frames` tool that reads the key frames
a question is about — the last few, those in a time range, or the one on screen
at a moment — each with its [slide text](#slide-text) and what was said while it
was on screen. For example:

- "Summarize that slide." / "What's on this slide?"
- "Translate the last slide into French."
- "Show me the last two slides."
- "What was on screen at 12:30?" / "Which slides did we see between 10:00 and 20:00?"

The text usually answers the question, so pictures are sent only when it does
not: when the assistant asks to look (a chart, a diagram, a layout), or for a
frame whose text has not been read yet. They go to the model as images when it
reads them, which every model the stack offers does except Amazon Nova Micro;
with Nova Micro the assistant answers from the text, the times and the
transcript. At most four pictures are sent with one tool call, and at most ten
frames are listed.
The assistant is told to describe what a frame shows and never to identify
people from their faces.

Where the answer appears decides how frames are shown:

| Where the question was asked | Frames in the answer |
|---|---|
| The meeting page's chat panel | Shown as pictures in the answer; selecting one shows it full size over the chat. The answer refers to a frame by its id and the page fetches a signed link for it through `getVideoKeyframes`, with the same access check as the strip. |
| The meeting chat (`LMA ask`, the Virtual Participant) | Described in words. Everyone in the meeting reads the answer, and the frames show what was shared with them. |
| The voice assistant | Described in words, and spoken. |
| The wake phrase (an answer written into the transcript) | Described in words. |

**Cost.** A picture is billed as the model's input tokens, so it is included in
the Meeting Assistant line of the meeting's
[cost estimate](meeting-cost-estimate.md), and the number of pictures sent is
listed beside it as an `images` count. Measured with one 1568 × 882 key frame on
Bedrock:

| Model | Input tokens one picture adds |
|---|---|
| Claude Haiku 4.5 | 1,564 |
| Amazon Nova Lite | 2,114 |
| Amazon Nova 2 Lite | 232 |
| Amazon Nova Micro | none: the model does not accept images |

## Which meetings have key frames

| Source | Key frames |
|---|---|
| **Virtual Participant** | Yes, when **Record meeting video** is on for the Virtual Participant (the toggle in the Create Virtual Participant modal; the deployment default is the `EnableVideoRecording` stack parameter). The VP samples its own meeting display, the same picture its screen recording captures. |
| **Desktop Capture App** | Yes, when **Also record screen video** is on in the app and the deployment records video (`EnableVideoRecording`). The WebSocket transcriber samples the video the app streams, as it arrives; see [Desktop video](#desktop-video). |
| **Stream Audio, Browser Extension, Upload Audio** | No. These sources carry no video. |

Sampling follows the meeting's recording: in the Virtual Participant it runs
only while the meeting is being transcribed, so nothing is captured before the
VP is admitted or while the meeting is paused from the chat; and nothing at all
is captured from either source when video recording is off.

## How a key frame is chosen

Once a second the VP takes a small grayscale copy of its display — a grid of
64 × 36 cells, each the average brightness of a block of the screen — and
compares it with two earlier pictures:

- **The previous second's**, to tell whether the picture is still moving. A
  slide transition, scrolling, a video or a camera is not captured while it
  moves; a frame is kept only once the picture has held still for two seconds
  (three without a screen-share signal, below).
- **The last key frame's**, to tell whether the settled picture is new. Going
  back to the slide that was just captured, or a share that stops and starts
  again on the same slide, does not produce another frame.

A cell counts as changed when its brightness differs by more than 12 (of 255).
Cells that keep changing — a strip of cameras beside the share, a video playing
in one corner — are recognised as moving and left out of both comparisons, so
they neither stop a slide from settling nor make it look new. No frame is kept
while more than half the picture is moving (a quarter without a screen-share
signal).

Each key frame is stamped with the moment its picture **first appeared**, not
the moment it was confirmed, so a slide that went up at 12:30 is listed at
12:30. Frames are at least four seconds apart, and a meeting keeps at most
1,000.

### Screen-share signal per platform

Where the meeting platform shows when someone's screen is being shared, samples
are judged only while a share is on screen, which keeps the camera gallery out
of the key frames. Where it does not, every sample is judged, against stricter
thresholds.

| Platform | Signal | When samples are judged |
|---|---|---|
| Zoom (web client and Zoom Meeting SDK) | The share view's container and the floating video window Zoom switches to during a share | While a share is on screen |
| Microsoft Teams (web client) | The screen-sharing video stream element | While a share is on screen |
| Microsoft Teams (Azure Communication Services SDK) | The call's state: a remote screen share or a PowerPoint Live presentation | While a share is on screen |
| Webex | None | Always, with the stricter thresholds |
| Amazon Chime | None | Always, with the stricter thresholds |

| Thresholds | Share on screen | No share signal |
|---|---|---|
| Part of the picture that must differ from the last key frame | 3% | 4% |
| Part that may still be changing for the picture to count as still | 2% | 1% |
| Seconds the picture must hold still | 2 | 3 |
| Largest part of the picture that may be moving | 50% | 25% |

Without a share signal, a gallery of moving cameras still produces no frames,
because the cameras are moving. A layout change that then holds still — someone
joins and the gallery rearranges, or every camera is off — can produce one.

The thresholds are low because slides built on one template differ less than
might be expected: on the calibration recording below, consecutive slides
differed in 4–7% of the grid.

### Measurements

Measured in the Virtual Participant's container image limited to 2 vCPUs, on a
synthetic meeting of 1920 × 1080: ten slides with a strip of four moving cameras,
a minute of a 3 × 3 camera gallery, a document scrolled for eight seconds, and a
slide built up one bullet at a time.

| | Result |
|---|---|
| CPU of the sampling process | 0.6% of one core (the screen recording's encoder: 7.3%) |
| CPU per key frame (one capture, two JPEG encodes) | about 125 ms |
| Size per key frame | about 70 KB (1568 × 882 image) + 6 KB (320 × 180 thumbnail) |
| Slides captured, with a share signal / without | 10 of 10 / 10 of 10 |
| Frames from the camera gallery | 0 / 0 |
| Document: when shown, and after the scroll stopped | 2 of 2 / 2 of 2 |
| Bullet added to a slide already captured (3% of the grid, at the threshold) | missed in both modes; a replay of the same samples caught it with a share signal |

The alternative considered, FFmpeg's own scene-change filter on the five-frames-
a-second recording picture, used 1.8% of a core; it compares each frame only
with the one before, which is what makes a camera strip or a slow transition
trip it.

### Desktop video

The desktop capture app sends its screen or window as H.264 at 5 frames a
second, with a full picture (an H.264 key frame) at least every two seconds. The
transcriber decodes only those full pictures, as the video arrives, and judges
each one exactly as the Virtual Participant judges its samples. The app gives no
screen-share signal — what it captures is whatever the user chose to share with
LMA — so every picture is judged against the stricter thresholds, except that
two still pictures in a row (about four seconds) are enough to settle, close to
the Virtual Participant's three seconds.

Each key frame's time is placed on the transcript's clock from the offset the
app reports between the start of its audio and the start of its video, which is
also how the recording is lined up with the audio. If the app restarts its video
during the meeting, the frames after the restart are placed by the time they
arrive and have no recording position, because the recording joins the two
parts end to end.

Measured in the transcriber's own container image limited to 2 vCPUs, with the
synthetic meeting above recorded as the app sends it (1920 × 1080, 5 fps, 600
kbps) and streamed in real time:

| | Result |
|---|---|
| CPU to decode and judge one video stream | 1.4% of one core |
| Pictures judged | 134 in 266 seconds (one every two seconds) |
| Slides captured | 10 of 10 |
| Frames from the camera gallery | 0 |
| Document: when shown, and after the scroll stopped | 2 of 2 |

The thresholds and limits are read from environment variables of the Virtual
Participant process — for example `KEYFRAME_CHANGE_FRACTION`,
`KEYFRAME_STRICT_CHANGE_FRACTION`, `KEYFRAME_MIN_INTERVAL_MS` and
`KEYFRAME_MAX_PER_MEETING`; see `keyframe-sampler.ts` — and, for desktop
video, of the transcriber (`KEYFRAME_DESKTOP_CHANGE_FRACTION` and the others in
`videokeyframes.ts`). They are not stack parameters: they are meant for local
runs and calibration, and a value added to a task definition by hand is replaced
at the next stack update.

## What is stored

Each key frame is two JPEG images in the recordings bucket, under
`lma-video-keyframes/<meeting>/` — a large one (1568 pixels wide) for reading the
slide and a thumbnail (320 pixels wide) for lists — and an item on the meeting
in the event-sourcing table (partition key `kf#<CallId>`). `<meeting>` is the
CallId with every character other than letters, digits, `_` and `.` replaced by
`_` and any leading and trailing `_` removed, which is how meeting deletion
finds the images. The item has:

| Field | Meaning |
|---|---|
| `FrameId` | The frame's id: its time in milliseconds, zero-padded, and a short suffix. Frames sort by it in time order. |
| `Timestamp` | Seconds on the transcript's timeline, the same clock as a transcript segment's start time. |
| `VideoOffset` | Seconds into the meeting's video recording, for playing the recording from that frame. |
| `ImageUrl`, `ThumbnailUrl` | Where the two images are stored. |
| `Source` | What took the frame: `VIRTUAL_PARTICIPANT` or `DESKTOP_CAPTURE`. |
| `ShareDetected` | `true` when the frame was taken while the platform showed a share; absent on platforms without the signal and for desktop video. |
| `ChangeScore` | The part of the picture that differed from the previous key frame. |
| `SlideText` | The text on the frame, read once after it is stored (see [Slide text](#slide-text)); empty when it holds none. |

### Retention, access and deletion

- **Retention.** Key frames are kept as long as the meeting's turn-by-turn
  transcript: the items carry the transcript's expiry
  (`TranscriptionExpirationInDays`), and a lifecycle rule on
  `lma-video-keyframes/` deletes the images after the same number of days.
- **Access.** Key frames are meeting data with the transcript's access rule:
  `getVideoKeyframes` and `onAddVideoKeyframe` return them to the meeting's
  owner, to the people it is shared with, and to administrators. Sharing or
  unsharing a meeting shares or unshares its key frames with it. The images
  are private objects in the recordings bucket, which the web UI's identity
  cannot read: `getVideoKeyframes` checks the caller's access to the meeting
  and then signs an hour-long link to each image, and only for an image stored
  in that meeting's own folder of this deployment's bucket.
- **Deletion.** Deleting a meeting deletes its key frames and their images.

## Reading key frames

The AppSync API has a query and a subscription:

```graphql
query {
  getVideoKeyframes(callId: "<CallId>", limit: 100, nextToken: null, afterFrameId: null, frameId: null) {
    VideoKeyframes { FrameId Timestamp VideoOffset ShareDetected ThumbnailSignedUrl ImageSignedUrl }
    nextToken
  }
}

subscription {
  onAddVideoKeyframe(CallId: "<CallId>") {
    CallId FrameId Timestamp Owner SharedWith
  }
}
```

The query returns frames in time order, up to 500 at a time (100 by default),
with `ThumbnailSignedUrl` and `ImageSignedUrl` filled in; `afterFrameId` returns
only the frames after the one given, which is how the meeting page fetches a
frame the subscription announced. `frameId` returns just that frame, and with it
`limit`, `nextToken` and `afterFrameId` are not used; it is how a chat answer's
picture is resolved. The subscription carries no signed links. As
with `onAddTranscriptSegment`, its selection must include `Owner` and
`SharedWith`.

### The `ADD_VIDEO_KEYFRAME` event

A key frame reaches the meeting as an `ADD_VIDEO_KEYFRAME` record on the call
data stream, which the call event processor stores and publishes. A producer
that uploads its own images can send the same event:

```json
{
  "EventType": "ADD_VIDEO_KEYFRAME",
  "CallId": "<CallId>",
  "FrameId": "0000754210-3fa2c1",
  "Timestamp": 754.21,
  "VideoOffset": 771.9,
  "ImageUrl": "https://<bucket>.s3.<region>.amazonaws.com/lma-video-keyframes%2F<meeting>%2F0000754210-3fa2c1.jpg",
  "ThumbnailUrl": "https://<bucket>.s3.<region>.amazonaws.com/lma-video-keyframes%2F<meeting>%2F0000754210-3fa2c1_thumb.jpg",
  "Width": 1568,
  "Height": 882,
  "Source": "VIRTUAL_PARTICIPANT",
  "ShareDetected": true,
  "ChangeScore": 0.05,
  "CreatedAt": "2026-11-01T10:12:34.210Z"
}
```

`Timestamp`, both URLs (HTTPS), a known `Source` and a `FrameId` of at most 64
characters are required; an event without them is logged and dropped, not
retried. The meeting must already exist. Owner and sharing are taken from the
meeting, and expiry from the deployment's transcript retention, not from the
event.

## What key frames are not used for

Key frames are pictures of the shared screen, kept so the meeting's content can
be found and read again. LMA does not analyse the people who appear in them: no
face detection or facial recognition is run on key frames or on the meeting
video, and nothing identifies participants from video. Speaker names come from
the meeting platform and the transcript, as described in
[Transcription & Translation](transcription-and-translation.md). Where the
platform gives no screen-share signal a key frame can show camera tiles, as
described above; they are stored like any other key frame and not analysed.
