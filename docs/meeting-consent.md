---
title: "Meeting Consent"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Consent

LMA can tell a meeting's participants that the meeting is being recorded and
transcribed, and can wait for them to agree before it records or transcribes
anything. Whether it does either is the **consent mode**, set for the whole
deployment on the Configuration page and, where the deployment allows it,
chosen again for a single meeting when the meeting starts. What participants
replied, who attested on their behalf, and any screenshots kept as evidence are
stored on the meeting in a **consent record**, beside its transcript.

The record is supporting evidence of what was said in the meeting. It is not a
legal determination that consent was given, and LMA does not decide which
rules apply to a meeting: recording and transcription laws differ between
countries, states and organisations, some require every party's consent and
some only one party's, and some set requirements a chat reply does not meet.
Choose the mode with whoever advises your organisation on those rules. Nothing
on this page is legal advice.

See also [Privacy and Data Handling Controls](privacy-and-data-handling.md),
which brings together the consent options, what LMA stores and for how long,
and the deletion controls.

**Current state:** the consent settings, the consent record and its API, the
evidence check and the meeting page's Consent section are in place. The
**Virtual Participant** runs the consent flow: it posts the notice in the
meeting chat, reads the replies typed there or said aloud, and withholds or
stops recording and transcription accordingly (see
[Virtual Participant](#virtual-participant)).
**Stream Audio** and the **Desktop Capture App** run it too: they show the
notice, ask the user to confirm, and the WebSocket transcriber records that on
the meeting and withholds or stops recording and transcription accordingly (see
[Stream Audio and the Desktop Capture App](#stream-audio-and-the-desktop-capture-app)).
The browser extension does not run it yet; how the
transcriber treats a client that shows no consent step is a setting. See
[Flows by capture method](#flows-by-capture-method).

## The three modes

| Mode | What participants see | When the meeting is recorded and transcribed |
|---|---|---|
| **Off** | Nothing | Always. This is how LMA behaves when consent has never been set up, and the shipped default. |
| **Notify** | A notice that the meeting is recorded and transcribed, and how to object | From the start, until a participant objects. An objection stops recording and transcription, and is recorded. |
| **Opt-in** | A request to agree, with the phrase to reply | Not until the consent rule below is met. An objection from anybody stops it again. Nothing is recorded or kept before then. A Virtual Participant still sends the meeting audio to the transcription engine while it waits, only to hear replies said aloud, unless **Read spoken replies** is off; see [Spoken replies](#spoken-replies). |

An opt-in meeting is granted by one of two **consent rules**:

- **Each participant consents.** Every participant listed as present has
  replied with consent. A participant who joins later and has not replied
  withholds recording and transcription again until they do.
- **The meeting owner attests that everyone agreed.** The person who owns the
  meeting in LMA confirms that everyone agreed: out loud, in the chat, or in the
  invitation's terms. An objection from any participant still stops recording
  and transcription, whatever the owner attested.

## Status, and when a meeting may be captured

Each meeting's consent record has a **status**, worked out again every time
something is recorded on it:

| Status | Meaning |
|---|---|
| `not_required` | The meeting runs in mode Off, or its consent was never started. |
| `pending` | Notify: the notice has not been recorded as posted yet. Opt-in: the rule is not met yet. |
| `granted` | Notify: the notice was posted and nobody has objected. Opt-in: the rule is met and nobody has objected. |
| `objected` | A participant's latest reply is an objection. |

The record also says whether the meeting **may be recorded and transcribed
now** (`CaptureAllowed`), and this is the one value a capture client acts on:

- **Opt-in:** only while the status is `granted`.
- **Notify:** unless the status is `objected`.
- **Off:** always.

A meeting with no consent record is judged by the deployment's mode: in a
deployment whose mode is opt-in it may not be captured until its consent has
been started and granted, because the absence of a record is not consent.

Every change of status, or of whether the meeting may be captured, is added to
the record's **status history** with the time and a short reason ("1
participant objected", "every participant consented"), so the record shows when
recording stopped and resumed as well as why.

## How a reply is read

A participant's reply — a chat message, or something said in the meeting — is
read as consent or as an objection by the phrases in the Meeting Consent
settings:

- It is an **objection** when it contains one of the objection phrases as whole
  words, ignoring case, punctuation and spacing.
- Otherwise it is **consent** when it contains one of the consent phrases.
- Objections are looked for first, so "I do not consent" is an objection
  although it contains "consent", and "I agree, but I object to the recording"
  is an objection.
- Anything else is neither, and is not recorded.

The Virtual Participant reads the meeting chat and speech by its own rules: a
consent is a consent phrase as the whole message, or `LMA` followed at once by
a consent phrase anywhere in a message of up to 200 characters; a typed
objection may be part of a message of up to 200 characters; only text outside
quotation marks is searched; and a spoken reply counts only when it is
addressed to LMA. See
[Replies in the chat](#replies-in-the-chat) and
[Spoken replies](#spoken-replies).

Each participant's **latest** reply is the one that counts: somebody who
objected and then consented has consented, and the other way round. Replies are
matched to participants by their display name in the meeting, compared without
regard to case or extra spacing.

## The Meeting Consent settings

An Admin sets the deployment's default in the **Meeting Consent** section of
**Configuration ▸ Configuration** (see the [Configuration Guide](configuration-guide.md)).
A saved change applies to meetings whose consent starts after the save; a
meeting already under way keeps the mode and rule it started with.

The configuration presets set these settings together with the related
retention and privacy ones: **Internal meetings – notify participants** sets
Notify, and **Regulated – consent required, transcript not retained** sets
Opt-in with the each-participant rule, a stricter choice only per meeting,
clients without a consent step refused and the Virtual Participant leaving
after an objection. Under Regulated, Stream Audio and the Desktop Capture App
cannot collect each participant's consent, so Start is refused from them (see
[Stream Audio and the Desktop Capture App](#stream-audio-and-the-desktop-capture-app)),
and a Virtual Participant on the
Webex join never meets the each-participant rule and cannot move to
owner-attests, so its meeting is not transcribed (see
[What each platform supports](#what-each-platform-supports)). On the Teams
browser join the rule is met only by consent said aloud. Nothing is recorded or
kept before consent under Regulated; while it waits a Virtual Participant
listens for spoken replies, so the meeting audio is processed by the
transcription engine and only the reply events are kept, unless **Read spoken
replies** is off. No preset sets **Read spoken replies**, so applying one leaves
it as it is.
**Regulated – notify with opt-out, transcript not retained** sets the same as
Regulated except the mode, which is Notify: capture runs from the start and
stops when a participant objects, and a meeting may use Notify or the stricter
Opt-in, not Off. Notify and objections work on every Virtual Participant join,
and Stream Audio and the Desktop Capture App run their notify step and capture
from Start, while clients without a consent step are still refused (see
[Clients without a consent step](#clients-without-a-consent-step)). **Standard** puts
back the shipped defaults. Apply one from
the editor's **Actions ▸ Apply a preset…**, or choose one at deploy time with
the `ConfigurationPreset` stack parameter; see
[Configuration presets](configuration-guide.md#configuration-presets).

| Setting | Meaning | Shipped default |
|---|---|---|
| Consent mode | Off, Notify or Opt-in | Off |
| Consent rule (opt-in) | Each participant consents, or the meeting owner attests | Each participant consents |
| Notice text (notify) | The notice a notify meeting posts | "{LMA_USER} is using Live Meeting Assistant to record and transcribe this meeting ({meeting}). If you do not agree, say so, and recording and transcription will stop." |
| Consent request text (opt-in) | The request an opt-in meeting posts | "{LMA_USER} would like to record and transcribe this meeting ({meeting}) with Live Meeting Assistant. Nothing is recorded or transcribed until everyone has agreed." |
| Consent phrases | Comma-separated phrases read as consent | I consent, I agree, I accept |
| Objection phrases | Comma-separated phrases read as an objection | I object, I do not consent, I don't consent, I do not agree, I don't agree, do not record, don't record, stop recording |
| Allow a different choice per meeting | Whether a meeting may be started with another mode, rule or notice | On |
| Keep evidence screenshots | Whether evidence images stay with the meeting. Off: each image is deleted once its check is stored, and the Virtual Participant stores only images it has checked automatically, so it keeps no participant-list screenshots and none for objections recorded in LMA or said aloud (see [Evidence screenshots](#evidence-screenshots)) | On |
| Evidence check model | The Amazon Bedrock vision model that checks evidence images; see [Checking evidence with a vision model](#checking-evidence-with-a-vision-model) | Amazon Nova Lite |
| Streaming clients without a consent step | What the WebSocket transcriber does, in Notify or Opt-in, with a meeting from a client that shows no consent step: **Accept with a warning** (captured as before, with no consent record, and a warning logged) or **Refuse** (not recorded or transcribed, and the client is told why). See [Clients without a consent step](#clients-without-a-consent-step) | Accept with a warning |
| Virtual Participant leaves after an objection | Whether a Virtual Participant leaves the meeting after a participant objects, in the chat or aloud, rather than staying in it without recording | Off |
| Read spoken replies | Whether a Virtual Participant also reads consent replies and objections said aloud and addressed to LMA, sending the meeting audio to the transcription engine while consent is awaited, or reads the chat only; see [Spoken replies](#spoken-replies) | On |

In the two texts, `{LMA_USER}` is the person using LMA in the meeting,
`{meeting}` the meeting's name, and `{consent_phrase}` and `{objection_phrase}`
the first phrase of each list. Any other text in braces is posted as written.
The shipped texts say nothing about how to reply, because each capture method
reads replies in its own way: a Virtual Participant posts one line after the
notice that gives the reply in the form it reads (see
[How the Virtual Participant asks for replies](#how-the-virtual-participant-asks-for-replies)),
and Stream Audio and the Desktop Capture App show the notice to the LMA user,
who confirms how everyone agreed. A customised text is posted as written,
placeholders filled in, and the Virtual Participant's line still follows it, so
a text customised before this line existed and that carries its own reply
instruction gives both.

With **Allow a different choice per meeting** off, a meeting may still be
started with a *stricter* choice than the default — opt-in in a notify
deployment, or each participant in an owner-attests deployment — but not a less
strict one, and not with its own notice text.

The settings are read when they are needed, with no cache, because they decide
whether a meeting may be captured. If they cannot be read, starting a meeting's
consent fails rather than falling back to off, and so does reading a meeting
that has no consent record; a meeting with a record is read without the
settings, since its record decides its own gate.

## On the meeting page

The meeting page's header names the consent setting that applied to the
meeting and its status (*Consent Opt-in · granted*), or *Consent enforcement
off* for a meeting whose consent was off or never started; selecting it opens
the record. The **Details** tab's **Capture and processing** section shows the
same setting with the status. While a live meeting in notify or opt-in mode has
recording and transcription withheld, a warning above the tabs says that
transcription is paused until consent is recorded (or that a participant
objected), from 30 seconds after the meeting started, with a link to the record.

A meeting whose consent was in notify or opt-in mode has a **Consent** section
on its **Details** tab. It shows:

- the **mode** and, for opt-in, the **rule**;
- the **status**, and whether the meeting may be **recorded and transcribed**
  now (*Allowed* or *Withheld*);
- the **notice** posted, and when it was first posted;
- the **participants**: everyone listed as present, then anybody else who was
  listed or replied, each with their latest reply (*Consented*, *Objected* or
  *No reply*);
- the owner's **attestation**, when there is one;
- the **evidence** images as thumbnails, each with what it was taken for and,
  when it has been checked, the vision model's verdict (shown as *Image check:*,
  because it reports what that image shows, which can differ from the record's
  status), confidence and summary; selecting one opens it full size;
- a **timeline** of everything, oldest first: consent started, notices posted,
  replies and attestations, and each change of status with its reason. Replies
  are placed by when LMA recorded them, the order the status is decided in.

If the record cannot be read when the page opens, the section says so rather
than disappearing, and the summary download has no record to include until one
is read. If a later read fails, the section shows the error above the last
record read, and the download carries that record.

The images are shown through links signed for whoever opened the page, after
the same access check as the transcript, and last an hour. An image that has
expired (see [What is stored](#what-is-stored)) or was deleted after its check
is shown as such, with its entry and check. The section updates
as the record changes, whoever changed it. A meeting whose consent was never
started, or ran in mode off, shows no section.

Three actions, each offered only to the people the backend lets do it:

- **Record an objection** — the owner, anyone the meeting is shared with, and
  Admins. Enter the participant's display name and, optionally, what they said.
  It is recorded as source `ui` under your username, and stops recording and
  transcription in a notify or opt-in meeting until that participant consents.
- **Attest that everyone agreed** — the meeting's owner only, and only for an
  opt-in meeting under the **The meeting owner attests** rule, where it is what
  grants the meeting. Choose how everyone agreed (out loud, in the chat, or by
  accepting the invitation's terms) and, optionally, add a statement. It grants
  the meeting unless somebody objected.
- **Check with model** (or **Check again**) on an evidence image — the owner
  and Admins. See
  [Checking evidence with a vision model](#checking-evidence-with-a-vision-model).

### In the summary download

The summary's downloads and its copy to the clipboard, under **Download** in
the meeting page's header, add the consent record after the summary: the
mode, rule and status, whether capture was allowed at the time of the download,
the notice, the attestation, each participant's latest reply, the timeline, and
each evidence image, described by its note with when it was captured and its
image check (*"- Meeting chat when Bo Chen objected (screenshot captured
2026-11-02T10:02:30.000Z). Image check: Objection found (model
amazon.nova-lite-v1:0, confidence 0.9)"*, or *"Image check: not run"*),
followed by the note that the record is supporting
evidence and not a legal determination. A meeting without a consent record
downloads as before. The email link carries the summary only.

## Flows by capture method

The consent record, its rules and its API are shared by every way a meeting
reaches LMA. Each capture method decides how the notice is posted and how
replies are read.

| Capture method | Notice | Replies | Status |
|---|---|---|---|
| **Virtual Participant** | Posted in the meeting chat when the Virtual Participant is admitted, and again when somebody joins | Read from the meeting chat by display name, and from what is said, under the name of the participant shown speaking; an attestation or an objection made in the meeting page's Consent section (or through the API); screenshots of the chat and the participant list kept as evidence | Available. See [Virtual Participant](#virtual-participant) for what each platform supports. |
| **Stream Audio** | Shown to the LMA user, with a copy button, to read out or post in their meeting | Attested by the LMA user, who owns the meeting (opt-in is met by the rule **The meeting owner attests**); an objection is reported with **Participant objected** | Available. See [Stream Audio and the Desktop Capture App](#stream-audio-and-the-desktop-capture-app). |
| **Desktop Capture App** (macOS and Windows) | As for Stream Audio, with a copy button | As for Stream Audio; evidence is a frame of the screen-video source selected in the app | Available in builds that include it. Earlier builds show no consent step and are treated as [clients without a consent step](#clients-without-a-consent-step). See [The Desktop Capture App](#the-desktop-capture-app). |
| **Browser extension** | As for Stream Audio | As for Stream Audio | Not available yet; treated as a client without a consent step. |
| **Upload Audio** | None: the recording already exists | — | Not applicable. A recording should only be uploaded with the consent its participants gave when it was made. |

Until a capture method runs the flow, its meetings have no consent record. A
streaming client's are captured or refused as the setting **Streaming clients
without a consent step** says. In a deployment whose mode is
opt-in, `getMeetingConsent` reports a meeting with no record as not allowed.

A capture client follows the same steps whichever method it is:

1. **Create the meeting** as it does today (the meeting record must exist), and
   in an opt-in meeting send no transcript or recording yet.
2. **Start consent** with `startMeetingConsent`, naming the meeting's mode and
   rule only when they differ from the deployment's. The answer holds the
   notice text to post, its placeholders filled.
3. **Post the notice** where participants will see it, and record that it was
   posted with `recordMeetingConsentNotice`.
4. **Keep the participant list current** with `setMeetingConsentParticipants`
   as people join and leave, leaving out LMA's own participant.
5. **Record each reply** that reads as consent or an objection (see
   [How a reply is read](#how-a-reply-is-read)) with
   `recordMeetingConsentEvent`, and any attestation the owner makes with
   `attestMeetingConsent`.
6. **Gate capture on `CaptureAllowed`** in every answer: start sending the
   transcript and recording when it becomes true, and stop when it becomes
   false.
7. **Keep evidence** where it can: store a screenshot under the meeting's
   evidence folder, record it with `addMeetingConsentEvidence`, and check it
   with `verifyMeetingConsentEvidence`.

## Virtual Participant

The Virtual Participant (VP) runs the consent flow as a capture client,
following the steps above. When the deployment's mode is **Off** and the
meeting chose nothing else, it does nothing new: it reads the mode before it
joins, finds it off, and joins, records and transcribes exactly as it always
has, without calling the consent API. The one exception is a VP that cannot
read its own row before joining, where a per-meeting choice would be stored:
it then withholds capture until a later read succeeds (see
[Choosing the mode for one meeting](#choosing-the-mode-for-one-meeting)). In every other mode it posts
the notice in the meeting chat once it is admitted, records that it did, keeps
the participant list on the record current, reads replies typed in the chat
and listens for replies said aloud (see [Spoken replies](#spoken-replies)), and
acts on `CaptureAllowed` in every answer the API gives.

What "capture" means here is everything the VP takes from the meeting: the
transcript, the audio recording, the meeting chat it adds to the transcript
(see [Meeting Chat in the Transcript](virtual-participant.md#meeting-chat-in-the-transcript)),
the voice assistant's input, the key frames and the screen recording. The VP gates them
with the same switch the `LMA pause` and `LMA start` chat commands use, so
starting after consent is the same path as resuming after a pause.

### Choosing the mode for one meeting

The **Create Virtual Participant** form has a **Meeting consent** choice: the
deployment's setting (the default), Off, Notify, Opt-in with each participant
consenting, or Opt-in with the meeting owner attesting. The each-participant
choice is not offered for Webex, and for Teams it carries a note that on the
Teams browser join participants consent aloud (see
[What each platform supports](#what-each-platform-supports)). Any choice other
than the deployment's is stored on the VP's row (`consentMode` and `consentRule` in
`createVirtualParticipant`) and read by the VP when it starts, so scheduled
meetings keep it too. Whether the deployment allows the choice is decided as for
any capture client: with **Allow a different choice per meeting** off, a less
strict choice is not used. When the form opens it reads the deployment's mode,
rule and that setting through `getMeetingConsentDefaults` (see [API](#api)), and
with the setting off it shows each less strict choice disabled, with a
description that names the mode the meeting would use instead (for example "Not
allowed in this deployment: the meeting would use Notify"). The deployment's
setting and every choice at least as strict as it stay available, and the
deployment's setting is described with the mode it stands for. A choice already
selected, or stored on a scheduled VP being edited, that becomes disabled goes
back to the deployment's setting. When the form cannot read the settings it
offers every choice, as the platform allows, and does not stop the VP from
being created. The form only shows the rule; the VP applies it itself before joining, so it
knows whether to withhold capture, and the resolver applies it again when the VP
starts the meeting's consent; when the resolver refuses the meeting's choice,
the VP runs the deployment's mode instead. A meeting that chose Off in a
deployment whose mode is not Off is captured as usual, and the VP still starts
its consent, in mode off, once the meeting exists, so that the record shows
consent was not required for it and the resolver's check applies to it too. The
meeting assistant's MCP tools that start and schedule a VP do not offer the
choice yet; a VP they start uses the deployment's setting.

The VP reads the Meeting Consent section with no cache when it starts. If it
cannot read it, and the meeting did not ask for opt-in, it does not assume Off:
it withholds capture, as for opt-in, until the consent API tells it the
meeting's mode, and then behaves as that mode says. A Virtual Participant stack
deployed on its own without its `ConfigurationTableName` parameter has no
settings to read, so it behaves this way in every meeting; LMA's main stack
always passes the parameter. In the same way, if it
cannot read the meeting's own choice from its row (it tries three times before
joining), it withholds capture and does not start the meeting's consent until a
later read succeeds, because the choice it could not read may have been
opt-in. While the meeting runs a consent flow, `LMA help` also gives the
consent replies, in the same form as the notice's reply line: *"Meeting
consent: type or say "LMA, I consent" to agree to recording and transcription,
or "LMA, I object" to object."*, with *"type in this chat"* instead of *"type
or say"* where spoken replies are not read, and *"say"* on the Teams browser
join.

### How the Virtual Participant asks for replies

Every notice the VP posts is followed by one line that says how to reply, in
one form for the chat and for speech: `LMA`, a comma, and the deployment's
first consent or objection phrase. With the shipped phrases the line is:

- *"Type or say "LMA, I consent" or "LMA, I object"."* where spoken replies
  are read (the default);
- *"Type "LMA, I consent" or "LMA, I object" in this chat. An objection made
  out loud takes effect when <LMA user> records it in LMA."* where **Read
  spoken replies** is off;
- *"Say "LMA, I consent" or "LMA, I object"."* on the Teams browser join, which
  shows no chat senders, so a typed consent cannot be attributed there (see
  [Who is present](#who-is-present)); with **Read spoken replies** off it gets
  the chat line above.

The line is the same in notify and opt-in meetings. Every other VP message that
says how to reply uses the same form: the acknowledgement of an objection (see
[Objections](#objections)), the answer to a consent whose sender cannot be
told, the detail line on the Teams browser join, and `LMA help`. The form is a
suggestion, not a requirement: a bare *"I consent"* typed in the chat is still
consent (see [Replies in the chat](#replies-in-the-chat)).

### Notify

The VP records and transcribes from the start, as it does without consent, and
posts the notice in the chat as soon as it is in the meeting. Once the notice is
recorded as posted and nobody has objected, the status is `granted`, and the VP
keeps screenshots as evidence (see [Evidence screenshots](#evidence-screenshots)).
When somebody joins later, the notice is posted again, at most once a minute.

### Opt-in

An opt-in meeting starts with capture withheld: the VP joins, but writes no
transcript and no recording, does not start the voice assistant or the screen
recording, and adds no chat messages to the transcript; messages sent while
capture is withheld are not added later either. Unless **Read spoken replies** is off, it does send the meeting audio
to the meeting's transcription engine, on a session of its own, but only to
hear consent replies and objections said aloud: everything else it hears is
discarded inside the VP (see [Spoken replies](#spoken-replies)). Because nothing
is transcribed, the VP sends the meeting's `START` event itself, which creates
the meeting record its consent needs, and then posts the consent request. The VP's
detail line on the Virtual Participants page reads **Waiting for consent: …**
with what it is waiting for, and an `LMA start` typed in the chat is answered
with *"Live Meeting Assistant cannot start until consent is given."* When the
record says the meeting may be captured, the VP posts that consent was given and
the usual start message, starts transcription and recording, keeps evidence
screenshots, and clears the detail line. If somebody typed `LMA pause` while the
VP was waiting, it says that consent was given but stays paused until
`LMA start`.

- **Each participant consents.** Every participant listed as present must reply
  with consent, in the chat or aloud (see [Replies in the chat](#replies-in-the-chat)
  and [Spoken replies](#spoken-replies)).
  Each consent is acknowledged with who is still awaited, for example *"Thank
  you, Bo Chen. Still waiting for Cy Diaz and 1 unidentified participant."*
  When somebody joins while the meeting is being recorded, the record lists one
  more participant who has not consented, so **recording and transcription
  pause** until they do; the notice is posted again with a line saying so, and
  the detail line shows what is awaited. This is the record's own rule: a
  participant who joins later and has not replied withholds capture again. Who
  counts as present is described in [Who is present](#who-is-present).
- **The meeting owner attests.** The request is followed by *"Recording starts
  when <LMA user> confirms in LMA that everyone has agreed."* The owner confirms
  with **Attest that everyone agreed** in the meeting page's Consent section
  (see [On the meeting page](#on-the-meeting-page)), or with
  `attestMeetingConsent` through the API. The VP reads the record every 10
  seconds, so an attestation reaches it within about that long. Replies are
  still read and recorded as evidence, but no reply is taken as the owner's
  attestation: the chat and the speaker indicator show a display name, which
  cannot be matched reliably to the LMA user who owns the meeting.

### Objections

In every mode an objection, typed in the chat or said aloud, stops capture at
once, before anything is written: the VP turns the transcript and recording off
and stops the screen recording, then posts *"<name> objected. Live Meeting
Assistant has stopped recording and transcribing this meeting."*, records the
objection (the sender's or speaker's display name, the words, `chat` or `voice`
as the source, and the time) and keeps evidence screenshots. The detail line
reads **Not recording: a participant objected to recording.** A spoken
objection's own words were said while the meeting was being captured, so they
are the last line of its transcript. An objection said aloud and also typed in
the chat, by the same participant or with either one unnamed, within 30 seconds
and while capture is still stopped by the first, is acted on and recorded once.

- With **Virtual Participant leaves after an objection** off (the default) the
  VP stays, and listens for spoken replies while capture is stopped. Each
  participant's latest reply is the one that counts, so if the objector later
  replies with consent, in the chat or aloud, capture resumes, as the
  acknowledgement says. Its last sentence gives the reply in the form of the
  notice's reply line, with the deployment's first consent phrase: *"If <name>
  types or says "LMA, I consent", it can continue."* where spoken replies are
  read; *"If <name> says "LMA, I consent", it can continue."* on the Teams
  browser join, which shows no chat senders; and *"If <name> types "LMA, I
  consent" in this chat, it can continue."* where spoken replies are not read.
  An objection with no sender gets no such sentence, because a consent needs a
  sender. The transcript and the audio recording resume; the screen recording does
  not. It stops for the rest of the meeting the first time capture is withheld
  after it started, and what it recorded until then is kept on the meeting as
  usual.
- With it on, the VP also posts its usual goodbye and leaves. Its status ends
  `COMPLETED` with *"Left after <name> objected to recording."* and the end
  trigger `consent-objection`.

An objection recorded somewhere else, by a signed-in user with **Record an
objection** in the meeting page's Consent section or through the API
(`recordMeetingConsentEvent`), reaches the VP at its next read of the record.
It stops capture in the same way and posts *"An objection for <name> was
recorded in LMA. …"*.

### Replies in the chat

The VP compares a chat message with the phrases as it compares its chat
commands: case, punctuation and spacing are ignored, phrases match only as
whole words, and an `LMA` addressee may stand anywhere in the message. A
consent is either the whole message or `LMA` followed at once by the phrase;
an objection may be part of any sentence, because reading a message as an
objection can only stop capture, never start it.

- **Consent** is a message that is one of the consent phrases and nothing else
  (*"I consent"*, *"i agree!"*, *"I accept, LMA"*), or a message of up to 200
  characters in which `LMA` is followed at once by a consent phrase or by
  `consent`, anywhere in it: *"…so that's the plan. LMA, I consent."*, *"Sounds
  good, LMA I agree, let's go on"*, *"LMA, I agree with the plan"*. Punctuation
  and spacing between `LMA` and the phrase do not matter. A consent phrase
  without `LMA` just before it is not consent: *"I agree with Bo's point"* is
  ordinary chat, and so is *"LMA, what do you think? I consent"*, where other
  words stand between them. A bare *"I consent"* is still consent, so a
  participant who does not use the `LMA` form is still heard.
- **An objection** is a message that is one of the objection phrases (*"I
  object"*, *"Stop recording."*), `LMA no consent` or `LMA object`, or a message
  of up to 200 characters that contains an objection phrase:
  - addressed to `LMA`: *"LMA, please stop recording now"*, except an `LMA ask`
    question for the meeting assistant, which is answered and is not a reply
    whatever it mentions;
  - not addressed: *"I object to this meeting being recorded"*, *"I agree with
    Sam, but I don't consent to being recorded"*, and also *"I don't agree with
    the roadmap"*, which with the default phrases is handled as any objection
    is (see [Objections](#objections)): capture stops until the sender consents
    again, the screen recording stays off for the rest of the meeting, and with
    **Virtual Participant leaves after an objection** on, the VP leaves. The
    default phrases `I do not agree`, `I don't agree`, `do not record` and
    `don't record` match everyday sentences (*"please do not record the demo
    twice"*); remove them from the objection phrases where that is too eager
    for a deployment's meetings.
- Only the text outside quotation marks (double or single) is searched for an
  objection phrase or for `LMA` and a consent phrase, so a message that quotes
  one to tell people how to reply is not a reply (*'Reply "I consent" to agree,
  or "I object" to decline.'*, *'Type or say "LMA, I consent" or "LMA, I
  object".'*), and the shipped notices and reply lines, posted by another LMA in
  the same meeting or pasted from Stream Audio, are not read as replies. A custom notice text should keep
  `{objection_phrase}` in quotation marks for the same reason. A phrase outside the quotation marks still
  counts (*'LMA, "stop recording" please'* does not, *'Bo said "stop
  recording", and I agree: stop recording'* does), and the phrase typed on its
  own is an objection, quoted or not, as *'"LMA, I consent"'* is consent.
- A message longer than 200 characters is searched for no phrase, and an `LMA
  ask` question is searched for neither an objection nor a consent.
- Objections are looked for first, so *"LMA I do not consent"* is an objection,
  and so is a message that contains both an objection and *"LMA, I consent"*.
- Anything else is ordinary chat.

This differs from the reading in [How a reply is read](#how-a-reply-is-read),
which looks for a consent phrase anywhere in a reply; the VP does not use that
reading for the chat or for speech. `LMA consent`, `LMA no consent` and `LMA object` work whatever
phrases are configured, so they are the forms to suggest when a deployment's
phrases are long or in another language.

A consent reply needs a sender. One whose sender the platform does not show is
not recorded, and under the each-participant rule it is answered with *"Live
Meeting Assistant could not tell who sent that reply, so it was not recorded as
consent."*, followed on the Teams browser join, which shows no chat senders, by
*"Say "LMA, I consent" aloud instead."* (with the deployment's first consent phrase), where spoken replies are read.
An objection without a sender still stops capture, and is recorded
for *Unidentified participant*. A sender whose display name reads as one of the
list's placeholders (*Unidentified participant 1*) is not counted as present
and their consent is not recorded, so a name cannot fill the seat it reads as. A reply that a chat
observer reports twice within five seconds is acted on once, and the VP never
reads its own messages as replies.

### Spoken replies

With **Read spoken replies** on (the shipped default), the VP also reads
replies said aloud, on every join and with either transcription engine, and
every notice it posts is followed by *"Type or say "LMA, I consent" or "LMA, I
object"."* (with the deployment's first consent and objection phrases; see
[How the Virtual Participant asks for replies](#how-the-virtual-participant-asks-for-replies)).
With it off, the VP reads the chat only, as it did before spoken replies, sends
no meeting audio anywhere while consent is awaited, does not read spoken
objections from the transcription while capture runs, and follows every notice
instead with *"Type "LMA, I consent" or "LMA, I object" in this chat. An
objection made out loud takes effect when <LMA user> records it in LMA."*,
which **Record an objection** in the Consent section does. The VP reads the setting before it joins; if it
cannot read the Meeting Consent section, it reads the chat only.

**What counts as a spoken reply.** Only an utterance addressed to LMA is a
reply, because people say *"I agree"* and *"I don't agree"* in conversation far
more often than they type them, so an unaddressed sentence that contains an
objection phrase, which is an objection in the chat, is ordinary speech.
Addressed, it is read as an
addressed chat message is (see [Replies in the chat](#replies-in-the-chat)),
with the deployment's phrases, the whole utterance as the speech engine
finalises it being the reply:

- **Consent** is `LMA consent`, `LMA` with one of the consent phrases and
  nothing else (*"I agree, LMA"*), or `LMA` followed at once by a consent
  phrase anywhere in an utterance of up to 200 characters (*"LMA, I
  consent."*, *"OK, so that's the plan. LMA, I consent."*). *"LMA, what do you
  think? I consent"* is not consent.
- **An objection** is `LMA object` or `LMA no consent`, `LMA` with one of the
  objection phrases, or an utterance of up to 200 characters addressed to `LMA`
  that contains an objection phrase anywhere (*"LMA, please stop recording
  now"*). Objections are looked for first.
- A bare phrase is not a reply when said aloud: *"I consent."* and *"Stop
  recording!"* are ordinary speech, although the same words typed in the chat
  are replies, as is a typed sentence containing an objection phrase.

Speech engines often write `LMA` as its letters, so *"L M A"* and *"L.M.A."*
are read as `LMA`. Only final results are read, never the engine's partial
results.

**Who said it.** A spoken reply is attributed to the participant the VP shows
as speaking during it, the same name the transcript gives that audio: on Amazon
Transcribe, the active speaker the platform showed for most of the utterance;
on the MicroVM engine, the attendee the row is named after, without the
engine's voice label (`spk_0`), which is not a name. A named speaker also
counts as seen for [Who is present](#who-is-present). A reply whose speaker the
VP cannot name (nobody was shown speaking yet, or the platform shows a
placeholder) is handled as a chat reply without a sender: a consent is not
recorded, and under the each-participant rule the chat says *"Live Meeting
Assistant heard consent said aloud but could not tell who said it, so it was
not recorded."*; an objection still stops capture and is recorded for
*Unidentified participant*. Each reply is recorded with `voice` as its source.
The VP's own voice assistant is never read: it is not in the meeting audio the
VP listens to, it has its own channel or session on both engines, and a reply
attributed to one of the VP's own names is ignored.

**While capture runs** (Notify, or Opt-in after consent), the VP reads the
final participant results of the meeting's ongoing transcription, and acts on a
spoken objection exactly as on one in the chat.

**While consent is awaited** (Opt-in before consent, after an objection, or
after somebody joins under the each-participant rule), nothing is captured, so
the VP opens a separate session to the meeting's transcription engine, Amazon
Transcribe or the MicroVM engine, chosen as transcription chooses it, and
streams the meeting audio to it from the moment it is admitted. With Amazon
Transcribe this is its own streaming request, with the meeting's language
settings; with the MicroVM engine it is its own session, on the MicroVM the
meeting's transcription uses when one is running, or else on one launched for
the meeting and then kept for its transcription once consent is given. If no
MicroVM can be started, Amazon Transcribe is used, as for transcription. Every
final result is attributed and read as above, and then discarded inside the VP:

- what it heard is **not** written to Kinesis, so it never reaches the
  transcript, the summary or anything built from the transcript;
- it is not written to the audio recording, which records silence while
  capture is withheld, and it does not reach the voice assistant;
- it is not logged, at any log level, including `DEBUG`;
- only a reply is kept, as an event on the consent record, with the words that
  were read as the reply.

When consent is given, the listening session is closed before capture starts,
and transcription opens sessions of its own, so no audio heard while consent was
awaited reaches the transcript.

**What this means for processing.** While consent is awaited the meeting audio
is processed by the transcription engine, although nothing is kept apart from
the reply events. With Amazon Transcribe, that audio is sent to the Amazon
Transcribe streaming service in this deployment's Region, as the meeting's
transcription would be, and it is billed and appears in the meeting's
[cost estimate](meeting-cost-estimate.md) as Amazon Transcribe streaming. With
the MicroVM engine it stays on the MicroVM, which runs, and is billed, from the
time the VP is admitted rather than from when consent is given. Choose the
mode with this in mind, and turn **Read spoken replies** off where no meeting audio may leave the Virtual Participant before consent.

### Who is present

The participant list the VP keeps on the record decides the each-participant
rule, so it is never allowed to be shorter than the meeting:

- **Where the platform gives a list of names** that accounts for everybody the
  attendee count shows (the Teams join through Azure Communication Services),
  the list is those names.
- **Where it gives only a count** (the Chime, Zoom, Teams and Google Meet
  browser joins, and the Zoom Meeting SDK join), the list is the names the VP
  has seen since the count last went up, as the active speaker, as the speaker
  of a spoken reply, or as a chat sender, plus one
  *Unidentified participant N* for each seat no name accounts for. An
  unidentified participant can never consent, so the meeting is not granted
  until everybody present has spoken or written in the chat, and consented.
  When the count goes up, the names seen before stop counting, because one of
  them may have left in the meantime: everybody present has to be seen again
  (speaking is enough; their consent is already on the record). When it goes
  down while consent is still awaited, the same applies. While consent is
  granted it does not, because a departure cannot add anybody who has not
  consented.
- **Where it gives neither** (the Webex browser join), the names seen can never
  be shown to be everybody, so one unidentified participant is always listed
  and the each-participant rule is never met, whether replies are typed or
  said. The detail line says so. Use the owner-attests rule on Webex.
- **Where the platform shows no chat sender** (the Teams browser join), a
  consent typed in the chat cannot be attributed, but one said aloud
  (*"LMA, I consent"*) can, under the name the platform shows speaking. So the
  each-participant rule is met when every participant present has said their
  consent while the VP could see who was speaking, and the detail line adds that
  on this platform consent is given aloud. With **Read spoken replies** off it
  is never met there, and the detail line says to choose the owner-attests rule
  or turn the setting on.

The count is the one each platform handler already reads to leave a meeting it
is alone in: whenever the count on the page changes on the Chime and Zoom
browser joins (plus a check every 30 seconds on Zoom), every 3 seconds on the
two SDK joins, every 20 seconds by default on the Teams browser join (the
Virtual Participant section's attendee poll interval), and every 20 seconds on
the Google Meet join. One person leaving and
another joining between two readings leaves the count unchanged, which the VP
cannot see; the window is as long as the interval between readings.

The names are display names, as everywhere in the record. The VP's own
identities are never listed.

### What each platform supports

| Join | Chat sender | Spoken replies | Who is present | Each participant consents | Participant list screenshot |
|---|---|---|---|---|---|
| Chime | Shown | Read, under the active speaker | Count | Yes | The chat and the attendee list are on screen together, so one screenshot has both |
| Zoom (browser) | Shown | Read, under the active speaker | Count | Yes | The participants pane is opened, shot and closed again |
| Zoom (Meeting SDK) | Shown | Read, under the active speaker | Count | Yes | One screenshot of LMA's SDK page |
| Teams (browser) | **Not shown** | Read, under the active speaker | Count | Yes, **aloud only** ("LMA, I consent"): a typed consent cannot be attributed, so not with **Read spoken replies** off | The People pane is opened and shot, then the chat reopened |
| Teams (Azure Communication Services) | Shown | Read, under the active speaker | Names | Yes | One screenshot of LMA's SDK page |
| Webex (browser) | Shown | Read, under the active speaker | **None** | **No**: see [Who is present](#who-is-present) | The participants panel is opened, shot and closed again |
| Google Meet (browser) | Shown (read from the message group's header) | Read, under the speaking tile | Count | Yes | The People panel is opened and shot, then the chat reopened |

Notify, owner-attests and objections, typed or said, work on every join.
Opening and closing panels is best effort: a button the VP cannot find means a
chat screenshot without a participant list, never a held-up meeting. The panel
buttons, the participant-name reading, and spoken replies on every join were
written against each platform's current page and have not all been exercised
in a live meeting yet. On Google Meet in particular, neither the chat sender
reading nor the speaking-tile reading has been confirmed in a live meeting (see
[Google Meet](virtual-participant.md#google-meet) in the Virtual Participant guide), so a
reply there could be recorded under the wrong name until they are; use the
owner-attests rule on Meet where that matters.

### Evidence screenshots

When the meeting's status becomes `granted` (in notify, once the notice is
recorded as posted), and on every objection, the VP takes a screenshot of the
meeting page with the chat open, then, where the platform allows it, opens the
participant list, takes a second one, and puts the chat back. The screenshots
are taken from the meeting page itself, so nothing open in the live view covers
them. Each is a JPEG stored in the recordings bucket under
`lma-consent-evidence/<meeting>/` and added to the record with
`addMeetingConsentEvidence`, with a note of what it shows and the event it was
taken for: *"Meeting chat after the notice was posted"* (notify) or *"… when
consent was granted"* (opt-in) when the status becomes `granted` with no objection on the record;
*"Meeting chat when Bo Chen objected"*; *"… after an objection for Bo Chen was
recorded in LMA"*; and, when capture resumes after an objection, *"Meeting chat
when Bo Chen consented"*, naming the latest participant to consent after the
latest objection on the record. For each chat screenshot the VP then
asks for the vision-model check (`verifyMeetingConsentEvidence`) without
waiting for it; a participant list on its own is not checked, because the check
reads a chat for the notice and the replies. Nor are the screenshots taken for
an objection that was not made in the chat: one recorded in LMA (**Record an
objection**, or `recordMeetingConsentEvent` through the API) or one said aloud.
Such an objection never appears in the chat as a participant's reply, and LMA's
own announcement of it is not read as one, so a check of that chat could only
report what else the chat shows, such as an earlier consent. With **Keep
evidence screenshots** on, those images are kept and listed like the others,
and the owner or an Admin can still check them with **Check with model**.

With **Keep evidence screenshots** off, the VP stores only the screenshots it
asks to have checked: the chat screenshot (or, where the chat and the
participant list are on screen together, the one screenshot of both) after the
notice, when consent is granted or
resumes, and when an objection is typed in the chat. It stores no participant
list screenshot, and nothing for an objection recorded in LMA or said aloud,
because each image is deleted once its check is stored and these would never
be checked. Each stored image is deleted after its automatic check, and the
entry keeps the check, so the meeting page shows it as deleted after its check
and offers no **Check with model**. A manual check started on the meeting page
just before the automatic check deleted the image fails, and the entry then
shows the same deleted state. The VP reads the setting from the consent API
when consent starts; if it cannot, it treats the setting as off. An image whose
automatic check fails, or that was uploaded but could not be added to the
record, stays until the meeting is deleted or the lifecycle rule removes it.
Neither the screenshots nor the
check ever hold up the meeting: a failure is logged and the meeting carries on.
A VP keeps at most 24 images per meeting, below the record's 30, and takes no
more after that. Before leaving after an objection it waits up to 30 seconds for
that objection's screenshots.

### Not done yet

- The notice is posted in the chat only, not spoken by the voice assistant, so
  a participant who cannot see the chat hears no request to reply.
- [Spoken replies](#spoken-replies) have not been exercised in a live meeting
  on any join or with either engine yet. How reliably a short reply is heard,
  and how often it is attributed to the right speaker, depends on each
  platform's speaker indicator and has not been measured.
- Spoken replies are not read while the transcript is paused with `LMA pause`
  (nothing is sent to the engine then); replies in the chat still are.
- The screen recording does not resume once capture has been withheld after it
  started (see [Objections](#objections)).

## Stream Audio and the Desktop Capture App

Stream Audio sends a meeting's audio to the WebSocket transcriber, which
creates the meeting. Neither the page nor the user's browser can see the
meeting's participants or its chat, so the consent step is the LMA user's own:
they share the notice, and confirm that it was shared or, in opt-in, that
everyone agreed. The transcriber records that confirmation on the meeting, as a
backend role, and is the component that decides whether audio is transcribed
and recorded. The Desktop Capture App uses the same protocol, described in
[the WebSocket streaming API](websocket-streaming-api.md#615-meeting-consent-frames),
and the same rules; [The Desktop Capture App](#the-desktop-capture-app) below
says where it differs.

### The consent step

When the page opens its connection to the transcriber it asks for the consent
settings in force (a `CONSENT_SETTINGS` frame). With the mode Off, or a
transcriber that does not answer, the page shows no step and streams as before.
Otherwise a **Meeting consent** panel appears above the Start button with:

- **The notice** for the mode in force (the consent request in opt-in), its
  placeholders filled with the meeting owner field and the meeting topic, and a
  **Copy notice** button for pasting it into the meeting's chat.
- **The confirmation**: in opt-in, *Everyone in the meeting agreed to it being
  recorded and transcribed*; in notify, *I have told everyone in the meeting,
  with the notice above*. With it, **how**: out loud, in the meeting chat, or in
  the invitation's terms, and an optional note.
- **Capture evidence** (optional): the browser asks which tab or window to
  share; one frame of it is kept as a JPEG and the capture stops at once. While
  streaming, the button takes a frame of the tab being streamed, with no prompt.
- **Participant objected**, while streaming, with an optional name.

In **opt-in**, Start is refused until the confirmation is ticked and a method
chosen. The page sends its consent step with every `START`, as mode Off when it
has not read the settings yet (Start pressed straight after opening the page,
or the settings unavailable), so the transcriber refuses an opt-in meeting the
page did not ask about, rather than treating it as a client without a consent
step; the page then reads the settings again and shows the step. In **notify**, streaming may start without them; the notice is then not
recorded as posted, so the meeting's status stays `pending`, which in notify
still allows capture.

### What the transcriber records, and when

The confirmation travels in the `consent` field of the `START` frame. The
meeting does not exist yet at that moment: the transcriber puts the meeting's
start on the Kinesis stream, and the call event processor creates its row a
moment later. So the transcriber waits for the row, retrying for up to 30
seconds, reads the meeting's owner, and then, if it is the signed-in user who
streams, through the meeting consent API:

1. starts the meeting's consent in the deployment's mode, naming the rule
   **The meeting owner attests** for opt-in, with the meeting owner field and the
   topic for the notice's placeholders;
2. records the notice as posted, if the user confirmed, with the channel the
   method implies (out loud: `voice`; chat: `chat`; invitation: `invite`) and
   the text as the page showed it;
3. in opt-in, relays the attestation, which the record attributes to the
   meeting's owner (the signed-in user who streams), with the method and the
   note, or a sentence saying how everyone agreed when there is no note;
4. stores any evidence image under the meeting's evidence folder, adds it to
   the record, and asks for it to be checked with the vision model
   (`verifyMeetingConsentEvidence`). The check is supporting evidence and does
   not change the status (see
   [Checking evidence with a vision model](#checking-evidence-with-a-vision-model));
   if the check fails, the image is kept unchecked. A meeting may send at most
   five images, each a JPEG or PNG of at most 3.75 MB; an image that is refused
   or could not be stored does not count towards the five.

After each step the transcriber tells the page what the record says, and the
page shows it. The transcriber records the step after `START` rather than the
page recording it itself, because the page cannot know when the meeting's row
exists and only a backend role may add evidence.

### How capture is gated

- **Notify:** audio is transcribed and recorded from the start, as notify
  allows until somebody objects. If the consent record cannot be written, the
  meeting goes on and the page says so: `CONSENT_NOT_RECORDED` when the record
  could not be started, `NOTICE_NOT_RECORDED` when only the notice could not be
  recorded as posted.
- **Opt-in:** nothing is transcribed or recorded until the record answers that
  the meeting may be captured, usually a second or two after Start. Audio sent
  before then is discarded, not buffered. If the record cannot be written —
  the meeting was not created in time, the API refused the attestation, or the
  transcriber has no API configured — the meeting is ended without anything
  having been captured, and the page says why.
- **An objection:** **Participant objected** stops the page's capture, and the
  transcriber stops transcribing and recording at once, before the objection is
  written, ends the meeting as if the page had sent `END`, and records the
  objection (under the name given, or *A participant*, with source `voice`).
  What was captured before the objection is kept: an objection stops capture,
  it does not delete what was captured while capture was allowed. A meeting
  whose record answers that it may not be captured is ended the same way.
- **A refused meeting** (opt-in without the confirmation, or a deployment the
  page cannot satisfy) is not created at all: the transcriber answers with the
  reason and closes the connection.
- **Video:** a desktop video stream for a meeting is refused while the meeting's
  consent does not allow capture, and is finalized when an objection stops it.

An opt-in deployment whose rule is **Each participant consents** and that does
not **allow a different choice per meeting** cannot be satisfied from Stream
Audio or the Desktop Capture App, which have no participant list: the page or
the app says so and Start is refused.
Use a Virtual Participant for such meetings, or allow a per-meeting choice.

If the transcriber cannot read the consent settings when a meeting starts, it
records the page's step in the mode the page showed, and the meeting consent
resolver, which reads the settings itself, decides: in either mode nothing is
transcribed or recorded until the record answers that the meeting may be
captured, and if the record refuses the step the meeting is ended. A client
without a step is then captured, since nothing says that a step is needed.

### The Desktop Capture App

The macOS and Windows apps show the same step in their panel (see
[Meeting consent in the Desktop Capture App](desktop-capture-app.md#meeting-consent-notify-and-opt-in-deployments)),
and the transcriber records and gates it exactly as for Stream Audio. The
differences:

- **Reading the settings.** The app connects to the transcriber only when a
  recording starts, so it asks for the settings on a short-lived connection of
  its own when the user signs in, when the panel opens, and at Start if it has
  no answer yet. A transcriber that does not answer within six seconds is
  treated as one that predates the consent frames: no step is shown. The app
  still sends its step with `START`, as mode Off, as Stream Audio does, so in an
  opt-in deployment the transcriber refuses the meeting and the app reads the
  settings again rather than being treated as a client without a consent step.
- **Evidence** is one frame of the screen or window selected as the
  screen-video source in the app's Settings, taken with the capture permission
  the app already holds (on macOS, ScreenCaptureKit, macOS 14 or later). It can
  be taken before Start, when it is sent once the meeting starts, or while
  recording, when it is sent at once.
- **Reconnects.** The app sends a fresh `START`, with the same consent step, each
  time its connection drops and reconnects. The transcriber finds the meeting's
  record already started and does not record the notice or the attestation a
  second time; in an opt-in meeting the first second or so of audio after each
  reconnect is discarded until the record answers again. A meeting somebody
  objected to is not resumed: the transcriber refuses it and the app stops.
- **Screen video** of an opt-in meeting starts once the meeting's consent allows
  capture, since the transcriber refuses a video stream before that.
- **The command line** (`--cli`) prints the notice and takes the confirmation as
  `--consent-method verbal|chat|invite_terms`, with an optional `--consent-note`.

### Clients without a consent step

Clients that show no consent step — the browser extension, the Stream Audio
embed (`#/embed?component=stream-audio`), Desktop Capture App builds from
before the consent step, and scripts written against the streaming API — send
`START` without a
`consent` field. In Notify or Opt-in the transcriber treats such a meeting by
the setting **Streaming clients without a consent step**:

- **Accept with a warning** (the shipped default): the meeting is recorded and
  transcribed as before, with no consent record, and the transcriber logs a
  warning naming the meeting. This keeps existing desktop builds and
  integrations working while they are updated, but it means that **in an opt-in
  deployment such a meeting is captured without anybody having confirmed
  consent**. Its meeting has no consent record, and `getMeetingConsent` reports
  it as not allowed.
- **Refuse:** the meeting is not created, recorded or transcribed; the client is
  sent a `CONSENT_STATE` frame with the code `CONSENT_REQUIRED` and the
  connection is closed. A client without the consent step does not read that
  frame either, so the browser extension and the Stream Audio embed show only
  that the connection closed, and each reconnect is refused again. Choose this
  only once every client in use shows the consent step, and not while the
  browser extension or the Stream Audio embed is in use.

## What is stored

The consent record is the `Consent` attribute of the meeting's row in the
event-sourcing table, so it is kept, shared and deleted with the meeting. It
holds:

| Field | Meaning |
|---|---|
| `Mode`, `Rule` | The meeting's mode, and for opt-in its rule |
| `Status`, `CaptureAllowed` | As above, worked out on every write |
| `NoticeText` | The notice or request for this meeting, placeholders filled |
| `NoticePostedAt`, `Notices` | When the notice was first posted; each posting, with its channel (`chat`, `voice`, `banner` or `invite`), text, time and who recorded it. The first posting and the latest 9 are kept |
| `Participants`, `ParticipantsSeen` | Who is present now, as last listed, and everyone ever listed (the first 500) |
| `Events` | Every reply and attestation in the order recorded: the participant's display name, `consent` or `objection`, where it was read (`chat`, `voice`, `ui` for an entry a signed-in user made, or `attestation`), the words when sent, when it happened, when it was recorded and by whom |
| `Attestation` | Who attested, how (`verbal`, `chat` or `invite_terms`), what they said, and when |
| `Evidence` | The images kept: each one's id, kind (`screenshot` or `invitation`), where it is stored, when it was taken, a note, the vision model's check of it (`Verification`) when one ran, and when the image was deleted after its check (`ImageDeletedAt`) when screenshots are not kept |
| `StatusHistory` | Each change of status or of `CaptureAllowed`, with the time and reason; the latest 100 are kept |
| `StartedAt`, `StartedBy`, `UpdatedAt`, `Version` | When and by whom consent was started; when it last changed; a count that increases with every write |

A participant list of more than 250 distinct names is refused, so a meeting
larger than that cannot be granted under the **Each participant consents**
rule; use **The meeting owner attests** for it. A meeting keeps at most 30
evidence images. Events are compacted towards 300: past that, older events are
dropped, but never a participant's latest reply or the latest attestation, so
the status does not change by being compacted, and a meeting in which more than
300 names have replied keeps one event for each of them.

The record shares one database item, of at most 400 KB, with the rest of the
meeting row, so it is also held to 150 KB as a whole. A write that would take it
past that first sheds what the status does not depend on, in this order: replies
that are no longer anybody's latest, the words of replies, the older status
history, the notices between the first and the latest, the names no longer
present, what the evidence check read in each image, and finally each reply's
other details and each image's note and check summary. A write that still would not fit is
refused with a message saying so.

Evidence images are private objects in the recordings bucket, under
`lma-consent-evidence/<meeting>/`, where `<meeting>` is the CallId with every
character other than letters, digits, `_` and `.` replaced by `_` and any
leading and trailing `_` removed (the same rule as the key frames' folder).
Two CallIds that differ only in such characters, `a b` and `a_b` for example,
share a folder, as they do for key frames.
Only a backend role can add one, and only from that folder. `getMeetingConsent`
signs an hour-long link to each image for whoever may read the meeting.

Deleting a meeting deletes its consent record, which is part of the meeting
row, and every image in its evidence folder. A lifecycle rule on
`lma-consent-evidence/` also deletes the images after
`TranscriptionExpirationInDays`, as for the key frames. The record itself is
kept as long as the meeting record (`MeetingRecordExpirationInDays`), which is
usually longer, so a record can go on listing images that have expired: their
entries and anything stored about them remain, and a link to one leads nowhere.
If the images are needed for as long as the record, keep the two retentions
equal, or copy the images elsewhere before they expire.

## Checking evidence with a vision model

An evidence screenshot — typically the meeting chat after the request was
posted — can be checked by an Amazon Bedrock vision model with
`verifyMeetingConsentEvidence`. The model is shown the image with the notice
that was posted, the meeting's mode, the consent and objection phrases and the
participants listed as present, and asked to report what the picture shows:

- whether the notice is visible, and whether it says the same as the notice
  posted;
- every visible reply to it, with the sender's name as shown, the reply's text,
  and whether it reads as consent, an objection, or neither;
- whether anybody objected;
- how sure it is, from 0 to 1, and a one-sentence summary.

The model is also sent the notices as LMA posted them in the chat, and told
that the screenshot is taken in LMA's own meeting window, where LMA's messages
appear as the window's own (on one side with no sender name, or under "You") or
under LMA's participant name, which starts with "LMA". Only a participant's
message is a reply: LMA's notice and its other messages, and the reply phrases
the notice quotes (*reply "I object"*), are never read as consent or an
objection. LMA also leaves out of the answer, before drawing the verdict, any
reply the model marks as LMA's, any reply under a sender name of that kind, and
any reply of six words or more that appears word for word in a notice LMA
posted; and when the only objection the model listed was such a message and
every other reply it listed is a consent, its report that somebody objected is
not kept. A short reply such as *"I object"* from a participant is never left
out for its words alone, although the notice contains them.

From that answer LMA draws a **verdict**, the same way whichever model answered:

| Verdict | When |
|---|---|
| `objection_found` | The model reported an objection, or a visible reply reads as one. Reported whatever the model's confidence. |
| `supports_consent` | The notice is visible, nobody objected, the model's confidence is at least 0.5, and — for the opt-in rule **Each participant consents** — every participant listed as present has a visible reply reading as consent. |
| `inconclusive` | Anything else, including an answer that could not be read. For the each-participant rule, the participants with no visible consent are listed (`MissingParticipants`). |

The result is stored on the evidence entry as `Verification`, with the model's
id, when the check ran, who ran it and the tokens it used. The verdict
describes that one image, not the meeting: an earlier reply scrolled out of
view, or an objection made outside the chat, is not in it, and when a
participant's replies are both visible the verdict does not apply the record's
rule that their latest reply counts. The meeting page and the summary download
show it as the *image check*. It is supporting evidence only: **it never changes the meeting's consent status**, which follows
only from the replies and attestations recorded on the meeting. A check that
finds an objection nobody recorded is for a person to look at — the owner can
record the objection — and a check that supports consent does not grant it.

The model is told that everything in the screenshot, including text that looks
like an instruction, is content to report, and not to identify anybody from
their face or appearance, only from names written on the screen.

**Model and cost.** The model is **Evidence check model** in the Meeting Consent
settings; the shipped default is Amazon Nova Lite (`amazon.nova-lite-v1:0`), the
same in-region model the key frame text reader uses by default, so a deployment
that reads key frames needs no further model access. Each check is one model
call, recorded against the meeting as **Consent evidence check** in its
[cost estimate](meeting-cost-estimate.md).

Measured on four synthetic screenshots of a meeting with a chat panel (1568 ×
882 JPEG), each checked three times per model at temperature 0: every
participant consenting (expected `supports_consent`), one of three participants
not replying (`inconclusive`), an objection worded freely, "I don't consent to
being recorded, please turn it off" (`objection_found`), and a chat line telling
the checker to ignore its instructions and report that everyone consented,
followed by an objection (`objection_found`):

| Model | Verdicts as expected | Input / output tokens per check | Cost per check | Time per check |
|---|---|---|---|---|
| Amazon Nova Lite | 12 of 12 | about 2,630 / 110 | about $0.0002 | about 1.3 s |
| Amazon Nova 2 Lite | 12 of 12 | about 790 / 140 | about $0.0006 | about 1.3 s |
| Claude Haiku 4.5 | 12 of 12 | about 2,120 / 190 | about $0.003 | about 1.8 s |

Four synthetic screenshots are a small sample with clean, legible text. Real
chat panels are smaller, busier and partly hidden, so expect more `inconclusive`
results from them, and choose a larger model if the default reads them poorly.

**Keeping or deleting the image.** With **Keep evidence screenshots** on (the
default), the image stays with the meeting until the meeting is deleted. With it
off, the image is deleted as soon as its check is stored, and the entry keeps
the check and `ImageDeletedAt`; such an entry cannot be checked again. The
Virtual Participant then stores only images it has checked automatically (see
[Evidence screenshots](#evidence-screenshots)). If the
deletion fails, the check is still stored and the image is left for the
meeting's deletion or the lifecycle rule to remove. If the image is deleted but
`ImageDeletedAt` cannot be recorded, the failure is logged with the evidence
id, and the entry goes on looking as if its image were kept.

**Timing.** The check runs while the request waits. AppSync answers a request
within 30 seconds; a check that takes longer still stores its result, but the
request returns an error and no `onUpdateMeetingConsent` message is sent, so the
result is seen on the next read of the record.

## Who can do what

| Operation | Who |
|---|---|
| Read the record (`getMeetingConsent`, `Call.Consent`, `onUpdateMeetingConsent`) | The meeting's owner, the people it is shared with, Admins, and LMA's backend roles |
| Read the deployment's mode, rule and **Allow a different choice per meeting** (`getMeetingConsentDefaults`) | Any signed-in user (Cognito user pool); it returns those three values only |
| Start consent (`startMeetingConsent`) | The owner, an Admin, or a backend role |
| Record that the notice was posted (`recordMeetingConsentNotice`) | The owner, an Admin, or a backend role |
| List the participants present (`setMeetingConsentParticipants`) | The owner, an Admin, or a backend role |
| Record a reply (`recordMeetingConsentEvent`) | A backend role, for a reply read from the chat or the speech. A signed-in user who can read the record may record an **objection**; only the owner or an Admin may record **consent** on a participant's behalf. A signed-in user's entry is recorded as source `ui` under their own username; the meeting page's **Record an objection** makes one |
| Attest (`attestMeetingConsent`) | The meeting's owner, or a backend role relaying the owner's attestation and naming them |
| Add evidence (`addMeetingConsentEvidence`) | A backend role |
| Check evidence with the vision model (`verifyMeetingConsentEvidence`) | The owner, an Admin, or a backend role. Where evidence screenshots are not kept, the check is what deletes the image |

The WebSocket transcriber's task role is one of LMA's backend roles. It is
granted the six consent mutations it calls (`startMeetingConsent`,
`recordMeetingConsentNotice`, `recordMeetingConsentEvent`,
`attestMeetingConsent`, `addMeetingConsentEvidence` and
`verifyMeetingConsentEvidence`), and `getCall` to read a meeting's owner before
writing anything, and nothing else on the API. It acts only
for the meeting started on the connection a frame arrives on, and records a
consent step only on a meeting owned by the signed-in user who started it, so a
signed-in user can record consent, an objection or evidence only for their own
meeting that they are streaming. A `START` with a consent step that names an
existing meeting owned by somebody else is refused with `MEETING_NOT_OWNED`
before anything is written for it, and the client is told to use another
meeting name. If such a meeting is created while the step is being recorded,
this connection's capture of it is dropped as soon as its owner is read, with
nothing written to its consent record and no end event or recording written for
it.

These rules are applied by the meeting consent resolver on every request, as
the meeting's other edits are applied by theirs; a meeting the caller may not
see is refused in the same way as one that does not exist.

## API

All the operations are on the AppSync GraphQL API, with Cognito user pool or
IAM authorization. Every mutation answers with a `MeetingConsentUpdate`
(`CallId`, `Owner`, `SharedWith`, `Consent`), which is also what the
`onUpdateMeetingConsent(CallId)` subscription carries. A subscriber receives
the `Consent` fields the writer selected, so a writer should select the whole
record; the fragment below does. A subscriber that shows the evidence images
reads `getMeetingConsent` again on each message, because only that query signs
their links.

```graphql
fragment ConsentRecord on MeetingConsent {
  Mode Rule Status CaptureAllowed NoticeText NoticePostedAt
  Notices { Channel Text PostedAt PostedBy }
  Participants ParticipantsSeen
  Events { EventId ParticipantName Decision Source Text OccurredAt RecordedAt RecordedBy }
  Attestation { AttestedBy Method Statement AttestedAt RecordedBy }
  Evidence {
    EvidenceId Kind S3Key CapturedAt AddedAt Note SignedUrl ImageDeletedAt
    Verification {
      Verdict MissingParticipants Readable NoticeVisible NoticeMatches
      Replies { Name Text Decision }
      AnyObjection Confidence Summary ModelId VerifiedAt VerifiedBy InputTokens OutputTokens
    }
  }
  StatusHistory { Status CaptureAllowed At Reason }
  StartedAt StartedBy UpdatedAt Version
}

query {
  getMeetingConsent(CallId: "<CallId>") {
    CallId
    CaptureAllowed
    Consent { ...ConsentRecord }
    Settings {
      Mode Rule NotifyNoticeText OptInNoticeText ConsentPhrases ObjectionPhrases
      AllowPerMeetingOverride KeepEvidenceScreenshots VerifierModelId
    }
  }
}

# Before any meeting exists, as the Create Virtual Participant form reads it.
query {
  getMeetingConsentDefaults { Mode Rule AllowPerMeetingOverride }
}

mutation {
  startMeetingConsent(input: {
    CallId: "<CallId>", Mode: "opt_in", Rule: "each_participant",
    LmaUser: "Ana Silva", MeetingName: "Weekly sync"
  }) { CallId Owner SharedWith Consent { ...ConsentRecord } }
}

mutation {
  recordMeetingConsentNotice(input: { CallId: "<CallId>", Channel: "chat" }) {
    CallId Owner SharedWith Consent { ...ConsentRecord }
  }
}

mutation {
  setMeetingConsentParticipants(input: { CallId: "<CallId>", Participants: ["Ana Silva", "Bo Chen"] }) {
    CallId Owner SharedWith Consent { ...ConsentRecord }
  }
}

mutation {
  recordMeetingConsentEvent(input: {
    CallId: "<CallId>", ParticipantName: "Bo Chen", Decision: "consent",
    Source: "chat", Text: "I consent", OccurredAt: "2026-11-02T10:01:12.000Z"
  }) { CallId Owner SharedWith Consent { ...ConsentRecord } }
}

mutation {
  attestMeetingConsent(input: {
    CallId: "<CallId>", Method: "verbal", Statement: "Everyone agreed at the start."
  }) { CallId Owner SharedWith Consent { ...ConsentRecord } }
}

mutation {
  addMeetingConsentEvidence(input: {
    CallId: "<CallId>", S3Key: "lma-consent-evidence/<meeting>/chat-0001.jpg",
    Kind: "screenshot", CapturedAt: "2026-11-02T10:01:30.000Z", Note: "Chat panel after the request"
  }) { CallId Owner SharedWith Consent { ...ConsentRecord } }
}

mutation {
  verifyMeetingConsentEvidence(input: { CallId: "<CallId>", EvidenceId: "<EvidenceId>" }) {
    CallId Owner SharedWith Consent { ...ConsentRecord }
  }
}

subscription {
  onUpdateMeetingConsent(CallId: "<CallId>") {
    CallId Owner SharedWith Consent { Status CaptureAllowed Version }
  }
}
```

Arguments that are checked:

- `Mode` is `off`, `notify` or `opt_in`; `Rule` is `each_participant` or
  `owner_attests`, and is used only with `opt_in`. Omitted, each is the
  meeting's own once its consent has started, and the deployment's before
  that.
- Starting consent again with the mode and rule already in force, or naming
  neither, returns the record as it is, without a write: a capture client that
  restarts keeps the meeting's choice and its notice even if the deployment's
  settings have changed since. `NoticeText`, `LmaUser` and `MeetingName` are
  read only when consent starts or its mode changes.
- Starting it with a different mode or rule is refused once a notice, reply,
  attestation or evidence image has been recorded. Before that, the mode is
  changed and the change is added to the status history; the participants
  listed and when consent was first started are kept.
- Every other mutation is refused until consent has been started.
- A backend role's reply names its `Source`, `chat` or `voice`. An attestation
  a backend role relays is recorded as the meeting owner's; `AttestedBy`, when
  given, must name the owner.
- Names are cut at 100 characters, reply and statement texts at 500, notices
  at 2,000, and an evidence key may be at most 256 characters.

Each write reads the meeting row and writes the record back on condition that
nobody else wrote it in between, retrying a few times, so replies that arrive
together are all kept. A write that keeps meeting another one is refused with
a message to try again.

The rules that decide the status and the gate are in one Python module,
`lma_consent` in the configuration layer (`lma-ai-stack/source/lambda_layers/configuration_layer/lma_consent/`),
which the resolver uses and a Python capture client or script can import; it
also holds the reply classifier described above.

## Limitations

- **Display names are not verified identities.** Replies are matched to
  participants by the name the meeting platform shows. A participant can rename
  themselves, two people can show the same name, and a guest's name is whatever
  they typed. A reply is recorded under the name its sender showed when it was
  sent, and two participants showing one name are counted as one.
- **Speech is read as words, and attributed by the speaker indicator.** A
  spoken reply is only as reliable as its transcript; a reply in a language the
  transcription is not set up for may not be read at all. It is recorded under
  the name the platform showed speaking, which can be wrong when people speak
  over each other or the indicator lags, and is missing when nobody was shown
  speaking.
- **A model's reading is supporting evidence.** The evidence check reports
  what a vision model read in a screenshot. It can misread small or partly
  hidden text, and it never changes a meeting's status.
- **What LMA records is evidence, not a determination.** The record shows what
  was posted and what was replied, when; whether that amounts to consent where
  the meeting took place is for your organisation to decide.
- **A streamed meeting's consent is the user's word.** Stream Audio and the
  Desktop Capture App cannot see the participants or the meeting chat, so an
  opt-in meeting from either is granted on the LMA user's attestation alone. An evidence screenshot is the
  only part of the record that shows what the participants saw or replied.
- **Not every capture method runs the flow yet.** See
  [Flows by capture method](#flows-by-capture-method).
- **What the Virtual Participant can see differs by platform.** Who is present
  and who sent a chat message or is speaking come from the meeting page, and not every
  platform shows all three; see
  [What each platform supports](#what-each-platform-supports).
- **The record is meeting data.** It is kept and deleted with the meeting, and
  anyone the meeting is shared with can read it, including participants' names
  and replies.

## Related

- [Configuration Guide](configuration-guide.md) — the Meeting Consent section,
  and the [configuration presets](configuration-guide.md#configuration-presets)
  that set it
- [Transcript Deletion After the Summary](transcript-deletion.md)
- [Privacy and Data Handling Controls](privacy-and-data-handling.md) — the
  consent options together with retention and deletion
- [Virtual Participant](virtual-participant.md)
- [Stream Audio](stream-audio.md)
- [User-Based Access Control](user-based-access-control.md) — who can read a meeting
