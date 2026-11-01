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
was on screen. They are read through the AppSync API, described below.

## Which meetings have key frames

| Source | Key frames |
|---|---|
| **Virtual Participant** | Yes, when **Record meeting video** is on for the Virtual Participant (the toggle in the Create Virtual Participant modal; the deployment default is the `EnableVideoRecording` stack parameter). The VP samples its own meeting display, the same picture its screen recording captures. |
| **Stream Audio, Browser Extension, Upload Audio** | No. These sources carry no video. |

Sampling follows the meeting's recording: it runs only while the meeting is
being transcribed, so nothing is captured before the VP is admitted or while the
meeting is paused from the chat, and nothing at all when video recording is off.

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

The thresholds and limits are read from environment variables of the Virtual
Participant process — for example `KEYFRAME_CHANGE_FRACTION`,
`KEYFRAME_STRICT_CHANGE_FRACTION`, `KEYFRAME_MIN_INTERVAL_MS` and
`KEYFRAME_MAX_PER_MEETING`; see `keyframe-sampler.ts`. They are not stack
parameters: they are meant for local runs and calibration, and a value added to
the task definition by hand is replaced at the next stack update.

## What is stored

Each key frame is two JPEG images in the recordings bucket, under
`lma-video-keyframes/<meeting>/` — a large one (1568 pixels wide) for reading the
slide and a thumbnail (320 pixels wide) for lists — and an item on the meeting
in the event-sourcing table (partition key `kf#<CallId>`), with:

| Field | Meaning |
|---|---|
| `FrameId` | The frame's id: its time in milliseconds, zero-padded, and a short suffix. Frames sort by it in time order. |
| `Timestamp` | Seconds on the transcript's timeline, the same clock as a transcript segment's start time. |
| `VideoOffset` | Seconds into the meeting's video recording, for playing the recording from that frame. |
| `ImageUrl`, `ThumbnailUrl` | The two images. |
| `Source` | What took the frame: `VIRTUAL_PARTICIPANT`. |
| `ShareDetected` | `true` when the frame was taken while the platform showed a share; absent on platforms without the signal. |
| `ChangeScore` | The part of the picture that differed from the previous key frame. |

### Retention, access and deletion

- **Retention.** Key frames are kept as long as the meeting's turn-by-turn
  transcript: the items carry the transcript's expiry
  (`TranscriptionExpirationInDays`), and a lifecycle rule on
  `lma-video-keyframes/` deletes the images after the same number of days.
- **Access.** Key frames are meeting data with the transcript's access rule:
  they are visible to the meeting's owner, to the people it is shared with, and
  to administrators. Sharing or unsharing a meeting shares or unshares its key
  frames with it.
- **Deletion.** Deleting a meeting deletes its key frames and their images.

## Reading key frames

The AppSync API has a query and a subscription:

```graphql
query {
  getVideoKeyframes(callId: "<CallId>", limit: 200, nextToken: null) {
    VideoKeyframes { FrameId Timestamp VideoOffset ThumbnailUrl ImageUrl ShareDetected }
    nextToken
  }
}

subscription {
  onAddVideoKeyframe(CallId: "<CallId>") {
    CallId FrameId Timestamp VideoOffset ThumbnailUrl ImageUrl Owner SharedWith
  }
}
```

The query returns frames in time order, up to 500 at a time (200 by default).
As with `onAddTranscriptSegment`, the subscription's selection must include
`Owner` and `SharedWith`.

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
retried. The meeting must
already exist. Owner and sharing are taken from the meeting, and expiry from
the deployment's transcript retention, not from the event. `<meeting>` is the CallId with every character other than letters,
digits, `_` and `.` replaced by `_` and any leading and trailing `_` removed,
which is how meeting deletion finds the
images.

## What key frames are not used for

Key frames are pictures of the shared screen, kept so the meeting's content can
be found and read again. LMA does not analyse the people who appear in them: no
face detection or facial recognition is run on key frames or on the meeting
video, and nothing identifies participants from video. Speaker names come from
the meeting platform and the transcript, as described in
[Transcription & Translation](transcription-and-translation.md). Where the
platform gives no screen-share signal a key frame can show camera tiles, as
described above; they are stored like any other key frame and not analysed.
