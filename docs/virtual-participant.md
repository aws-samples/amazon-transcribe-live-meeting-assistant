---
title: "Virtual Participant"
---

# Virtual Participant

## Table of Contents

- [Overview](#overview)
- [When to use Virtual Participant](#when-to-use-virtual-participant)
- [Several people behind one attendee](#several-people-behind-one-attendee)
  - [Cost of voice separation](#cost-of-voice-separation)
- [How Speaker Names Are Assigned](#how-speaker-names-are-assigned)
  - [Naming rows from the voice as well](#naming-rows-from-the-voice-as-well)
  - [Live rows](#live-rows)
- [Supported Platforms](#supported-platforms)
  - [Google Meet](#google-meet)
  - [Zoom passcodes and join links](#zoom-passcodes-and-join-links)
- [Joining a Meeting](#joining-a-meeting)
  - [Reviewing the launch settings](#reviewing-the-launch-settings)
- [Status Lifecycle](#status-lifecycle)
  - [The status detail line](#the-status-detail-line)
  - [How the VP decides a meeting has ended](#how-the-vp-decides-a-meeting-has-ended)
- [Manual Action Required (CAPTCHA, 2FA, SSO)](#manual-action-required-captcha-2fa-sso)
- [AI-driven self-healing](#ai-driven-self-healing)
- [Zoom Sign-in](#zoom-sign-in)
- [Meeting Scheduling](#meeting-scheduling)
  - [Changing a scheduled Virtual Participant](#changing-a-scheduled-virtual-participant)
- [Meeting Invitation Parsing](#meeting-invitation-parsing)
- [VNC Preview](#vnc-preview)
  - [How a viewer is authorized](#how-a-viewer-is-authorized)
    - [Known limitations](#known-limitations)
  - [Opening the preview from the meeting assistant, and closing a failed session](#opening-the-preview-from-the-meeting-assistant-and-closing-a-failed-session)
- [Launch Types](#launch-types)
  - [MicroVM launch type (default)](#microvm-launch-type-default)
- [EC2 Instance Types](#ec2-instance-types)
- [Auto-Scaling](#auto-scaling)
- [Transcription Engine](#transcription-engine)
- [Chat Introduction Message](#chat-introduction-message)
- [In-meeting Chat Commands](#in-meeting-chat-commands)
- [Meeting Chat in the Transcript](#meeting-chat-in-the-transcript)
- [Meeting Consent](#meeting-consent)
- [Troubleshooting](#troubleshooting)
- [Developer Testing](#developer-testing)
- [See Also](#see-also)

## Overview

> **Not sure which capture option to use?** See [Meeting Sources](meeting-sources.md) for a side-by-side comparison of the Chrome Extension, Stream Audio, and Virtual Participant.

The Virtual Participant (VP) is a headless Chrome browser that joins meetings as a separate participant, driven by Playwright. It runs on ECS (EC2 or Fargate) or, where available, in an AWS Lambda MicroVM — see [Launch Types](#launch-types). It captures audio and metadata, sending them to the LMA Kinesis Data Stream for transcription and processing.

## When to use Virtual Participant

- **Attendees on native desktop or mobile apps**: Participants are free to use native meeting apps (Zoom, Teams, etc.) instead of being limited to the browser — the Chrome Extension and Stream Audio require joining from the web client.
- **Independent attendance**: The VP can join before you arrive, stay after you leave, or attend meetings you do not join at all — including scheduling it in advance.
- **Voice Assistant**: The VP is the only capture option that supports the Voice Assistant (wake phrase, push-to-talk, always-on modes).
- **Open VP live view**: Use the Meeting Assistant's "Open VP live view" feature to see the bot's browser view of the meeting in real time.

See [Meeting Sources](meeting-sources.md) for the full comparison.

## Several people behind one attendee

A Virtual Participant names speakers from the meeting roster, which is exact for
an ordinary remote attendee and useless for a conference room: everybody sharing
one speakerphone shares one roster entry, so a whole room arrives as a single
speaker.

**Virtual Participant voice separation**, in the **Transcription Engine** section of
**Configuration ▸ Configuration**, fixes that, and is on by default. With it on, the
VP tells apart the distinct voices behind one attendee and labels them
`Conference Room (spk_0)`, `Conference Room (spk_1)` and so on — the roster name
for who the platform says is talking, the voice id for which voice within it.

It also helps ordinary remote attendees, which is why it is on by default. Once a
voice has been heard long enough alongside one attendee, its rows are named after
that attendee even where the platform's active-speaker indicator is late or
flickers, which is where short replies used to get the previous speaker's name (see
[Naming rows from the voice as well](#naming-rows-from-the-voice-as-well)).

To turn it off, clear **Virtual Participant voice separation** in the
**Transcription Engine** section and save. Every VP that starts after the save then
names its rows from the meeting roster alone, with no voice ids. A meeting already
running keeps the setting it started with. A deployment upgraded from 0.3.x where an
administrator saved the Transcription Engine page keeps the value saved then, which
was off unless someone turned it on, and so does a Custom configuration profile that
holds the switch; turn it on there to get the new default. What it costs is
described under [Cost of voice separation](#cost-of-voice-separation).

Name them on the
[Speakers panel](transcription-and-translation.md#naming-the-voices), which
lists one row per voice and relabels every segment that voice produced.

The voice ids are shown only where they tell people apart. Each roster name is
shown as **one person** — `Bob Strahan` for every line, however many voice ids
separation gave him and however long each of them spoke — unless it reads as a
room (`Conference Room`, `ConfRm_1`, a room device) or its voices were given
different names, in which case it is shown as **several people**:
`Conference Room · Voice 1`, `Voice 2`. How many voices a name has, or how much
each said, does not make it several people, because separation splitting one
person's voice into two or three labels looks exactly like that. The
one-person / several-people icon beside the name's chips on the
**Transcript** tab (and beside the name in the Speakers panel) corrects either
guess with one click, and in **Several people**
each voice can be named in place; see
[One person or several people](transcription-and-translation.md#one-person-or-several-people).
So an attendee whose name does not read as a room, with several people behind it
(a shared laptop, a room named after a person), shows as one person until somebody
switches it.

The other two naming routes do **not** apply to a Virtual Participant today, and
it is worth knowing which:
[naming from what people say](transcription-and-translation.md#finding-the-names-automatically)
only considers voices whose base name is a placeholder such as
`Other Participant`, and a VP's base name is always a real roster name; and
[voiceprints](voice-enrollment.md) are seeded only into streaming sessions, not
into a VP's.

It works on either transcription engine. On Amazon Transcribe the turns are
recovered from Transcribe's own per-item speaker labels; on the
[on-demand engine](microvm-asr.md) they come from that engine's own separation.
Either way the switch is read once at the start of each meeting, so it takes
effect on the next meeting with no redeployment.

Three limits are worth knowing.

Labels arrive only when a segment finalises. Amazon Transcribe caps a result at
about thirty seconds, so the live transcript can show one roster name, and no
voice ids, for that long before the row splits into voices. The words themselves
are not lost; a watcher simply sees the separation late.

Accuracy depends on the audio, and on which engine. A room heard through a
speakerphone, compressed by the meeting platform and mixed with everyone else,
is the hardest input separation gets.

Measured on a deployed stack with four synthesised voices mixed onto one
channel, 33 seconds, identical audio to both engines:

| Engine | Distinct labels for 4 voices | Time-weighted purity |
|---|---|---|
| Amazon Transcribe | 2 | 57% |
| [On-demand engine](microvm-asr.md) | 3 | 79% |

Purity here is the share of labelled time whose label maps to a single real
person; 100% would mean every label belongs to exactly one speaker. **Prefer the
on-demand engine for rooms.** On Amazon Transcribe each label straddled more
than one person, so naming those rows attaches a real name to a substantial
share of somebody else's words — which is worse than the unseparated
`Conference Room`, because that at least claims nothing it cannot support.

Both engines merged the speaker whose turns were under about a second
("Agreed.", "Fine by me."): too little audio to characterise a voice. Expect
short interjections to be attributed to whoever spoke around them.

Synthesised voices may be more alike than real ones and the test had no room
reverb or background noise, so treat the figures as directional rather than a
guarantee for your room.

To see how a particular room separated, set the stack's **LogLevel** parameter
to `DEBUG`. The Virtual Participant then logs one `[DIARIZATION]` line per
Transcribe result, giving the number of segments it produced and the speaker it
assigned to each — which is what tells you whether the labels or the platform's
active-speaker signal is the thing going wrong.

Note what that writes: the speaker names on those lines are **participant
names**, so at `DEBUG` they reach CloudWatch Logs. No transcript text is
written by this line. Turn the level back down once you have what you need.

Where the two signals disagree on Amazon Transcribe, the finer boundary wins: a
voice label running across a change of platform speaker is split at the change,
and the platform's name is the one each part carries. The exception is a voice
that has been proven to one attendee, whose turn is kept whole under that
attendee (see
[Naming rows from the voice as well](#naming-rows-from-the-voice-as-well)). A turn split in two
can be put right with **Only this segment** on the meeting page, which is the
affordance for a single turn attributed to the wrong voice; a turn merged under
one name cannot be separated at all. (Renaming on the Speakers panel would not
help here — it is per voice, so it would rename every turn that voice produced,
not the one that was misattributed.)

The voice assistant's own speech is transcribed on a channel of its own on both
engines, so it never acquires a voice id.

### Cost of voice separation

What turning voice separation on costs depends on the engine the meeting runs on.

**On Amazon Transcribe** it costs nothing extra. The Virtual Participant asks
Transcribe for per-item speaker labels (`ShowSpeakerLabel`) on every session
whether the switch is on or off; the switch only decides whether the VP reads
them. The request, and so what Transcribe bills for it, is the same either way.

**On the [ASR MicroVM engine](microvm-asr.md)** it does not change what is billed
but does add work. A meeting gets one MicroVM whatever the switch says, sized by
the deployed bundle (8192 MiB for every bundle in the shipped catalog), and a
MicroVM is billed for its size and lifetime, not its load; see
[Cost and sizing](microvm-asr.md#cost-and-sizing). With the switch on, the engine
also runs its speaker diarizer (Sortformer, in the default bundle) on the
participants' session, which it skips when the switch is off. The voice
assistant's channel is never diarized. What has been measured, and what has not:

- On the 58-minute, two-person Zoom meeting described under
  [Naming rows from the voice as well](#naming-rows-from-the-voice-as-well), run
  with voice separation on, the engine's per-minute load line read about 32% of the
  MicroVM's 16 CPUs in every minute but one. The same meeting was not run with
  separation off, so how much of that load the diarizer adds is **not measured**.
- In that meeting one diarizer step took 34.1 s. The engine stopped returning text
  for about 35 s while its bounded audio backlog filled, and it dropped about
  1.66 s of audio, which was never transcribed. The load that minute fell to 18%,
  so the engine was blocked rather than short of CPU. Why the step took that long is
  not known, and how often it happens is not known: it is one occurrence in one
  meeting. Rows after a drop keep their place on the meeting's timeline, and the
  drop is logged (`sortformer step ... took` and `engine behind, dropped ...`); see
  [Cost and sizing](microvm-asr.md#cost-and-sizing).
- Whether diarization delays when a row reaches the transcript has not been
  measured.

**Amazon Bedrock.** Speaker naming (**Identify speakers**, which calls Amazon
Bedrock) works only on voices under a placeholder name such as `Unknown`, or under
a channel's own label. A meeting the Virtual Participant captured carries that in
its record (its capture source), and its roster names are never treated as a
channel label. So voice separation splitting an attendee into two voices, even
the only attendee who spoke, starts no automatic naming passes and adds no call to
the end-of-meeting pass; only voices under a placeholder name can cost a Bedrock
call. A meeting recorded before LMA stored the capture source has none, and there
a single roster name split into several voices is still treated as a channel label
and named (see
[Finding the names automatically](transcription-and-translation.md#finding-the-names-automatically)).

## How Speaker Names Are Assigned

A participant row is named after **the attendee the meeting platform showed as
speaking during that row's audio**, not after whoever is showing when the row
reaches the transcript, which is a second or more later. The Virtual Participant
notes each change of active speaker on the transcription audio clock at the moment
the platform reports it, so the change and the words are on the same clock.

- **On Amazon Transcribe** each word goes to the attendee shown for most of that
  word, and a result is cut into separate rows wherever that changes. A partial
  (in-progress) row carries the attendee shown for most of it until its final
  arrives and is cut.
- **On the [on-demand engine](microvm-asr.md)** the engine has already cut the rows,
  and each row goes to the attendee shown for most of it.
- The Virtual Participant's own name is never used for a participant row, and when
  the platform shows nobody speaking the last attendee shown carries on.

The platform reports a change slightly after it happens, so each change is moved
back by an allowance before rows are matched to it:

| Platform | Allowance | Why |
|---|---|---|
| Zoom, web client | 0 s | The page reports the change as it happens. On a three-person Zoom meeting it showed the Virtual Participant as speaking 0.08–0.36 s (median 0.16 s) after the Virtual Participant's own audio started, and other attendees' changes landed in the pause before their first transcribed word. |
| Zoom, Meeting SDK | 2 s | The SDK monitor polls every 3 seconds. |
| Teams, Chime, Webex, Google Meet | 0.75 s | The default for the other platforms. |

The `ASR_ROSTER_LAG_SECONDS` environment variable on the Virtual Participant task
overrides the allowance for every platform.

### Naming rows from the voice as well

With [voice separation](#several-people-behind-one-attendee) on, every row also
carries the engine's voice label, and the Virtual Participant uses it to correct
the platform's name rather than only appending it. The two signals fail in
different ways. The platform's active-speaker indicator names real people, but it
lags and flickers: on a two-person Zoom meeting the speaker view held each switch
for about 2.7 s and briefly showed a listener for a backchannel "mm-hmm", so a
short reply was often named after whoever had spoken before it. The voice label
names nobody, but it follows the voice.

So each voice label keeps a running, time-weighted count of the attendees the
platform showed while that voice was speaking:

- **A voice is proven** once it has at least 20 s of speech that the platform
  attributed to somebody, and one attendee holds at least 70% of it. From then on
  each of its rows is named after that attendee, whatever the platform shows at
  that moment. When the voice changes mid-conversation, the new row takes the new
  voice's attendee as soon as the engine finishes it, without waiting for the
  platform to catch up. On Amazon Transcribe, a proven voice's turn is also no
  longer cut where the platform's indicator flickers under it.
- **Until a voice is proven**, and for a row with no voice label, the row is named
  by the platform exactly as described above.
- **Hysteresis.** A proven voice keeps its attendee while that attendee holds at
  least 60% of the count, so a name does not flap as the count moves by a point.
  Below that it falls back to the platform's name.
- **A voice that turns out to be two people.** The platform's indicator is
  unreliable on short rows and reliable on long ones. A row of 3 s or more that
  the platform shows as somebody else for at least 90% of its length is named by
  the platform. Once such rows add up to 8 s (older ones count for less, with a
  half-life of 120 s of that voice's speech), the voice is demoted to platform
  naming for the rest of the engine session. That pattern is what the engine
  giving two people one voice label looks like. A demoted voice is not proven
  again in the same session, so the worst a false demotion can do is return that
  voice to platform naming.
- **A name from [voice enrollment](voice-enrollment.md)** stands on its own and is
  never outvoted, should the engine report one.
- Voice labels restart with each engine session (a MicroVM reconnect, or a new
  Amazon Transcribe session), so the count starts again. Rows already written are
  never renamed. When the on-demand engine re-sends a finished row to correct its
  text, the row keeps the name it was first given.

**How the voice id is shown.** A row carries its voice id only under the attendee
the voice is attributed to: the proven attendee, or before then, the attendee
holding most of its count. Any other row carries the bare name, so a voice id
never appears under two names. A room's voices, all of which are counted for the
room, still read `Conference Room (spk_0)`, `Conference Room (spk_1)`. A remote
attendee's row that the engine happens to give a voice it mostly hears from
somebody else reads just `Ana Ruiz`, not `Ana Ruiz (spk_3)` alongside
`Sam Lee (spk_3)`. Before this change, an on-demand engine row always carried the
platform's name plus its voice id, so one voice could appear under two names. The
Speakers panel then counted those rows as a second voice of the wrong person,
which could push a one-person name towards **Several people**.

So a stored speaker is either `Name (spk_N)` or the bare `Name`, and both are
intended. Every reader shows the two the same way while the name is shown as one
person: the transcript, its chips and the page header, the Speakers panel, the
downloads, the summary and agenda prompts, and the Meeting Assistant all read
`Bob Strahan` for both. The meeting page counts a name's bare-name rows as that
name's lines, not as a voice of their own: they are not listed as a second voice
in the Speakers panel, and the speaker dialog does not offer to combine them.

**What it does on a real meeting.** The thresholds were chosen by replaying a
58-minute, two-person Zoom meeting (web client, on-demand engine, 415 rows)
through this code. The platform's speaker changes and the rows' times were taken
from the meeting's logs and transcript. Who actually spoke was judged from the
content for every row where the signals disagreed, and assumed correct where they
agreed. The meeting ran before the engine kept its timeline across dropped
audio: midway through, it dropped 1.66 s of audio, and every later row sat that
much early. The replay moves those rows back by 1.66 s, which is where the engine
now places them. On the uncorrected times, the platform alone named 37 rows
wrongly and both signals together named 6.

| Naming | Wrong rows (of 415) | Speech wrongly named |
|---|---|---|
| Platform indicator alone (before this change) | 10 | 46 s |
| Voice label alone, mapped to attendees over the whole meeting | 6 | 5 s |
| Both, as described above | 6 | 5 s |

Both voices were proven after 95 s and 105 s of the meeting, and neither was
demoted. The same 6 rows remain wrong with every evidence requirement from 5 s to
30 s and proving share from 60% to 80%. Those 6 are the first row of the meeting
and five sub-second fragments that the engine itself labelled with the wrong
voice; no naming rule can fix them. Every row then carried its voice id under one
name only. Before, 33 s of one person's speech and 25 s of the other's sat under
the wrong voice id.

To check the guard against one voice carrying two people, the replay was repeated
with one attendee's later speech relabelled as the other's voice. Counted from
the point where that relabelling starts:

| Relabelled | Platform alone | Voice label alone | Both |
|---|---|---|---|
| The second person, from 25 min | 7 | 62 | 11 |
| The second person, from 42 min | 3 | 37 | 7 |
| The first person, from 25 min | 7 | 176 | 8 |

Treat these as directional. They come from one meeting, with two people, on one
platform. Teams, Chime, Webex and Google Meet report the active speaker
differently and have not been measured. Amazon Transcribe's labels are much less
pure on meeting audio (one label coincided with all three people of a three-person
Zoom standup), so on that engine fewer voices are proven and more rows keep the
platform's name. To see how a meeting was named, compare its rows' voice ids with
their names: a voice id that appears under one name throughout, and names that
change at the voice changes, mean the voices were proven.

### Live rows

A row is shown live, as partial results, while the person is still speaking, and its
final replaces it when the engine closes the row. On the
[on-demand engine](microvm-asr.md) with voice separation on, the Virtual Participant asks
the engine for a provisional voice label on each partial too. The label is read from the
most recent second of the row's own audio (see
[Partials name nobody](microvm-asr.md#partials-name-nobody)). Each partial is then named
the same way as a final:

- **When the partial's voice is proven,** the live row takes that voice's attendee at
  once, whatever the platform shows at that moment. A reply that begins while Zoom still
  shows the previous speaker is named after the person replying from its first partials,
  rather than only when its final arrives.
- **Before the voice is proven,** or when a partial has no label yet (the first fraction
  of a second of a row, an engine that does not send one, or Amazon Transcribe, which
  never labels partials), the live row is named by the platform as before.
- **No flicker.** A live row keeps the name it shows until a different name is
  confirmed. A proven voice's name replaces a platform name at once. Any other change
  needs two partials in a row (about a second) to agree on it, so a single reading, such
  as a backchannel "mm-hmm" or a diarizer blip, never renames the row, and a row cannot
  go back and forth between two names on single readings.
- **The final decides.** The final is named exactly as described in
  [Naming rows from the voice as well](#naming-rows-from-the-voice-as-well), and it is
  the row that is stored. Partials add nothing to a voice's count. When the final's name
  differs from the name its live row last showed, the Virtual Participant logs
  `[ASR] row … renamed at its final: <live name> (<source>) -> <final name> (<source>)`,
  so how often it happens can be counted from the log.

**What it does on a real meeting.** This was replayed on a 33-minute, three-person Zoom
meeting (web client, on-demand engine, voice separation on), whose log records the time
and text of every partial and every change of active speaker. The log does not record
the name each partial was sent with. On the shipped code that name is the platform's
majority over the row so far, which the replay recomputes. The replay compares each
partial's name with the name of the final row that covers the words being spoken at that
moment, which is the name the reader sees once the row is corrected. The voice labels
the engine would have sent on the partials are not in the log either. They are estimated
from the finals' labels, which come from the same diarizer frames, read 1 s behind the
partial, as the engine's refresh allows, and named after each voice's proven attendee.
Every voice in this meeting was proven early, so the estimate leaves out the platform
fallback that would apply before a voice is proven.

| Live-row naming | Time a live row showed a name other than the corrected one | Name changes on live rows | Changes back to a name already left |
|---|---|---|---|
| Platform indicator (before this change) | 146.8 s of 1,412.9 s | 31 | 3 |
| Voice when proven, no hold | 58.6 s | 50 | 18 |
| Voice when proven, with the hold described above | 66.3 s | 28 | 4 |

Of the 157 live rows, 116 were spoken by one voice. On the platform indicator, 5 of those
were still showing the wrong name when their final arrived, against none with the voice.
The other 41 were closed by the engine as one utterance spoken by two or more voices, and
split at the final. Those rows are corrected at their final whatever the live name was,
because the final gives the row's own place in the transcript to the first voice and puts
each later voice's words on a new row. A live row follows the voice currently speaking,
so on those rows the change at the final goes the other way. That case is what was
reported in this meeting: an attendee's reply was shown under the previous speaker's name
until the end of the utterance. With the voice, his name would have appeared 2.1 s into
his 4.7 s reply; the platform's majority reached him only after he had finished. The row
would still have been split when it closed. Closing a row as
soon as the voice changes, rather than at the end of the utterance, is what would remove
that correction, and the on-demand engine does not do this with the Sortformer diarizer.

With the engine's labels 0.4 s behind instead of 1 s, the wrong-name time is 58.3 s; at
2 s behind it is 111.9 s, with 46 name changes, which is why the engine refreshes its
labels for partials about once a second. All of this comes from one meeting on one
platform, with an estimate of the partials' labels rather than the labels themselves. It
has not run in a live meeting, and what it costs the engine has not been measured (see
[Partials name nobody](microvm-asr.md#partials-name-nobody)).

## Supported Platforms

- Zoom
- Microsoft Teams
- Amazon Chime
- WebEx
- Google Meet (see [Google Meet](#google-meet) for what the meeting's organiser has to allow)

### Google Meet

The VP joins a Google Meet meeting as a guest from its browser, without a Google account, under its display name (`LMA - <your user name>` by default, set on the Configuration page; see [More than one LMA in a meeting](#more-than-one-lma-in-a-meeting)). On the create form choose **Google Meet** and enter the meeting code (`abc-defg-hij`) or paste the meeting link (`https://meet.google.com/abc-defg-hij`); Meet meetings have no password, so the form does not ask for one, and a link is stored as its code. A `meet.google.com/lookup/<name>` link is not accepted: it uses a meeting nickname, which Google lets only signed-in members of the organiser's Google Workspace use, and the VP joins as a guest, so enter the meeting's code instead. Pasting a Meet calendar invitation into **Paste Invitation** fills the form in the same way. The `lma vp create --platform GOOGLE_MEET` command, the SDK, and the meeting assistant's `start_meeting_now` and `schedule_meeting` tools accept Meet too.

On the pre-join screen the VP types its name under *What's your name?*, turns the microphone off (unless a voice assistant needs it) and the camera off (unless the Simli avatar is its camera), and presses **Ask to join**, or **Join now** when Meet lets it straight in. It then waits, for up to five minutes, until it is in the meeting, posts its introduction in the meeting chat (*In-call messages*) and starts transcribing.

**What the meeting's organiser has to allow.**

- **Guests from a browser.** Meet has to accept a guest who is not signed in to a Google account. Google offers this for meetings organised from a Google Workspace account whose administrator allows participants from outside the organisation and without a Google account; a meeting organised from a personal Google account may not accept such guests at all. When Meet does not accept the VP, it shows *"You can't join this video call"*, either before any name field or a moment after the VP clicks **Join now** / **Ask to join**. The VP then reloads the page (and, after a refusal at the join click, enters its name and clicks join again), up to four times in all for the join, after pauses of 4, 8, 15 and 20 seconds (about 50 seconds in all, plus the page loads), and clears the browser's Google cookies before the second and later reloads. If Meet still refuses it, the VP stops with **FAILED** and the detail *"Google Meet did not let the participant join"* (`endTrigger` `pre-join:meet-refused`).
- **Being let in.** Somebody in the meeting has to admit the VP when it asks to join, unless the meeting lets guests in without asking. With a Google Workspace organiser, setting the meeting's **Meeting access** to **Open** is meant to let anyone with the link join without being admitted. In live attempts a guest with no Google account joined an Open meeting without being admitted. A request that is declined ends the join with `pre-join:meet-denied`, and one that nobody answers within five minutes with `pre-join:not-admitted`.
- **Chat.** The chat commands (`LMA leave`, `LMA pause` and the rest) and the introduction need the meeting chat. If the organiser has turned chat off for participants, the VP posts nothing and cannot be dismissed from the chat; remove it from the meeting from Meet's People panel instead, or end it from LMA.

What has been observed in live attempts, from a browser on an Amazon EC2 host and not signed in, to a meeting whose organiser had set Meeting access to Open: every join that reused the browser profile saved from an earlier Meet join was refused with *"You can't join this video call"*, and the first join from a fresh profile got in. A page load that Meet refused has also been followed by a reload that showed the name field, which is why the VP reloads before giving up. So a Google Meet join always starts from a fresh browser profile, and the VP never saves its profile after a Meet meeting, unlike the per-user profile kept for the other platforms (see [Zoom Sign-in](#zoom-sign-in)). Meet's reasons for refusing a guest are not documented, so these are observations rather than an explanation; treat Google Meet as working only where a test meeting shows it does.

**What is not available, or not yet confirmed, on Google Meet.**

- **Confirmed in a live meeting:** joining, the introduction and the chat (including `LMA ask` and the assistant and translator switches), the voice assistant, and leaving when the host ends the meeting for everyone. The VP opens Meet in English (`hl=en`) whatever the browser's language, and falls back to the [AI resolver](#ai-driven-self-healing) for the join controls and the chat button.
- **Speaker names are not confirmed yet.** The VP reads the audio-level indicator on each participant tile to tell who is speaking. That reading is built from one live meeting and has not yet been confirmed while people speak, so transcript lines may still carry the voice-separation label (`spk_0`, `spk_1`) rather than participants' names; name the voices in the Speakers panel. When no tile reads as speaking and exactly one other named participant is in the meeting, the VP names that participant as the speaker, so a two-person meeting is attributed either way. The VP logs each change in a tile's speaking state (`[meet] tile … speaking=…`, the first 50 per meeting), the structure of the first tiles and, at debug level, which tile elements change while people speak (`[meet] tile … activity`) over the first 45 seconds of the meeting, or until it ends if that is sooner.
- **Meeting consent:** Notify, both Opt-in rules and objections are offered. A consent typed in the chat is recorded under the sender the VP reads from the chat, as on Zoom and Chime, and one said aloud under the participant the VP shows speaking. Neither reading has been confirmed in a live Meet meeting yet (see the speaker names above); see [Meeting Consent](meeting-consent.md#what-each-platform-supports).
- **Screen-share key frames:** key frames are judged only while a participant tile shows a presentation ("(Presentation)" or "is presenting"), so the camera grid is not kept when nobody presents. See [Screen-share signal per platform](meeting-video.md#screen-share-signal-per-platform).
- The Simli avatar has not been tested on Meet. Joining with a Google account signed in to the VP's browser profile is not supported; the VP joins as a guest.

### Zoom passcodes and join links

A Zoom meeting can be given to the VP in two ways: as the numeric meeting ID with the meeting passcode, or as the meeting's full join link.

- **Meeting ID and passcode.** The passcode is the one the invitation text shows, for example `Passcode: 1234 5678`. Zoom limits a passcode to 10 characters ("By default, a passcode can only have a maximum length of 10 characters", in the `password` field of Zoom's [Create a meeting](https://developers.zoom.us/docs/api/meetings/) API reference). The VP removes the grouping spaces from a numeric passcode and types it into the passcode box on Zoom's join screen.
- **Join link.** For Zoom, the meeting ID may be the full join link, such as `https://us05web.zoom.us/j/81234567890?pwd=...`, in the create form, `lma vp create`, the SDK, or the meeting assistant's `start_meeting_now` and `schedule_meeting` tools. The VP reads the meeting number and the link's `pwd` value from links of the forms `/j/<number>`, `/wc/<number>/join` and `/wc/join/<number>` on `zoom.us` or any of its subdomains. If a passcode is also given, the VP uses the passcode.
- **Personal meeting link.** A personal meeting link, `https://<name>.zoom.us/my/<room>` with or without `?pwd=...`, does not carry the meeting number, so before it joins the VP asks Zoom for the link once, without following the redirect, and takes the meeting number from the `/j/<number>` link Zoom redirects to. The `pwd` value comes from the personal link, or else from the redirect, and is used as a join link's is; a passcode given separately is used instead. Only the host and the meeting number are logged. If the request fails or Zoom does not redirect to a meeting, the join ends with `pre-join:zoom-link-unresolved` and the message *"Zoom did not resolve this personal meeting link; create the Virtual Participant with the meeting ID and passcode instead."* Zoom redirects a room name it does not know to a meeting number too, so a mistyped name is found out only on the join screen, where Zoom says the meeting does not exist and the VP stops with *"Invalid meeting ID"* (`error:meeting-not-found`). The create form lists the Zoom link forms that work under the field when a link is entered.

The `pwd` value of a join link is not the passcode: it is an encoded form of it, usually 32 characters ending in `.1`, and the passcode box refuses it. A meeting password that is longer than 10 characters, or ends in a period followed by digits, is therefore treated as a link's `pwd` value. The VP's web-client join opens the meeting at `/wc/<number>/join?pwd=<value>`, as the link does, and does not type the value. If Zoom still asks for a passcode, for example because the value was cut off, the VP sets `MANUAL_ACTION_REQUIRED` with the message *"The meeting password looks like the pwd value from a Zoom link and Zoom did not accept it; enter the meeting passcode from the invitation and select Join."* Enter the passcode and select **Join** in the live view; if nobody does within five minutes, the join ends with `pre-join:passcode-not-entered`.

The meeting assistant's `start_meeting_now` and `schedule_meeting` tools check a Zoom meeting before creating the VP. A password longer than 10 characters is refused unless it has the shape of a whole `pwd` value (at least 30 letters and digits, then a period and digits, the shape seen on Zoom links) or the meeting ID is a join link with its own `pwd` value, and a meeting ID that is a link must be a Zoom join link of one of the forms above.

The Zoom Meeting SDK join (used when Zoom Meeting SDK credentials are configured) passes the password to the SDK unchanged. Whether the SDK accepts a link's `pwd` value in place of the passcode has not been tested, so give the SDK path the passcode from the invitation.

## Joining a Meeting

1. Navigate to **Virtual Participant** in the LMA UI.
2. Enter meeting details: URL, platform, meeting ID/password, and meeting name.
3. Click **Join Now**.
4. The VP starts in approximately 20-25 seconds (MicroVM, the default), 30-60 seconds (EC2), or 1-2 minutes (Fargate).
5. Once joined, the VP posts an introduction message in the meeting chat.
6. View VP status in the UI as it progresses through its lifecycle (see [Status Lifecycle](#status-lifecycle)).

### Reviewing the launch settings

The Virtual Participant page has a **Launch settings** section (collapsed by default) that lists what the Create Virtual Participant form asked for, in three groups:

- **Meeting**: meeting name, platform, meeting ID, URL or Google Meet code, whether a meeting password was given (the password itself is never shown), the Zoom account choice for Zoom meetings, the meeting time (**Start now**, or the scheduled time in the time zone it was entered in and in yours when they differ), and the agenda.
- **Capture**: whether the meeting video (screen) is recorded. Audio recording and the transcript are not a per-meeting choice.
- **Consent**: the meeting consent choice for this meeting, or **Not set (deployment default)** when the deployment's [Meeting Consent](#meeting-consent) setting applies.

A setting the form did not send, such as on a Virtual Participant started from an MCP client or created before the setting was recorded, reads **Not set (deployment default)** or **Not recorded**.

## Status Lifecycle

The VP reports a granular status as it boots, joins, and runs. The UI uses these to give a precise picture of where in the startup sequence the VP is:

| Status | What's happening |
| --- | --- |
| `INITIALIZING` | Step Functions has submitted the ECS RunTask request (or `RunMicrovm` under `MICROVM`); the VP is being scheduled |
| `WAITING_FOR_CAPACITY` | The task is queued waiting for compute placement (typically 60-90 seconds). On **EC2**, the capacity-provider auto-scaler may be launching a new host (see [Auto-Scaling](#auto-scaling)); on **Fargate**, this is the brief serverless provisioning / networking step. The UI tailors this message to your deployment's `VPLaunchType`. |
| `BOOTING` | Container started; pulling Chrome image, starting Xvfb / VNC / PulseAudio |
| `REGISTERING_NETWORK` | Registering the task with the live-view ALB (typically 30-60 seconds). Skipped under `MICROVM`, which has no ALB — each MicroVM exposes its own HTTPS endpoint. |
| `HYDRATING_PROFILE` | Restoring the per-user Chromium profile (cookies, "trusted device" markers) from S3 |
| `LAUNCHING_BROWSER` | Launching Chromium via Playwright (`chromium.launchPersistentContext`) |
| `VNC_READY` | Browser is up; live-view viewer can connect |
| `CONNECTING` | Initializing audio/video pipelines (Nova Sonic, Simli avatar, agent mic) |
| `JOINING` | Navigating to the meeting URL; signing in to Zoom if credentials are stored |
| `MANUAL_ACTION_REQUIRED` | A CAPTCHA, 2FA, SSO, consent dialog, or other Zoom verification step needs human input — see [Manual Action Required](#manual-action-required-captcha-2fa-sso) |
| `ACTIVE` / `JOINED` | VP is in the meeting and capturing audio |
| `COMPLETED` / `ENDED` | Meeting ended cleanly (host removed the VP, all attendees left, or the VP got "lonely") |
| `FAILED` | The VP could not join. The DDB record carries an `errorMessage` describing what went wrong (e.g. *"Meeting join failed: …"*, *"Zoom login failed: invalid credentials"*, *"ECS RunTask soft-failure: agent not connected"*) |

When ECS reports a "soft failure" on RunTask (returns HTTP 200 with a non-empty `failures` array — typically the container instance agent is briefly disconnected), the state machine catches it explicitly and writes `FAILED` plus the original failure reason to the VP record, instead of leaving the VP stuck in `INITIALIZING` indefinitely. That write is conditional on the row not having reached a terminal status, so a task that did start and is reporting its own outcome keeps it.

### The status detail line

Alongside `status`, the VP record carries two fields describing the current status in more detail:

| Field | Contents |
| --- | --- |
| `errorMessage` | The prose the UI shows under the status. It is the detail line for every status, not only failures: the reason a meeting ended on `COMPLETED` (*"Asked to leave by Jeremy."*), the current sub-step while the VP is still `JOINING` (*"Waiting to be admitted…"*), and the error on `FAILED`. |
| `endTrigger` | The machine-readable counterpart: which branch the VP exited through, from the vocabulary below. Written on `COMPLETED` and `FAILED`, so join outcomes across many meetings can be counted with a query over the VP table instead of a search through container logs. |

Reaching `JOINED`, `ACTIVE` or `COMPLETED` without a new detail to show clears `errorMessage`, so a message from earlier in the same run — a join sub-step, or an attempt that did not work out before a later one did — is not still displayed next to a VP that is in the meeting.

Most values are one of these fixed strings:

| Group | Values |
| --- | --- |
| Never got in | `pre-join:not-admitted`, `pre-join:no-join-button`, `pre-join:no-name-input`, `pre-join:goto-failed`, `pre-join:ai-error-screen`, `pre-join:meet-refused`, `pre-join:meet-denied`, `pre-join:meet-invalid-code`, `pre-join:meet-ended`, `pre-join:passcode-not-entered`, `pre-join:zoom-link-unresolved`, `acs-not-admitted`, `sdk-not-admitted`, `enterprise-login-timeout` |
| Asked to leave in chat | `chat` |
| Left after a participant objected to recording ([Meeting Consent](meeting-consent.md#objections)) | `consent-objection` |
| The meeting ended | `post-meeting-screen`, `meeting-ended-text`, `meet-ended-screen`, `HANGUP_BUTTON_HIDDEN`, `REJOIN_BUTTON_APPEARED`, `ZOOM_END_DIALOG`, `SDK_HOST_ENDED`, `SDK_REMOVED`, `SDK_DISCONNECTED` |
| Everyone else left, or the VP was removed | `meet-removed-screen`, `attendees-left`, `acs-participants`, `sdk-participants`, `participants-watchdog`, `participants-ui:counter-gone`, `participants-ui:page-dead`, `attendee-badge-missing` |
| Ran out of time, or lost the page | `meetingTimeout`, `renderer-crash`, `navigated-away`, `meet-left-screen`, `URL_CHANGE_BLANK`, `ZOOM_END_DIALOG_ABORTED` |
| The join threw, and the message matched one of the container's six classes | `error:renderer-crashed`, `error:password`, `error:meeting-not-found`, `error:meeting-ended`, `error:permission-denied`, `error:unclassified` |

The rest are a fixed prefix plus a detail that came from the platform, so the prefix is what groups and what follows the colon varies:

| Form | Written when | Detail after the colon |
| --- | --- | --- |
| `ACS_REMOVED:…`, `ACS_DISCONNECTED:…` | The Teams SDK reported the call disconnected, or the VP removed from it | The SDK's own `code` and `subCode`, or `no-endreason` |
| `webex-text:…` | A Webex meeting-end phrase appeared on the page | Which of five phrases matched |
| `error:…` | The Webex watcher threw while waiting for the meeting to end — so, after the VP was admitted | The start of the underlying browser message |
| `poll-error:…` | The Chime meeting-end poll threw | The start of the underlying browser message |

Note that `error:` appears in both tables and means different things in each. The six values in the first are written by the container's own classifier when a *join* throws, and are fixed. An `error:` value that is not one of those six came from the Webex watcher *after* admission, and its detail is whatever the browser said — so **the value space is open**, and this page cannot list it. Group those by prefix and read `errorMessage` for the detail, which is where prose belongs.

Whatever follows the first colon is reduced to a lower-case, hyphenated slug before the value is stored, and the whole value is capped at 100 characters. That is what keeps the second table countable: two reports of the same failure become the same string whatever the wording or punctuation around them. A trigger with no colon is stored exactly as it is.

`COMPLETED` means the VP was in the meeting. A VP that never got in — not admitted, or stuck on the pre-join screen — ends as `FAILED` with *"Could not join the meeting (was not admitted, or stuck on the join screen)."*, on every platform.

### How the VP decides a meeting has ended

The VP leaves on its own so an abandoned meeting is not recorded (or billed) indefinitely. It uses several signals, in order of confidence:

1. **An in-meeting chat command** — `LMA leave` and its synonyms; see [In-meeting Chat Commands](#in-meeting-chat-commands).
2. **A positive end-of-meeting signal from the platform** — on Teams, the post-meeting screen ("You left the meeting", "The meeting has ended") or a post-meeting URL, checked before anything else on every poll, so a meeting Teams announces as over is detected on the first poll. The hang-up button disappearing is a second such signal, watched continuously rather than on the poll cycle.
3. **A sustained empty roster** — the participant count is present and reads one or fewer for several consecutive polls.
4. **A sustained absence of the whole meeting UI** — neither a readable participant count nor any in-meeting controls. This is the backstop for the case where none of the above fires.

On Google Meet the same four signals are read from Meet's page. The end-of-meeting signal is Meet's own screen once its **Leave call** button has gone (*"You've been removed from the meeting"*, *"The call has ended"*, *"You left the meeting"*), or the page leaving meet.google.com; the screen's wording is only read once the button has gone, so chat text that happens to contain it is not mistaken for the end of the meeting. The participant count is the number on the **People** button, and an unreadable count with the Leave call button still on screen does not count towards leaving, as with Teams below. Meet uses the default bounds below; the three Teams settings do not apply to it.

The distinction between the third and fourth signals matters, and getting it wrong is what caused [#660](https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/issues/660). Teams removes the roster button, and with it the participant-count badge, whenever the meeting toolbar collapses — most commonly during a content share or in full-screen layouts. An unreadable badge therefore says nothing on its own: it is equally the signature of a meeting that ended and of a meeting where someone is presenting. So before an unreadable count counts towards leaving at all, the VP checks whether the rest of the in-meeting interface (the stage, the calling screen, the call controls) is still present, and while it is, the VP stays. That matters because nothing in the VP moves the pointer, so a toolbar that auto-hides during a share can stay hidden for the whole presentation.

That check is itself bounded, deliberately, because Teams is not reliable in the other direction either: it sometimes leaves in-meeting controls on screen after a meeting has ended, which is how a Virtual Participant once kept recording long after everyone had gone. So the in-meeting interface can hold the VP in a meeting with an unreadable roster for about 30 consecutive minutes, after which the unreadable count starts counting again and the VP leaves about five minutes later. A readable count at any point restores the full allowance, so a meeting containing several long shares is fine; it is one unbroken half-hour with no readable roster at all that is treated as suspicious. The worst case in either direction is therefore bounded: a share is safe for half an hour at a stretch, and a meeting whose end Teams announces to nobody is left after about 35 minutes at the default cadence — and never more than an hour, whatever the settings below.

Three settings control the timing, in the **Virtual Participant** section of the [Configuration page](configuration-guide.md), where they can be changed while the stack runs, with no stack update. They apply to the **Teams browser join path** only — Zoom has its own equivalent, and the Teams ACS SDK join path (used when `AcsConnectionString` is configured) gets deterministic roster events instead and needs none of this. Both the ECS and MicroVM launch types honour them. The defaults suit Teams as it behaves today; change them only if a deployment is consistently leaving meetings early, or lingering after they end:

| Setting | Default | Meaning |
| --- | --- | --- |
| **Teams: milliseconds between meeting checks** | `20000` | How often the VP samples the meeting UI, in milliseconds. Range 1000–120000. |
| **Teams: checks alone before leaving** | `3` | Consecutive polls reading one or fewer attendees before the VP leaves — about 60 seconds at the default cadence. Range 1–90. |
| **Teams: checks without a meeting before leaving** | `15` | Consecutive polls with neither a readable attendee count nor any in-meeting controls before the VP leaves — about 5 minutes at the default cadence. Range 1–90. |

A value saved on the Configuration page takes effect on the next Virtual Participant to start. Editing the VP task definition by hand does not work: both launch types reference a specific task-definition revision, so a revision registered outside CloudFormation is never used. To change one, an Admin makes and activates a Custom configuration profile (see [Configuration profiles](configuration-guide.md#configuration-profiles)) and saves the setting there.

Inside the container the three values are the `VP_ATTENDEE_POLL_MS`, `VP_POLLS_BEFORE_END` and `VP_POLLS_BEFORE_END_MISSING` environment variables, and a value saved on the page replaces the variable when the Virtual Participant starts; the variables are also how they can be set when [running the container locally](virtual-participant-local-dev.md). The container also bounds them on its own, in both directions: a value that is not a whole number in range is ignored in favour of the built-in default, and each tolerance is held inside a wall-clock window however the cadence and the count combine: 4 to 30 minutes for an unreadable meeting, and 30 seconds to 10 minutes for an empty one. So lowering the cadence for more frequent logging cannot accidentally narrow a tolerance to seconds, and raising a count cannot leave a VP running without a backstop. A count outside its window is adjusted rather than honoured, with a line in the log saying so — at the default cadence, for instance, **Teams: checks without a meeting before leaving** below 12 is raised, despite the page accepting 1.

Every decision the watchdog makes is logged. The resolved values appear once per meeting next to `Listening for attendee changes`; each poll logs its running counter against the bound that applies to it; and a poll held back by the in-meeting interface reports which element it matched, along with how much of the 30-minute allowance is left. If a Virtual Participant ever leaves a meeting that was still running, or lingers after one ended, those lines are what to look at first.

## Manual Action Required (CAPTCHA, 2FA, SSO)

When the VP can't proceed without a human (Zoom 2FA passcode, CAPTCHA, SSO redirect, an unknown consent dialog the auto-dismiss handler can't classify), it sets status to `MANUAL_ACTION_REQUIRED` and surfaces:

- A persistent **Flashbar alert** at the top of the LMA UI with the action type and a link to the meeting detail page.
- An **action banner** on the Virtual Participant detail page above the live VNC viewer.
- A **browser notification + audio chime** if you've granted notification permission, so you can be tabbed-away or in another window.

Open the live VNC viewer for the affected VP, complete the challenge (type the passcode, solve the CAPTCHA, sign in via SSO, click the consent button — whatever the banner says), and the VP picks up automatically. The default timeout is 3 minutes; if no human response arrives, the VP fails the meeting cleanly.

The banner is dismissible — dismissed alerts are remembered in `localStorage` (per VP id) so the banner doesn't reappear on every page refresh after you've dealt with it.

## AI-driven self-healing

Every platform handler (Zoom, Teams, Webex, Chime, Google Meet) wraps its hard-coded CSS selectors in an AI fallback resolver. When a primary selector misses (because the meeting platform shipped a UI change), the resolver asks Claude (Bedrock, vision-capable) to find the right element by reading a compact DOM summary plus a screenshot of the current page. Successful resolutions are cached in a shared `DomSelectorCache` DynamoDB table (30-day TTL on `lastUsedAt`) so the cost is paid only once per platform UI change.

The same resolver classifies unknown popup dialogs:
- `CONSENT` / `RECORDING_NOTICE` → auto-clicked by the VP. It never clicks a button that would join without meeting audio (for example Teams' "Continue without audio or video"). It escalates a camera/microphone dialog to `MANUAL_ACTION_REQUIRED` when it has no safe button to click (no button, a button that would join without audio or has no label, or a failed click). Any other notice without a usable button is left alone unless the model says a human is needed, as before.
  - On a camera/microphone dialog, the manual-action message tells you not to choose "Continue without audio or video": the VP would stay in the meeting but capture no audio, so there would be no transcript and no recording. The VP grants camera and microphone to the meeting sites at launch, so this dialog normally does not appear.
- `CAPTCHA` / `LOGIN_REQUIRED` / `SSO_REDIRECT` / `BLOCKED` → escalated to `MANUAL_ACTION_REQUIRED` (see above) so a human can solve it via the live VNC viewer.

Disable the AI fallback by setting `BedrockDomResolverModelId` to an empty string at deploy time — the VP reverts to hardcoded-selector-only behavior.

For full details on the AI resolver and its interaction with the Zoom sign-in flow, see [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md).

## Zoom Sign-in

LMA can sign the VP in to Zoom using **per-user stored credentials** before navigating to the meeting URL. A signed-in session joins far more reliably ("We detected you may be a bot…" guest blocks become rare) and allows the VP to join meetings that disallow guests.

- Each user adds their own credentials via the **Zoom account** card in the Create Virtual Participant modal. Credentials live in AWS Secrets Manager keyed by Cognito sub, and the plaintext password is never returned to the React UI.
- The VP container reads the secret at runtime; it is never put on the task definition or in the Step Functions execution input.
- The sign-in flow is **AI-driven** end-to-end. After submitting the username, Claude inspects each subsequent page (password entry, OTP, passkey-binding upsell, phone-binding upsell, dashboard, etc.) and decides whether to fill, skip, click-through, wait, or escalate — so it tolerates Zoom's frequent post-login interstitials without code changes.
- **2FA / CAPTCHA / SSO challenges** that need human input fall through to `MANUAL_ACTION_REQUIRED` (see above).
- **Per-user persistent Chromium profile** in S3 means cookies and "trusted device" markers survive across meetings — after the first manual sign-in, subsequent VPs reuse the session and sign in cleanly without re-prompting.

For setup, caveats, and the full join-reliability stack, see [Zoom Sign-in & Join Reliability](zoom-credentials-and-join-reliability.md).

## Meeting Scheduling

Enter a future meeting time to schedule the VP to join a meeting later. The scheduling interface supports:

- Setting a specific date and time for the VP to join
- Starting a scheduled meeting immediately
- Stopping an in-progress scheduled meeting
- Deleting a scheduled meeting before it starts
- Changing a scheduled meeting before it starts

### Changing a scheduled Virtual Participant

While a Virtual Participant is waiting for its scheduled launch, its owner and Admins see an **Edit** button on the **Launch settings** section of its page. It opens the Create Virtual Participant form's fields, filled in with the stored values: the meeting name, platform, meeting ID or link, meeting time, agenda, meeting consent, video recording and, for Zoom, the stored Zoom account choice. The meeting password field starts empty, and leaving it empty keeps the current password. The stored Zoom account used is always that of the user who scheduled the meeting, whoever makes the change.

Changes are accepted until 5 minutes before the meeting time, and a new meeting time must also be at least 5 minutes away. The check is made again when the change is saved: if the Virtual Participant has started launching in the meantime, the change is refused and it launches as it was scheduled.

A saved change moves the launch schedule: the scheduler rewrites the one EventBridge Scheduler schedule created for the Virtual Participant, so it launches once, at the new time and with the new settings. If the schedule cannot be moved (for example because it no longer exists), no new schedule is created, and the Virtual Participant's status line says that it will launch as it was scheduled before; cancel it and schedule a new one to use the new settings. The agenda and meeting consent choice are read by the Virtual Participant when it starts, so changes to them apply even without moving the schedule.

## Meeting Invitation Parsing

Paste a full meeting invitation into the input field and Bedrock AI automatically parses it to extract and auto-fill:

- Meeting platform
- Meeting URL
- Meeting ID
- Meeting password

This eliminates the need to manually copy individual fields from calendar invitations.

## VNC Preview

The VNC preview provides real-time browser viewing and remote control of the VP's Chrome window at 1920x1120 resolution. This feature is available on the meeting detail page and allows you to:

- See exactly what the VP sees in the meeting
- Interact with the VP's browser session remotely
- Troubleshoot joining issues in real time

### How a viewer is authorized

Opening the viewer takes two steps, both of which happen automatically in the UI, and rests
on a third control at the load balancer.

**1. A token for the connection.** Before it opens the WebSocket, the viewer calls a
GraphQL mutation to mint a token for the specific Virtual Participant it is about to
watch. The resolver looks the VP up in DynamoDB and mints only for the VP's owner, a
user it has been shared with, or a member of the `Admin` group. Which mutation it
calls depends on the launch type:

| Launch type | Mutation | Transport |
|---|---|---|
| `MICROVM` | `createMicrovmVncToken(vpId)` | Port-scoped auth token passed as a WebSocket subprotocol to the MicroVM's own endpoint |
| `EC2` / `FARGATE` | `createVncEdgeToken(vpId)` | Token passed as the `token` query parameter on `wss://<cloudfront>/vnc/<vpId>` |

For the ECS launch types the token is `base64url(payload).base64url(HMAC-SHA256(payload))`.
The payload names the `vpId`, the `/vnc/<vpId>` path, the target port, and an expiry a few
minutes out. A Lambda@Edge viewer-request function on the CloudFront `/vnc/*` behavior
recomputes the MAC and forwards the request only when the signature matches, the expiry is
inside the permitted window, and both the `vpId` and the path in the payload equal the ones
in the request. The signing key is generated per deployment into AWS Secrets Manager and
read only by the minting resolver and the edge function; the edge function fetches it once
per cold start from the stack's home region (Lambda@Edge takes no environment variables and
supports no layers, so the secret ARN and region are substituted into its source at deploy
time) and refuses the request if it cannot read it. Both mutations mint on demand per viewer
session, so the viewer's existing auto-reconnect simply mints again.

This is one of three controls, not the whole of it. The edge function sees traffic that
arrives through this deployment's CloudFront distribution, which is what step 3 below
establishes for every route into a task; the credential in step 2 applies at the VNC server
itself.

**2. A credential for the VNC server.** On the ECS launch types the VP generates a
random VNC credential when it boots and publishes it on its own record; the
`createVncEdgeToken` response carries it back to the already-authorized viewer, which
supplies it to the server. The credential belongs to one task and is replaced on every
boot. Under `MICROVM` the VP runs as a pre-snapshot stack at image-build time, where
there is no per-meeting record to publish a credential to and any credential would be
captured in the snapshot and shared by every launch; there the framebuffer is bound to the
VM's loopback interface and the port-scoped auth token is the only route in.

**3. An origin-verify header on the load balancer.** The CloudFront distribution attaches a
per-deployment header value to every request it sends to the VNC load balancer, and the load
balancer forwards only requests carrying it — anything else gets a 403. This applies to both
routes into a task: the rule the AI stack creates for the shared target group, and the rule
each Virtual Participant creates for its own path at run time. The participant is given the
secret's ARN, not its value, and reads the value from Secrets Manager once when it starts, so
the value never appears in its task definition; if it cannot read the value it does not
create a routing rule at all, and the participant fails to start rather than being reachable
without the header.

The load balancer's security group additionally admits only CloudFront's
`com.amazonaws.global.cloudfront.origin-facing` managed prefix list. That is a useful coarse
filter, and it establishes that a request came from CloudFront. The header establishes the
narrower property: that it came through this deployment's own distribution.

> **If you are upgrading, these take effect only after the Virtual Participant container
> image is rebuilt** — both are applied in code that runs inside the container:
>
> - The framebuffer's loopback binding (`entrypoint.sh`). Under `MICROVM` this runs at
>   image-build time, so it lands when the MicroVM snapshot is rebuilt; existing snapshots
>   keep the previous binding until then.
> - The origin-verify condition on the per-participant routing rule (`status-manager.ts`).
>
> Everything else in this section — the token check at the edge, the mutations, and the
> load balancer's own rule and default response — is CloudFormation and applies as soon as
> the stack update completes.
>
> Virtual Participants that are already running when the stack updates keep the routing rule
> their task created; those rules are deleted when each participant's meeting ends, by the
> same cleanup that removes its target group, so they clear on their own as those meetings
> finish. A rule can outlive its task if that task ended without the cleanup running; a
> participant started afterwards on the rebuilt image replaces any rule it finds on its own
> path before creating its own, and a leftover rule for a participant that no longer exists
> can be deleted from the load balancer's listener by hand (its `VirtualParticipantId` tag
> names the participant it belonged to).

#### Known limitations

These are current trade-offs rather than oversights, recorded so they are visible when
planning changes to this path.

**The ECS token is a bearer credential in a URL.** Browsers cannot set headers on a
WebSocket handshake, so the token travels as the `token` query parameter, and the `/vnc/*`
behavior sets `ForwardedValues: QueryString: true`. It therefore appears in CloudFront
access logs and in browser history, and it is a bearer credential — valid for whoever
presents it, with nothing binding it to the browser it was minted for. Its few-minute
lifetime and the VNC server credential are what bound that. The MicroVM path avoids
carrying the token in the URL at all, by passing it as a WebSocket
subprotocol, which is the same trick applied to a different endpoint. The same approach
could in principle be used here, but websockify would have to be configured to accept and
ignore the extra subprotocol, and the edge function would have to read it from the
`Sec-WebSocket-Protocol` header instead of the query string; that has not been done.

**The edge function calls Secrets Manager cross-region on a cold start.** A viewer-request
trigger is capped at 5 seconds and 128 MB, and the signing key lives in one region while
the function runs replicated in whichever edge region is nearest the viewer. The call is
made once per container and cached for its lifetime, and the function refuses the request
if it cannot complete — so the failure mode is a viewer in a distant region intermittently
being unable to open the live view, not an unchecked request. Lambda@Edge cannot be
pre-warmed. Worth watching after a first deployment.

**Rotating the signing key requires a deliberate rollover.** The key is cached for the life
of each edge container with no TTL, and nothing rotates it on a schedule. Replacing the
secret's value therefore leaves already-warm replicas verifying against the previous key and
rejecting newly minted tokens, for as long as those containers live — an interval that is
not bounded by anything in the design. A rotation needs either a tolerated window of
failures or a change that lets the function hold more than one key.

**Rotating the origin-verify value is harder still, and needs a stack update.** Five places
hold it: the CloudFront origin's header and the load balancer's own rule, both resolved at
stack create/update; the in-memory copy each running task read when it started; the literal
baked into each per-participant rule already on the listener; and Secrets Manager itself,
which newly started tasks read live. Replacing the secret's value **without** a stack update
leaves running participants working while every newly started one becomes unviewable — its
rule carries the new value, the distribution still sends the old one, so requests fall
through to the load balancer's own rule and return a 503 from the shared target group, which
holds no targets. Doing it **with** a stack update inverts that: new participants work and
every already-running one becomes unviewable for the remainder of its meeting. Either way a
rotation requires a stack update and interrupts live viewing for an interval bounded only by
how long the longest running meeting lasts. There is no rotation schedule on this secret, or
on any secret in this solution. Live viewing is a diagnostic aid rather than part of the
capture path, so a rotation does not affect transcription or recording.

**The hop from CloudFront to the load balancer is not encrypted.** The `vnc-alb` origin is
configured `OriginProtocolPolicy: http-only`, so the origin-verify header, the access token
in the query string, the VNC server credential in the RFB handshake and the framebuffer
itself all travel in clear text between the CloudFront edge and an internet-facing load
balancer. HTTPS to this origin is not available: CloudFront requires a publicly trusted
certificate matching the origin's domain name, and one cannot be issued for an
`*.elb.amazonaws.com` name. Moving the load balancer behind a **CloudFront VPC origin** is
the change that removes this along with the public DNS name, and is the recommended
direction for this path.

### Opening the preview from the meeting assistant, and closing a failed session

The meeting assistant can open or close the VNC preview on a meeting page; it does so
through the `toggleVNCPreview` mutation, which the meeting page receives through the
`onVNCPreviewToggle` subscription. Both apply the rule the meeting page itself applies:
they are answered for the meeting's owner, the users it is shared with, members of the
`Admin` group, and backend roles with IAM credentials such as the meeting assistant.

When a Virtual Participant fails to join, its live browser stays available for up to 10
minutes so the failure can be inspected. The **Got it — close Virtual Participant
session** button closes it sooner, through the `acknowledgeVPFailure` mutation. The button
is shown to the Virtual Participant's owner and to members of the `Admin` group, the
callers the mutation accepts; anyone else it is shared with sees the session close on its
own.

## Launch Types

### MicroVM launch type (default)

`VPLaunchType=MICROVM` is the **default**. It runs each Virtual Participant inside an [AWS Lambda MicroVM](https://docs.aws.amazon.com/lambda/latest/dg/microvms.html) — a Firecracker VM resumed from a memory+disk snapshot — instead of an ECS task. It runs **the same container image** as the ECS launch types; only the surrounding infrastructure differs.

**Requirements and limits** (all imposed by the service, not by LMA):

| | |
|---|---|
| Architecture | **ARM64 only** |
| Regions | us-east-1, us-east-2, us-west-2, ap-northeast-1, eu-west-1 |
| Max meeting duration | **8 hours** (hard ceiling) |
| Connections per MicroVM | 8 (not adjustable) |

**What it changes:**

- **No ALB.** Each MicroVM returns its own HTTPS endpoint, so the VNC ALB, its target group, listener, security groups, CloudFront `/vnc/*` behavior and the Lambda@Edge auth function are not created at all. The viewer authenticates with a short-lived, port-scoped MicroVM auth token instead. This is why `MICROVM` also removes those resources from the AI stack.
- **No hosts to scale.** No EC2 ASG, no capacity provider, no SSM host patching. Billing is per meeting rather than per warm instance, and there is no `WAITING_FOR_CAPACITY` wait for a host to launch.
- **Faster startup.** The Firecracker snapshot is taken with Xvfb, x11vnc, websockify and PulseAudio already running, and the image-build `/validate` hook exercises a real Chromium so Lambda prefetches its snapshot pages. Measured on a live deployment: VNC ready in ~20-25s.
- **Config delivery.** ECS passes per-meeting values as `ContainerOverrides` environment variables. MicroVM image environment variables are baked into the image and shared by every MicroVM launched from it (and capped at 50), so the launcher stages the full configuration in the VP task registry and the `/run` hook payload carries only the VP id. The service caps `runHookPayload` at 4096 bytes, which three Cognito JWTs alone exceed.
- **Egress through your VPC.** A `AWS::Lambda::NetworkConnector` routes MicroVM outbound traffic through the private subnets, so the VP reaches meeting platforms from the same NAT gateway Elastic IP as an ECS task. This costs no hourly charge (NAT data processing applies, which the ECS launch types already pay) and keeps meeting-platform-visible behavior identical across launch types.

**Still requires a VPC.** The WebSocket transcriber mandates the VPC and its NAT gateways regardless of how the VP is hosted, so `MICROVM` does not remove the VPC stack — it removes the VP's ALB.

### EC2

EC2 launch type uses warm instances with cached Docker images. This provides 85-90% faster startup compared to cold Fargate launches, with the VP ready in approximately 30-60 seconds. The estimated cost is approximately $33/month for always-on instances.

EC2 is the recommended choice in regions where Lambda MicroVMs are not available and startup latency matters.

### Fargate

Fargate launch type is serverless and uses SOCI (Seekable OCI) for faster container image pulls, providing 40-60% faster startup than standard Fargate. The base cost is approximately $2/month, making it more economical for infrequent use. However, startup time is longer at 1-2 minutes.

## EC2 Instance Types

> Applies to `VPLaunchType=EC2` only. Fargate and MicroVM have no instances to size.

Each VP container is capped at 3500 MB (observed peak memory is ~1650 MB with Chromium + Simli + Nova Sonic; the cap leaves headroom). A full voice-assistant + Simli-avatar meeting draws ~1.35 vCPU steady-state and runs reliably on the default **`t3.medium`** (the CloakBrowser stack downscales the avatar to 256×256@15fps and disables the emulated GPU, which keeps the encode cheap). Pick a larger instance mainly for **more concurrent VPs per host** rather than for a single meeting's headroom — and ensure host memory accommodates your expected concurrent VPs plus ~600 MB for the OS / ECS agent:

**General Purpose:**
- `t3.medium` (default) -- 3867 MB host → 1 concurrent VP. The capacity-provider auto-scaler launches additional hosts when concurrent demand exceeds capacity, so users running one meeting at a time pay the baseline (~$30/month per host) and only scale up while multiple VPs are active.
- `t3.large` -- 7857 MB host → 3 concurrent VPs. Pick this if you regularly run 2-3 concurrent meetings and want fewer scale-outs.
- `t3.xlarge` -- 15.7 GB host → 6 concurrent VPs.

**Compute-Optimized (non-burstable; for higher concurrency or guaranteed sustained CPU):**
- `c5.large` (2 vCPU) -- a dedicated-CPU alternative to `t3.medium` for a single voice + avatar VP (no burstable credit limits)
- `c5.xlarge` (4 vCPU) -- multiple concurrent voice + avatar VPs per host
- `c5.2xlarge` -- heavier concurrency

**Memory-Optimized (for large meeting context):**
- `m5.large` / `m5.xlarge` -- when meeting context / model memory needs are higher

`t3` instances are burstable: a single voice + avatar VP fits the `t3.medium` baseline, but if you run **multiple concurrent** VPs per host (or very long meetings), prefer `c5.*` / `m5.*` for guaranteed sustained CPU.

## Auto-Scaling

> Applies to `VPLaunchType=EC2` only. Fargate scales per task, and MicroVM launches one Firecracker VM per meeting with no cluster to scale.

The VP cluster uses an **ECS capacity provider with managed scaling**:

- **`VPMinInstances`** (default `1`) -- Minimum warm hosts always running. Set to `0` to fully scale down when idle and pay only when a VP is requested (cold-start adds ~60-90s to the first VP).
- **`VPMaxInstances`** (default `10`) -- Hard ceiling on concurrent hosts. With the `t3.large` default each host fits 3 VPs, so 10 hosts × 3 VPs = 30 concurrent meetings.

When concurrent demand exceeds the current cluster's capacity, ECS automatically launches new hosts (`TargetCapacity=100`, step size 1-2, instance warmup 90s). The launching VP shows status `WAITING_FOR_CAPACITY` while the auto-scaler provisions a new host — typically 60-90 seconds — then transitions to `BOOTING`. Scale-down to `VPMinInstances` happens automatically when reservation drops.

`ManagedTerminationProtection` is enabled, so a host running an active VP will never be killed by scale-in until the VP exits.

## Meeting Video Recording

In addition to the audio recording, the Virtual Participant can capture the meeting **screen** (screen shares, slides, participant video, the platform UI) as an MP4. The VP records its own X11 display — the same browser view you see in the live VNC viewer — with FFmpeg, uploads 60-second segments to S3 as the meeting runs (so a container crash loses at most the last segment), and assembles a single seekable `.mp4` when the meeting ends.

**Enabling / disabling**

- **Per meeting**: the **Create Virtual Participant** modal has a **Record meeting video** toggle (default **on**). Turn it off to capture audio + transcript only for that meeting.
- **Per deployment**: the `EnableVideoRecording` CloudFormation parameter (default `true`) sets the default the toggle starts from and applies to launches that don't specify a value (e.g. the `start_meeting_now` MCP tool).

**Playback**: once the meeting ends and the video finishes uploading, a **Recording Video** player appears in the meeting detail view alongside the **Recording Audio** player. The URL is stored on the call record (`Call.VideoRecordingUrl`) and presigned with your Cognito credentials at playback time — the same mechanism as the audio recording.

**Storage & retention**: final videos are written to the recordings bucket under `lma-video-recordings/` and follow the same retention as audio (`AudioRecordingExpirationInDays`). In-progress segments under `lma-video-chunks/` are deleted as soon as the final `.mp4` is assembled; a 1-day S3 lifecycle rule reaps any chunks orphaned by a crash. Deleting the meeting deletes its video recording and any of its segments still in the bucket, as it does the audio recording. Expect roughly **30-50 MB per meeting** at the default 5 fps (vs. ~5 MB for audio-only).

**Resource cost**: capture runs at 5 fps with `libx264 -preset ultrafast` to keep CPU overhead low (~5-10%). This fits comfortably on the default `t3.medium` host for a typical VP; if you run many concurrent video-recording VPs per host, size the instance up. Tunable via the `VIDEO_FRAMERATE`, `VIDEO_RESOLUTION`, and `VIDEO_SEGMENT_DURATION` task-definition environment variables.

## Transcription Engine

A Virtual Participant is transcribed by Amazon Transcribe by default. Where the
[ASR MicroVM engine](microvm-asr.md) is deployed (`EnableMicrovmAsr=true`, the default,
in a Region with AWS Lambda MicroVMs), an admin can move Virtual Participants onto it;
a VP on that engine falls back to Amazon Transcribe when a MicroVM cannot be
acquired. On Amazon Transcribe, with a voice assistant
configured, the VP sends Amazon Transcribe one stream with two channels, with channel
identification on: the meeting's participants on the first channel and the assistant's
voice on the second, the same layout as Stream Audio's tab and microphone. Participant
rows are on the `REMOTE` channel, named from the meeting roster, and only they are
checked for wake phrases and stop requests; the assistant's rows are on the `LOCAL`
channel under the VP's own name. A participant who talks over the assistant therefore
gets a line of their own, and the assistant can never wake or stop itself. A two-channel
stream is billed for its duration, the same as one mono stream. Without a voice
assistant the VP sends one mono stream of the meeting audio. The S3 audio recording is
always the mix of both, as is the video recording's audio track.

**Virtual Participant voice separation**, in the **Transcription Engine** section of
**Configuration ▸ Configuration** (admin only), is on every deployment. It asks for
per-voice labels, on either engine (on the on-demand engine, where its model bundle has
speaker identification), so several people behind one attendee tile (a conference room,
a shared screen playing a recording) come out as `Name (spk_0)`, `Name (spk_1)`, and each
voice corrects the roster name where the platform's speaker indicator lags. On by
default; a name that does not read as a room is still shown as one person, so an
ordinary attendee reads as their roster name. Clear it to name rows from the meeting
roster alone. See [Several people behind one attendee](#several-people-behind-one-attendee)
and [Cost of voice separation](#cost-of-voice-separation).

On a deployment with the ASR MicroVM engine the same section also shows the engine
choices:

- **Virtual Participants: engine** chooses the engine for every VP that starts after the
  change: Amazon Transcribe (the default) or the ASR MicroVM engine. On the MicroVM
  engine the VP acquires and releases its own MicroVM, and falls back to Amazon
  Transcribe when none can be acquired. The VP captures and records the meeting while
  the MicroVM launches and sends that audio to it when it connects; see
  [While the MicroVM starts](microvm-asr.md#while-the-microvm-starts).
- **The voice assistant is its own channel.** With the assistant enabled, its speech is
  transcribed from a second engine session on the same MicroVM and appears on the `LOCAL`
  channel under the VP's own name, never diarized, exactly as Stream Audio separates the
  microphone from the tab. Meeting rows are named after the attendee the platform showed
  during them (see [How Speaker Names Are Assigned](#how-speaker-names-are-assigned)).
- **Pausing keeps the engine session.** The chat pause command stops feeding meeting audio
  but leaves both sessions up, so the resume command continues the same transcript. If the
  engine dies mid-meeting, Amazon Transcribe takes over for the rest of it.

See [MicroVM ASR](microvm-asr.md) for the engine itself.

## Chat Introduction Message

The VP posts a customizable introduction message in the meeting chat when it joins. This message informs meeting participants that the VP is present and recording. You can configure the message content to suit your organization's requirements and compliance policies.

## In-meeting Chat Commands

Anyone in the meeting can control the VP by typing one of these commands in the meeting chat. Commands work across Zoom, Microsoft Teams, Webex, Chime and Google Meet. Every command is addressed to `LMA` by name.

| Command (typed in chat) | What it does |
| --- | --- |
| `LMA leave`, `LMA end`, `LMA stop`, `LMA quit`, `LMA exit` | The VP says goodbye and leaves the meeting. |
| `Goodbye LMA`, `bye LMA` | Same as above. |
| `LMA pause` | Stops transcription and recording without leaving the meeting. The audio recording keeps the paused time as silence, so it stays in step with the transcript. |
| `LMA start` | Resumes a paused session. |
| `LMA mute` | Silences the voice assistant at once, and keeps it silent — a wake phrase does not bring it back — until `LMA unmute`. Transcription carries on. Also `LMA quiet`, `LMA be quiet`, `LMA silence`, `LMA stop talking`, `LMA hush`. Needs a voice assistant; see [Muting the Assistant from Chat](voice-assistant.md#muting-the-assistant-from-chat). |
| `LMA unmute` | Brings a muted voice assistant back (also after a spoken "Hey Alex, stop"). Also `LMA resume`, `LMA speak`. This does not resume a paused transcript; that is `LMA start`, and the reply says so while the transcript is paused. |
| `LMA help` | The VP posts the list of chat commands in the meeting chat. |
| `LMA ask <question>`, `@LMA <question>`, `LMA <question>?` | The meeting assistant answers the question in the meeting chat, using the meeting's transcript. See [Asking the Meeting Assistant from Chat](#asking-the-meeting-assistant-from-chat). |
| `I consent`, `LMA consent` / `I object`, `LMA no consent` | In a meeting whose consent mode is Notify or Opt-in, a reply to the consent notice. See [Meeting Consent](#meeting-consent). |

When a Nova Sonic voice assistant is configured, the assistant-mode commands (`LMA translate`, `LMA translate <language>`, `LMA assist`, `LMA listen`, `LMA always`, `LMA wake`) also work, and `LMA help` lists them, along with the spoken forms: the wake phrase plus a mode ("Hey Alex, switch to translator mode") and "stop translating", which leaves translator mode and needs no wake phrase; see [Changing Mode During a Meeting](voice-assistant.md#changing-mode-during-a-meeting). With no voice assistant, or with ElevenLabs, which cannot change mode during a meeting, `LMA help` leaves them out. `LMA mute` and `LMA unmute` work with either provider and are listed whenever a voice assistant is running.

Every command is answered in the chat, including one that changed nothing ("the voice assistant is already muted"), so nobody is left retyping a command into silence. Note that `LMA stop` on its own is a dismissal and removes the VP from the meeting; to silence only the voice assistant, type `LMA mute` (or `LMA stop talking`).

Notes:

- Every command requires the literal token `LMA` in the message. For the dismissal commands that means bare words like `END` or `LEAVE` are ignored so that prose like *"the meeting will end at 3pm"* or *"I'll leave at 3"* never trips a false dismissal. The `LMA` and verb tokens are matched case-insensitively and word-bounded (so `endless` or `ENDPOINT` don't match either).
- When the chat platform exposes the sender name (Zoom, Chime, Webex, Google Meet), the goodbye message acknowledges who asked the VP to leave: *"Thanks Alice — I'll head out now."*
- The default introduction message tells participants to type `LMA leave` to dismiss the bot and `LMA help` to see the other commands, and the name to add when there is more than one LMA in the meeting (see [More than one LMA in a meeting](#more-than-one-lma-in-a-meeting)). Customize via the `IntroMessage` CloudFormation parameter of the Virtual Participant stack or the `INTRO_MESSAGE` env var; a customized message is used as it is. In it, `{LMA_USER}` is replaced by the name of the user who started the VP, and `{LMA_HANDLE}` by the name to add after `LMA` to address this VP.
- While `LMA pause` is in effect, the meeting's page in LMA shows a warning above its tabs that transcription is paused, since when, by whom when the chat shows the sender (Zoom, Chime, Webex and Google Meet, and the Zoom and Teams SDK joins; not the Teams web client), and that `LMA start` in the meeting chat resumes it; see [Web UI Guide › Meeting header](web-ui-guide.md#meeting-header). The VP records each pause and resume on the meeting record as it answers the command; if that write is lost, the command still takes effect and only the warning is missing. Consent withholding capture has its own warning and is not shown as a pause.
- `LMA pause`, `LMA start` and `LMA help` must be the whole chat message: the `LMA` addressee (or `@LMA`, before or after the command) plus the command word, matched case-insensitively with punctuation ignored, so *"@LMA, pause!"* and *"start LMA"* count as the command. A bare `PAUSE` or `START` without the addressee is ordinary chat, as is a message that merely contains the command — *"LMA PAUSED"*, *"please LMA pause now"*, or an introduction message that explains the commands.
- The help reply is posted as one to four single-line chat messages, listing each command in one canonical form: the general commands, then the voice-assistant modes and the mute commands when a voice assistant is running. How to address this VP by name is added only while the VP knows of another LMA VP in the meeting, since in a meeting with one VP that line is only more chat. It is answered whether or not transcription is paused. Apart from the help reply, the VP posts its introduction and the "started" status when it joins, the answer to each command, consent notices, and its goodbye; it does not post what the voice assistant says.
- On the Zoom browser join the VP reads the chat from Zoom's chat panel, which Zoom closes by itself, for one when someone starts sharing their screen. The VP reopens the panel within a few seconds whenever Zoom's toolbar shows it closed, and then reads the messages sent while it was closed; a command typed while the panel was closed is therefore acted on a few seconds late rather than missed. It reads the messages already in the panel when it starts listening as sent before it joined and does not act on them, which matters on a meeting whose chat continues in a Team Chat group chat (*Meeting Group Chat*), where the panel can hold messages from earlier occurrences of the meeting.
- On the Teams browser join the VP reads and posts the chat through Teams' chat pane, which shares the side rail with the People pane. On the anonymous join, opening the People pane puts it in the chat's place, and closing it again does not bring the chat back; the toolbar has no chat button, and the toolbar's **More** menu has no chat item (its items are settings and toggles such as **Show chat bubbles**). So the VP keeps the chat open by never opening the People pane: its consent evidence there is the chat screenshot only (see [Meeting consent](meeting-consent.md#what-each-platform-supports)). It never opens the **More** menu, and clicks a toolbar chat button only on a client that has one. If the chat pane is closed from the meeting anyway, the VP logs that once and does not reopen it: until someone opens it again, chat messages are not read, and posts are reported as not posted, so the consent flow keeps posting its notice and does not record it as posted. Messages sent while the pane was closed are read once it is open again, so a command typed meanwhile is acted on late rather than missed. Posts, the consent screenshots and the VP's periodic check of the chat pane take turns, so a screenshot never shows a half-typed message. A message counts as posted only when the compose box held it before it was submitted: an empty compose box is not submitted, and the message is reported as not posted. The VP types keystrokes and presses Enter only in the compose box when it has the focus. It does not press Teams' **Retry** on a message that failed to send, since the caller posts again; a post that Teams shows as failed, or that stays in the compose box, is reported as not posted (the consent flow then posts its notice again; see [Meeting consent](meeting-consent.md#notify)).

### More than one LMA in a meeting

When two or more people each invite their LMA Virtual Participant to the same meeting, `LMA leave` is addressed to all of them. So each VP acts on a chat command, a question, or a consent reply addressed to LMA only when it is addressed to that VP by name, or when it is the only LMA VP it knows to be in the meeting:

- **By name.** A VP's display name is the **Display name** setting in the Virtual Participant section of the [Configuration page](configuration-guide.md), with `{LMA_USER}` replaced by the name of the user who started the VP: `LMA - {LMA_USER}` by default, which gives `LMA - <user>` on every platform. An administrator can change it there, in the active profile, without a stack update; the change applies to the next VP to start. The setting accepts only letters, digits, spaces, the characters `- ' . _ @` and `{LMA_USER}`, and must start with a letter or digit, because Microsoft Teams accepts only those characters in a guest's name; on Teams, a character of the filled-in user name that Teams does not accept (an accent, a comma) loses its accent or is left out. A profile that saved the earlier default, `LMA ({LMA_USER})`, keeps it and its VPs still start under it, shown as `LMA - <user>` on Teams; its Virtual Participant section can be saved again once the name is changed to one the setting accepts. Keep `LMA` as its first word and keep `{LMA_USER}` in it, for the reasons below. Typing or mentioning that name addresses the VP: *"@LMA - Bob Strahan leave"*, *"LMA - strahanr@example.com summarize the meeting"*. So does `LMA` followed by the part after `LMA -` (or the part in parentheses, for a name such as `LMA (Bob Strahan)`), or its first word (the user's first name, or the part of an email address before the `@`): *"LMA Bob leave"*, *"@LMA strahanr help"*, *"LMA (Bob) what was decided?"*. The rest of the message is then read as if it had been typed after a bare `LMA`, so every command, `LMA ask`, the bare question form and `LMA consent` work the same way. A first word that is itself a command or a question word (a user called "Help Desk") is not used as a short name; the whole name still works. When several VPs could match, the longest name wins, so with VPs for Bob Strahan and Bob Smith in one meeting *"LMA Bob Smith leave"* reaches only Bob Smith's.
- **A short name two VPs share.** With those two VPs, *"LMA Bob leave"* and *"LMA (Bob) leave"* could mean either, so neither acts on them. Each VP's introduction and help reply then give its whole name in parentheses instead, *"LMA (Bob Strahan) leave"*, which reaches only that VP. Two VPs with the same display name cannot be told apart at all.
- **The bare `LMA`.** `LMA leave`, `@LMA help` and the like are acted on as before when the VP knows of no other LMA VP in the meeting. When it knows of one or more, it does nothing with them — it does not post the help either — since it cannot tell which VP was meant.
- **Another VP's name.** A message addressed to another VP by its name, its short name, or any name in parentheses after `LMA` that is not this VP's is left to that VP.
- **Not addressed to LMA.** A consent reply that does not name LMA (*"I agree"*) is read by every VP, as before, and ordinary chat is unaffected.

The introduction message and the help reply say which name to add. A VP knows another one is in the meeting from a display name whose first word is `LMA`: a chat sender (every VP posts its introduction when it joins), a name the platform shows as speaking, a Google Meet participant tile, or the call's participant list on the Teams Azure Communication Services join, which also shows when one has left. On the other joins, a VP is taken to have left when it posts its exit message (this deployment's, or the default *"Live Meeting Assistant has left the room."*, or the *"Thanks <name> — I'll head out now."* it posts to whoever asked it to leave), or when it has not been seen for 10 minutes. Each chat message it posts, each time the platform shows it as speaking, and on Google Meet its tile (read every second) count as seeing it. So a VP that was removed from the meeting or stopped without posting its exit message stops counting after at most 10 minutes, and the bare `LMA` is answered again. The cost is on the Zoom and Teams browser joins, where a VP with no voice assistant that stays quiet in the chat is seen only once, when it posts its introduction: 10 minutes later it is no longer counted, and both VPs answer a bare `LMA` again, as they would without this feature. Address VPs by name to avoid that. A VP that has not yet appeared in any of these ways is not counted, so a VP that joins after another may answer a bare `LMA` until it has seen the first one; address VPs by name when there are several. A VP whose display name does not start with `LMA` (a customized display name) cannot be told from a person by its name. Messages from another LMA VP are never acted on. On Microsoft Teams a mention shows only the mentioned name, without the `@`; both forms work, as does Zoom's `@` mention.

### Asking the Meeting Assistant from Chat

`LMA ask <question>` (also `@LMA ask <question>`, `LMA, ask: <question>`) sends the question to the deployment's Strands meeting assistant — the same agent the LMA meeting page's assistant and the voice assistant's lookups use — with the meeting's transcript as context, and posts the answer in the meeting chat. `@LMA <question>` works too when the text after `@LMA` is not one of the other commands, so `@LMA help` is still help and `@LMA translate` still changes mode. A message that starts with `LMA` and is not one of the other commands is a question too when it reads as one: it ends with a question mark, or its first word is one of *what*, *whats* (*what's*), *who*, *whos* (*who's*), *whom*, *whose*, *when*, *where*, *why*, *how*, *which*, *summarize*, *summarise*, *recap*, *explain*, *list* or *tell*. That is the whole list: a message starting with any other word needs the question mark (or `LMA ask`). So *"LMA what was just discussed?"* is answered like *"LMA ask what was just discussed?"*, while *"LMA is great"* is ordinary chat and *"LMA help?"* is still help.

- **Availability.** It needs a Strands meeting assistant (**MeetingAssistService** set to one of the `STRANDS_BEDROCK` options); it does not need a voice assistant. `LMA help` lists `LMA ask` only when one is configured. Without one, an explicit `LMA ask` is answered with a note saying so, and `@LMA` and `LMA <question>?` messages are ignored. **For operators:** on a Strands deployment any participant, including a guest, can ask, and each question is a meeting-assistant (Bedrock) request billed like any other. There is no separate switch to turn chat questions off; open an issue if your deployment needs one.
- **What the agent can use.** A chat question is an [in-meeting request](meeting-assistant.md#in-meeting-requests), answered as **Meeting Assistant › In-meeting requests** on the Configuration page (`meeting_assist.inMeetingRequestTools`) allows; a change applies within about five minutes to questions asked after that.
  - **Current meeting only** (`current_meeting_only`, the default). The answer comes from the current meeting alone: its transcript, the messages typed in its chat, its [agenda](meeting-agenda.md) and what was shown on its [shared screen](meeting-video.md) (each frame's title and text). No other tool is offered — no web search, document knowledge base, past meetings, MCP servers, or VNC and browser controls. Anyone in the meeting, including a guest, can therefore ask about the agenda of the meeting they are in (`LMA ask what's left on the agenda?`); ask through the LMA meeting page for answers that use the other tools.
  - **Unrestricted** (`unrestricted`). Every chat question is offered every tool the meeting page's assistant has — web search (where configured), the document knowledge base, the owner's past meetings, MCP servers and the VNC and browser controls — acting for the VP's LMA user, whoever asked. A chat sender is known to the VP only by the display name the meeting shows, so every sender is treated alike. Choose it only where everyone who can join the meeting may read what those tools return, since the answer is posted for the whole meeting.

  The same setting applies to requests spoken to the [voice assistant](voice-assistant.md#what-the-voice-assistant-can-look-up) and with the wake phrase; the meeting page's own assistant is always unrestricted. **For operators:** whatever the choice, an answer can draw on anything in the meeting's transcript and chat so far — including what was said before the person asking joined — and is posted where everyone in the meeting can read it.
- **How answers are posted.** The answer is reduced to plain text on a single line, starting `Live Meeting Assistant:`. Markdown is removed; a link is shown as its text followed by its full address in parentheses, and a bare address is left whole, so it can be followed from the chat. Long answers are split at sentence ends into at most three messages, numbered `(1/3)`, `(2/3)`, ..., and the rest is cut with an ellipsis. LMA keeps each message to 1,000 characters on Zoom and Teams, 500 on Webex and Chime, which post by typing, and 500 on Google Meet; these are LMA's own conservative limits, not the platforms' maximums.
- **Limits.** Each participant may ask once every 20 seconds (told once if they ask sooner), questions over 500 characters are declined, and one question is answered at a time. On the Teams web client the sender is not visible, so everyone there shares one 20-second limit. Answers usually take a few seconds and can take longer when the agent searches; other commands are not held up meanwhile. If no answer arrives within 90 seconds the VP says so and takes the next question. While `LMA pause` is in effect questions are not sent to the meeting assistant; an explicit `LMA ask` is told to wait for `LMA start`. An answer that becomes ready while the VP is posting another message (a help or mode reply, say) can interleave with it on any platform; it is most likely on Webex and Chime, which post by typing.
- **The VP never answers itself.** Its answers begin `Live Meeting Assistant`, which cannot be read as a question; its configured introduction, recording-start, recording-stop and exit messages are recognised and ignored; and messages from the VP's own display name are ignored.
- The Virtual Participant logs each question and answer as `[chat-ask] question ...` and `[chat-ask] answered platform=<platform> chars=<n> messages=<n> ms=<n>`, without the question text. The meeting-assistant function's own log records the request it receives, question included, as it does for every meeting-assistant request.

## Meeting Chat in the Transcript

The VP adds the messages participants send to everyone in the meeting chat to
the meeting's transcript, on Zoom, Microsoft Teams and Google Meet. Each message becomes one transcript line on its own
channel, `MEETING_CHAT`, with the sender's display name as the speaker (or
*Unknown sender* where the platform does not show one) and the message as it
was typed. Its time is the point in the meeting at which the VP read the
message, on the same clock as the speech: no platform gives a message time that
can be placed on the recording's timeline. A link in a message whose address is
not in its text is added after the text as `(link: <address>)`. The meeting
page shows these lines among the speech, labelled *<name> · chat*, as plain
text with any web address shown as code to copy rather than as a link; see
[Meeting chat messages](web-ui-guide.md#meeting-chat-messages).

It is on by default. Turn it off with **Virtual Participant › Add the meeting
chat to the transcript** on the Configuration page; like the section's other
settings, a change applies to the next Virtual Participant to start.

What is not added:

- the LMA chat commands, whatever state the meeting is in (`LMA start` typed
  while the meeting is already running is still a command). An `LMA ask` or
  `@LMA` question **is** added, since it was asked in front of everyone,
  and so is the meeting assistant's answer the VP posts to it, once, under
  the VP's display name. The VP's notices in reply to a question (asked too
  soon, still answering another, too long, no answer from the assistant) are
  not added.
  An answer is added only when its question was: the answer to a question
  that was not added (sent privately, say, or while the transcript was
  paused) is not, nor is one that arrives after `LMA pause`;
- replies to a consent notice, and everything the VP itself posts (its
  introduction, status messages and help, and the answers as the chat panel
  shows them, which are added once as above);
- private messages: on Zoom (the browser join and the Meeting SDK join), a
  message sent to anyone but everyone (on a meeting whose chat continues in a
  Team Chat group chat, a message to *Meeting Group Chat* is sent to everyone), and a message whose recipient the VP
  could not read (a chat label in a language other than English, or a Meeting
  SDK message that names no receiver). Only a message the VP read as sent to
  everyone is added. Teams and Google Meet meeting chats have no private
  messages;
- what the chat panel already held when the VP first read it (the Teams
  browser join reads the whole panel once it is in), and anything sent while the
  transcript is paused (`LMA pause`) or while consent is withheld. Those
  messages are not kept for later: resuming does not add them;
- everything while **Amazon Transcribe › Redact personal information** is on,
  whatever the meeting's language, because chat text is not redacted. If the
  VP could not read the Configuration page's values when it started, it does
  not know the saved choice and adds no chat for that meeting
  (`outcome=redaction-unknown`; the `[SETTINGS]` line at start says why the
  values were not applied);
- more than 60 messages a minute, or 3,000 in a meeting;
- edits and deletions made after a message was sent: the line keeps the text
  the VP first read;
- the chat of Webex and Amazon Chime meetings. The Webex chat the VP reads
  does not reliably show which messages are private, so Webex is left out
  until it can be told apart. The VP still reads Webex chat for the chat
  commands, and writes the debug line below for it with
  `outcome=not-covered`, so a live meeting shows whether a private message
  can be recognised (`recipient=direct`).

Chat lines are stored with the meeting's transcript, expire with it (**Days
to keep transcript segments**), and are deleted with it, including by
[Delete the transcript after the summary](transcript-deletion.md).

The meeting summary uses them. Each message is part of the transcript the
summary sections read, as a line beginning `[chat]`, and a meeting with chat
gets a **CHAT & SHARED LINKS** section: what was discussed in the chat, and
the links participants shared, listed from the messages themselves rather
than written by the model. See
[Chat and Shared Links](transcript-summarization.md#chat-and-shared-links).
The summary is kept when the transcript is deleted, so the links it lists
outlive the chat lines they came from. The meeting assistant reads them too:
its `current_meeting_transcript` tool returns each message among the speech as
a line `[chat] <sender>: <message>`, with its time when the transcript is asked
for with times. The copy of the transcript written for the knowledge base's
meeting-history search, which the MCP `get_meeting_transcript` tool also
returns, keeps the chat lines in the same form. Chat lines are not used by
sentiment, speaker naming or the wake phrase: those read what was said.

How well each platform's chat can be read differs, and none of this has been
measured in a live meeting yet:

| Platform | Sender | Message id | Private messages |
| --- | --- | --- | --- |
| Zoom (browser) | From the message's label (English labels) | The message element's id | Left out, as is a message whose label shows no recipient |
| Zoom (Meeting SDK) | From the SDK | From the SDK, where it sends one | Left out, as is a message that names no receiver |
| Teams (browser) | The message's author, where the row shows it | The message element's id, else the text | None in a meeting chat |
| Teams (Azure Communication Services) | From the chat service | From the chat service | None in a meeting chat |
| Webex | Not added to the transcript (see above) | | |
| Google Meet | From the message group's header | Meet's message id | None in a meeting chat |

With the stack's `LogLevel` at `DEBUG`, the VP writes one line per chat message
it reads, `[meeting-chat] platform=<platform> outcome=<what happened>
sender=yes|no id=yes|no time=yes|no recipient=everyone|direct|unknown
links=<n> chars=<n>`, with no message text and no sender name, so a live
meeting shows what each platform actually gives. The VP never logs chat text.

## Meeting Consent

When the deployment's consent mode (the Meeting Consent section of the
Configuration page), or the **Meeting consent** choice made on the Create Virtual
Participant form, is Notify or Opt-in, the VP posts a notice in the meeting chat
once it is admitted and reads the replies typed in the chat and, unless the
Meeting Consent setting **Read spoken replies** is off, those said aloud and
addressed to LMA (*"LMA, I consent"*, *"LMA, I object"*). In
Notify it records and transcribes from the start, and stops at once when a
participant objects. In Opt-in it joins with capture withheld (no transcript,
audio or screen recording, voice assistant or chat added to the transcript);
with spoken replies read, the meeting audio still goes to the meeting's
transcription engine, only so that a reply said aloud can be heard, and nothing
else heard is kept (see
[Spoken replies](meeting-consent.md#spoken-replies)). It starts only when every
participant present has consented, in the chat or aloud, or when the meeting's
owner has confirmed in LMA that everyone agreed;
until then its detail line reads **Waiting for consent: …** and `LMA start` is
refused. It keeps screenshots of the chat and the participant list as evidence
when consent is granted and on every objection, and can be set to leave the
meeting after an objection. With mode Off it behaves as before, apart from a
short read of its own row before joining, where a per-meeting choice is stored;
see [Choosing the mode for one meeting](meeting-consent.md#choosing-the-mode-for-one-meeting).

A consent reply is the whole chat message, as for the commands above: one of
the configured consent phrases (by default *"I consent"*, *"I agree"*, *"I
accept"*) or `LMA consent`. An objection is one of the configured objection
phrases (by default *"I object"*, *"I do not consent"*, *"stop recording"* and
others), `LMA no consent` or `LMA object`, or a chat message of up to 200
characters that contains an objection phrase outside quotation marks (*"I
object to this meeting being recorded"*); see
[Replies in the chat](meeting-consent.md#replies-in-the-chat). Said aloud, a
reply counts only when it is addressed to LMA: *"LMA, I consent"* or *"LMA
consent"*, and *"LMA, I object"* or *"LMA, please stop recording"*. Which platforms
support which rule, how the participants present are counted, and what is
stored are in [Meeting Consent › Virtual Participant](meeting-consent.md#virtual-participant).
While the meeting runs a consent flow, `LMA help` adds a line naming
`LMA consent` and `LMA no consent`.

## Troubleshooting

- **Scheduling issues**: Check the Step Functions execution logs in the AWS console for errors related to meeting scheduling and trigger timing.
- **Joining or streaming issues**: Check ECS Fargate or EC2 task logs in CloudWatch for errors during the browser launch, meeting join, or audio streaming phases.
- **VP stuck "in progress"**: This may indicate that the ECS task crashed unexpectedly. This issue was identified and fixed in v0.3.0. Ensure you are running the latest version.

## Developer Testing

### Manual Step Function Execution

You can manually invoke the VP Step Function with a JSON payload for testing. The payload supports the following methods:

- **POST** -- Start a new VP session with specified meeting details
- **GET** -- Retrieve the status of an existing VP session
- **DELETE** -- Stop and clean up a running VP session

### Local Docker Testing

For local development and debugging, run the VP Docker container against a deployed LMA stack using the `make vp-start` target:

```bash
make vp-start STACK_NAME=<your-stack> PLATFORM=WEBEX MEETING_ID=<id>
```

This invokes `lma-virtual-participant-stack/backend/local-test.sh`, which reads configuration from CloudFormation, generates a `.env.local`, builds the Docker image, and runs the container locally with VNC exposed on ports 5900 / 5901.

For the recommended EC2 + VSCode Remote-SSH + VNC workflow — including how to manage secrets (`--reuse-env`), enable dev-mode auto-reload, and fix stale VSCode port forwarding — see [Virtual Participant Local Development](virtual-participant-local-dev.md).

## See Also

- [Virtual Participant Local Development](virtual-participant-local-dev.md) -- EC2 + VSCode + VNC workflow for running the VP locally
- [Stream Audio](stream-audio.md) -- Browser-based audio capture alternative
- [Voice Assistant](voice-assistant.md) -- Add a voice assistant to the Virtual Participant
- [Simli Avatar Setup](simli-avatar-setup.md) -- Configure a visual avatar for the VP
- [MicroVM ASR](microvm-asr.md) -- The on-demand transcription engine and its Virtual Participant switches

