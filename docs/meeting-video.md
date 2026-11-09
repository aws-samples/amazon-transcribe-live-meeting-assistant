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

A meeting with key frames has a **Screen share** tab on its page, with the
number of frames in its label: a strip of thumbnails in time order, each
labelled with the time it went up. The tab appears once the first frames have
loaded, including during a live meeting when the first frame is taken. Frames
of the meeting's camera video rather than shared content are not shown or
counted (see [Camera frames](#camera-frames)), and frames that repeat the
screen before them are hidden and not counted until asked for (see
[Repeated frames](#repeated-frames)).

- **Selecting a thumbnail** shows the picture large and moves the transcript to
  that time, highlighting the segment that was under way, for when the
  **Transcript** tab is next opened; **Show in transcript** opens it there.
  During a live meeting this pauses following the newest segment, as scrolling
  up does; **Resume** in the transcript returns to it.
- **After the meeting**, selecting a thumbnail also moves the meeting page's
  [player](web-ui-guide.md#the-recording-player) to that frame, without playing
  it. **Play recording from here** opens the **Transcript** tab and plays the
  recording from the frame selected, and **Open full size** opens the picture
  in a new tab.
- **A slide named in the summary** is a thumbnail on the **Summary** tab;
  selecting it opens this tab with that frame selected.
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
- **Labels**: a frame is labelled with its own title, a few words read with
  its [slide text](#slide-text); a frame without one that begins one of the
  meeting's [chapters](#chapters) is labelled with the chapter's title.
- **Repeated frames**: when some frames were found to show the same screen as
  an earlier one, the strip says how many are hidden, and **Show all N
  frames** shows them in place (**Hide repeated frames** hides them again). A
  search looks through the frames the strip shows, so with repeats hidden it
  finds the first frame of each screen. A repeated frame selected from the
  summary is shown either way.
- **Export slides (PDF)** opens every frame, without the repeated ones, as a
  printable deck; see [Exporting the slides](#exporting-the-slides).

The pictures are shown through links signed by the server for whoever opened
the page, after the same access check as the transcript, and they last an
hour; the strip renews them when one has expired. A meeting without key frames
shows no section at all.

## Slide text

Each key frame's text is read once, a few seconds after the frame is stored: a
vision model (**Key frame text** in the Amazon Bedrock Models section of the
[Configuration page](configuration-guide.md), Amazon Nova Lite unless changed) transcribes the readable text, top to bottom, and adds a line in
brackets for a chart, diagram or picture, for example
`[Bar chart of revenue by quarter]`. It is told not to describe or name people,
and to leave out the meeting's video tiles, small terminal windows and status
strips, the operating system's menu bar and taskbar, and window title bars. The
model's answer begins with two lines: what the frame shows (`CONTENT: SCREEN`,
`CAMERA` or `MIXED`; see [Camera frames](#camera-frames)) and a title of at most
eight words naming the main shared content. An answer without them is read as
`SCREEN` with no title, all of it text. The text is stored on the frame
(`SlideText`; empty when the frame holds none), with `ContentKind` and `Title`,
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

A model reading a small, dense strip of text can fall into a loop, writing the
same few words until it reaches its output budget (measured on a Desktop
Capture App meeting: three of 24 frames). The budget is 1,200 tokens, about
twice a dense page, and what is kept collapses a run of words or lines repeated
in a row to one copy and drops text that is still mostly repetition after that.
An answer cut off at the budget is logged and keeps its text, without its last,
cut-off line.

### Camera frames

A key frame is taken whenever the picture changes and then holds still, so a
camera view that fills the screen (a meeting window showing a room camera, in
the Desktop Capture App's capture of the whole display) yields frames when the
people in it move. On one measured meeting 10 of 24 frames were of that kind.
The vision model therefore also says what each frame shows: `SCREEN` (shared
slides, documents or applications), `CAMERA` (the meeting's video tiles or
camera), or `MIXED` (both, with shared content worth reading). A `CAMERA` frame
is stored with no text and no title, and is left out of the
[screen share summary](#screen-share-summary), the slide changes the
[chapters](#chapters) are placed by, the meeting's knowledge-base document, the
[assistant's view](#asking-the-assistant-about-the-shared-screen), the
**Screen share** strip and the exported deck. Its images are kept like any
other frame's. Frames read before `ContentKind` was stored, or not yet read,
have none and are treated as shared content. How well the model tells the kinds
apart depends on the model.

### Repeated frames

A shared application whose content keeps updating, or a page scrolled a little,
can give several frames of what a reader would call one screen: the pictures
differ, but the text on them is nearly the same. Once a frame's text is read it
is compared with the last frame before it that is neither a camera frame nor
itself a repeat, and stored with `DuplicateOf` naming that frame when the two
show the same screen:

- their texts are at least 80% alike (the words, in order, compared as a
  sequence), or
- their titles are the same, ignoring case and punctuation, and their texts are
  at least 50% alike. The title alone is not enough, because a model can name a
  whole deck ("Q3 review") rather than each slide.

A frame whose text holds every line of the earlier frame's and more is not a
repeat, however alike the two are: it is a slide built up a point at a time,
and the later frame is the fuller one. Two frames with no text are repeats
only when their titles match. Nothing is marked when the earlier frame's text
has not been read yet, or when the earlier frames cannot be read; such a frame
is shown, as every frame was before frames were compared.

Repeats are hidden on the **Screen share** strip unless **Show all N frames**
is selected, and are left out of the exported deck. They are still stored, with
their images, and are returned by `getVideoKeyframes` like any other frame; the
summary, the chapters, the assistant and the meeting-history document read them
as before. During a live meeting a frame is shown as soon as it is taken, before
its text has been read; the strip reads the newest frames again with each new
one, and a frame found to be a repeat is hidden then.

## Screen share summary

A meeting with key frames gets a **SCREEN SHARE SUMMARY** section in its
[summary](transcript-summarization.md): what was shared on screen, in the order
shown, whether slides, documents, web pages, applications, code or a demo. Each
entry has the time it went up, a few words naming what was shown, the thumbnail
of its most representative frame and a sentence or two on what it showed and
what was said or decided about it. The prompt asks the model to group
consecutive frames of one thing (a slide built up point by point, a document
being scrolled, an application being demonstrated) into one entry, to write no
more than 15 entries, and to leave out frames with nothing on them, such as an
empty desktop, a blank window or the meeting's own video tiles. It also asks the
model to treat everything in the frames as quoted meeting content rather than
instructions, never to identify people from how they look, and not to write out
web addresses, credentials or personal data read from the screen. How well it
does each depends on the model. A meeting without key frames gets no such
section, as a meeting without an agenda gets no agenda section.

The section was called **SLIDES PRESENTED** in 0.4.0's pre-releases. Summaries
written then keep that title and are shown in the same way. On a stack update,
a Custom profile holding SLIDES PRESENTED exactly as it was shipped, or turned
off, has it removed (turned off, its replacement is turned off in its place);
one whose administrator changed that prompt keeps it, and the seeder logs a
warning naming it on every deployment until it is removed. See
[Summary sections a release no longer ships](configuration-store.md#summary-sections-a-release-no-longer-ships).

How it is built:

- The fetch-transcript function gives each key frame **what was said** while it
  was on screen: the transcript segments that began between it and the next
  frame, with the speaker names the transcript shows.
- The section's prompt names the `{slides}` placeholder (`{screen_share}` is
  another name for it), which the summarizer fills with each frame's time, its
  picture reference (`![Slide at 12:30](keyframe:<FrameId>)`), its
  [slide text](#slide-text) and what was said, and the prompt asks the model to
  copy the picture reference exactly. The transcript itself is not sent with
  this section. At most 60 frames are listed (past that, the ones on screen for
  the shortest time are left out), with each frame's text cut at 1,500
  characters and its speech at 3,000 (the fetch-transcript function sends no
  more than that).
- The meeting page's **Summary** tab shows each picture reference as the
  frame's thumbnail, inside a section that starts closed, so the summary of a
  long presentation is not a column of slides until it is opened; the frames
  are read once the section is opened, and selecting one opens it on the
  **Screen share** tab. The embeddable summary panel shows each picture
  reference as the frame's thumbnail, read through `getVideoKeyframes` for that
  meeting with the same access check as the strip, in one request for every
  frame the summary names; selecting it opens the full picture. A picture whose
  signed link has lapsed (links last an hour) is read again by itself. A frame
  that cannot be read, because the meeting's retention has removed it, shows as
  its alt text. Copy, download, Word, email, the meetings
  list's summary preview and the MCP `get_meeting_summary` tool show the alt
  text too, and so does the summary document for the meeting-history knowledge
  base.
- A key frame of the meeting is the only picture a summary shows, in this
  section or any other. Any other image a summary names, by any address, is
  shown as its alt text and is not fetched.

The section is a prompt in the `summary_prompt_templates` section like any
other, so it can be edited, replaced or removed (set it to `NONE`) on the
**Transcript Summary** section of **Configuration ▸ Configuration**. A prompt of
your own can use `{slides}` or `{screen_share}` as well.

The measurements below were made with the SLIDES PRESENTED prompt, which asked
for one entry per slide and had no limit on the number of entries. They have not
been repeated with the SCREEN SHARE SUMMARY prompt, so its entry counts and
costs are not yet measured; the input is the same, and the output should be no
longer.

Measured on the synthetic meeting (14 key frames: ten
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
list beside the summary on the **Summary** tab, in place of the section itself.
Selecting a chapter opens the **Transcript** tab at its time and, after the
meeting, plays the recording from there. The chapters are also marks on the
seek bar of the page's [player](web-ui-guide.md#the-recording-player). In the **Screen share** strip, the frame that went up within
30 seconds of a chapter's start is labelled with the chapter, unless the frame
has a title of its own; a chapter that began without a change of slide labels
no frame, and a camera frame is never matched.

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
- Chapter times are on the transcript's clock. To play the video recording
  from a chapter, the page converts the time to the recording's clock using the
  key frame nearest before it, as each frame carries both. A Virtual
  Participant's video starts before transcription and falls further ahead at
  each reconnection of its transcription, which the frames follow. A Virtual
  Participant meeting whose video has no key frames plays the audio recording
  by default for this reason. The audio recording starts with transcription,
  so it needs no conversion. See
  [Lining the recording up with the transcript](web-ui-guide.md#lining-the-recording-up-with-the-transcript).
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

**Export slides (PDF)**, on the **Screen share** tab, opens the meeting's
key frames in a new tab as a deck, one landscape page per frame in time order,
each with its picture at full size (1568 pixels wide), its time, its label (as
on the strip), and its [slide text](#slide-text); camera frames and
[repeated frames](#repeated-frames) are left out, whether or not the strip is
showing all frames. It then opens the browser's print
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
| The meeting page's assistant panel | Shown as pictures in the answer; selecting one shows it full size over the chat, with **Show in transcript** under it, which opens the Transcript tab at that moment and moves the player there. The answer refers to a frame by its id and the page fetches a signed link for it through `getVideoKeyframes`, with the same access check as the strip. |
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

- **The previous second's**, and the first picture of the still spell, to tell
  whether the picture is still moving. A slide transition, scrolling, a video or
  a camera is not captured while it moves; a frame is kept only once the
  picture has held still for two seconds (three without a screen-share signal,
  below). Comparing with the start of the spell as well means a picture that
  creeps a little every second, as a live web page can, does not count as
  holding still.
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
12:30, and never earlier than the frame before it was taken. No two frames are
stamped less than a second apart, on the detector's clock or on the
transcript's, and a meeting keeps at most 1,000.

### How often frames are kept

A shared application whose content keeps updating, such as a live web page, a
dashboard or a scrolling transcript, changes by about as much each time as one
template slide differs from the next. On a measured 58-minute meeting with a
browser showing such a page, the frames differed from the one before by 3–9%,
and the four-second spacing alone let through 146 frames in 42 minutes, a
median of 10 seconds apart. Raising the change threshold to separate the two
would also drop slides (template slides differ by 4–7%, below), so the spacing
adapts instead:

- While the screen keeps changing, the second frame can follow the first after
  four seconds, the third waits 30 seconds after the second, and every later
  one 60 seconds after the one before.
- Each 10 seconds that the screen is quiet — holding still, and no different
  from the last key frame — steps the wait back down one: from 60 to 30 seconds,
  then to four. A deck whose slides each stay up for longer than about 12
  seconds (10 seconds quiet, plus the two seconds to settle) is therefore kept
  slide by slide; slides changed faster than that, on a screen that has been
  changing, can be skipped until the wait runs out.
- A picture that differs by at least 10% from the one that had settled before
  it is a different screen rather than an update of the same one (a new
  application, a slide with another layout), and waits only the four seconds.
  It is judged against the picture just before it, not against the last key
  frame, because the small updates of a busy screen add up against an old frame.

Frames that still show the same screen after this are collapsed once their text
is read; see [Repeated frames](#repeated-frames).

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
| Google Meet | None | Always, with the stricter thresholds |

| Thresholds | Share on screen | No share signal |
|---|---|---|
| Part of the picture that must differ from the last key frame | 3% | 4% |
| Part that may still be changing for the picture to count as still | 2% | 1% |
| Seconds the picture must hold still | 2 | 3 |
| Largest part of the picture that may be moving | 50% | 25% |
| Change from the picture settled before it that is a different screen (no wait beyond four seconds) | 10% | 10% |
| Wait before the second, third and later frames while the screen keeps changing | 4 s, 30 s, 60 s | the same |
| Quiet time that steps the wait back down one | 10 s | the same |

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

These measurements were made before the spacing adapted (above) and have not
been repeated since. The adaptive spacing and the stillness check against the
start of a still spell are covered by unit tests on synthetic samples only: a
page updating 5% of the grid every three seconds gives 44 frames in 42 minutes
(420 with the four-second spacing alone), and a deck of template slides changed
every 12 seconds keeps every slide. They have not yet been measured on a
recorded or live meeting.

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
the Virtual Participant's three seconds. The spacing adapts in the same way
(four seconds, then 30 and 60 while the picture keeps changing, stepping back
down one for every 10 seconds it is quiet).

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
`KEYFRAME_STRICT_CHANGE_FRACTION`, `KEYFRAME_MIN_INTERVAL_MS`,
`KEYFRAME_BACKOFF_STEPS_MS` (the longer waits, comma-separated milliseconds,
`30000,60000`), `KEYFRAME_QUIET_STEP_MS` (`10000`),
`KEYFRAME_MAJOR_CHANGE_FRACTION` and `KEYFRAME_STRICT_MAJOR_CHANGE_FRACTION`
(`0.1`), and `KEYFRAME_MAX_PER_MEETING`; see `keyframe-sampler.ts` — and, for
desktop video, of the transcriber (`KEYFRAME_DESKTOP_CHANGE_FRACTION`,
`KEYFRAME_DESKTOP_MAJOR_CHANGE_FRACTION`, the same `KEYFRAME_BACKOFF_STEPS_MS`
and `KEYFRAME_QUIET_STEP_MS`, and the others in `videokeyframes.ts`). They are not stack parameters: they are meant for local
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
| `ContentKind` | What the frame shows, read with its text: `SCREEN`, `CAMERA` or `MIXED` (see [Camera frames](#camera-frames)). Absent until read, and on frames read before it was stored. |
| `Title` | A few words naming the frame's shared content, when the model gave them. |
| `DuplicateOf` | The `FrameId` of an earlier frame showing the same screen, set with the text when the frame is a repeat (see [Repeated frames](#repeated-frames)); absent otherwise. |

### Retention, access and deletion

- **Retention.** Key frames are kept as long as the meeting's turn-by-turn
  transcript: the items carry the transcript's expiry, and a lifecycle rule on
  `lma-video-keyframes/` deletes the images after `TranscriptionExpirationInDays`
  days. A shorter transcript retention set on the Configuration page
  ([Retention](configuration-guide.md#retention-settings-the-stack-limits))
  applies to the items and not to the images, which the bucket rule keeps for
  the parameter's number of days.
- **Access.** Key frames are meeting data with the transcript's access rule:
  `getVideoKeyframes` and `onAddVideoKeyframe` return them to the meeting's
  owner, to the people it is shared with, and to administrators. Sharing or
  unsharing a meeting shares or unshares its key frames with it. The images
  are private objects in the recordings bucket, which the web UI's identity
  cannot read: `getVideoKeyframes` checks the caller's access to the meeting
  and then signs an hour-long link to each image, and only for an image stored
  in that meeting's own folder of this deployment's bucket.
- **Deletion.** Deleting a meeting deletes its key frames and their images,
  and its video recording: the file named on the meeting record, and any
  Virtual Participant recording segments left under `lma-video-chunks/`.

## Reading key frames

The AppSync API has a query and a subscription:

```graphql
query {
  getVideoKeyframes(callId: "<CallId>", limit: 100, nextToken: null, afterFrameId: null, frameId: null, frameIds: null) {
    VideoKeyframes { FrameId Timestamp VideoOffset ShareDetected DuplicateOf ThumbnailSignedUrl ImageSignedUrl }
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
`limit`, `nextToken` and `afterFrameId` are not used. `frameIds` returns the
frames it names, up to 100, in the order named, each once, leaving out any that
do not exist, also without `limit`, `nextToken` or `afterFrameId`; it is how the
summary's pictures, and a chat answer's, are resolved, in one request rather than
one per frame. Giving both `frameId` and `frameIds` is refused. The subscription
carries no signed links. As with `onAddTranscriptSegment`, its selection must
include `Owner` and `SharedWith`.

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
