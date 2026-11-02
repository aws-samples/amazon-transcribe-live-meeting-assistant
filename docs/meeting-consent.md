---
title: "Meeting Consent"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
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

**Current state:** the consent settings, the consent record and its API are in
place. No capture method runs the consent flow yet — the Virtual Participant,
the Desktop Capture App, Stream Audio and the browser extension neither post a
notice nor wait for consent — so choosing Notify or Opt-in on the Configuration
page does not yet change how any meeting is captured. See
[Flows by capture method](#flows-by-capture-method).

## The three modes

| Mode | What participants see | When the meeting is recorded and transcribed |
|---|---|---|
| **Off** | Nothing | Always. This is how LMA behaves when consent has never been set up, and the shipped default. |
| **Notify** | A notice that the meeting is recorded and transcribed, and how to object | From the start, until a participant objects. An objection stops recording and transcription, and is recorded. |
| **Opt-in** | A request to agree, with the phrase to reply | Not until the consent rule below is met. An objection from anybody stops it again. |

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

Each participant's **latest** reply is the one that counts: somebody who
objected and then consented has consented, and the other way round. Replies are
matched to participants by their display name in the meeting, compared without
regard to case or extra spacing.

## The Meeting Consent settings

An Admin sets the deployment's default in the **Meeting Consent** section of
**Configuration ▸ Configuration** (see the [Configuration Guide](configuration-guide.md)).
A saved change applies to meetings whose consent starts after the save; a
meeting already under way keeps the mode and rule it started with.

| Setting | Meaning | Shipped default |
|---|---|---|
| Consent mode | Off, Notify or Opt-in | Off |
| Consent rule (opt-in) | Each participant consents, or the meeting owner attests | Each participant consents |
| Notice text (notify) | The notice a notify meeting posts | "{LMA_USER} is using Live Meeting Assistant to record and transcribe this meeting ({meeting}). If you do not agree, reply "{objection_phrase}" in the chat or say so, and recording and transcription will stop." |
| Consent request text (opt-in) | The request an opt-in meeting posts | "{LMA_USER} would like to record and transcribe this meeting ({meeting}) with Live Meeting Assistant. Nothing is recorded or transcribed until everyone has agreed. Reply "{consent_phrase}" to agree, or "{objection_phrase}" to decline." |
| Consent phrases | Comma-separated phrases read as consent | I consent, I agree, I accept |
| Objection phrases | Comma-separated phrases read as an objection | I object, I do not consent, I don't consent, I do not agree, I don't agree, do not record, don't record, stop recording |
| Allow a different choice per meeting | Whether a meeting may be started with another mode, rule or notice | On |

In the two texts, `{LMA_USER}` is the person using LMA in the meeting,
`{meeting}` the meeting's name, and `{consent_phrase}` and `{objection_phrase}`
the first phrase of each list. Any other text in braces is posted as written.

With **Allow a different choice per meeting** off, a meeting may still be
started with a *stricter* choice than the default — opt-in in a notify
deployment, or each participant in an owner-attests deployment — but not a less
strict one, and not with its own notice text.

The settings are read when they are needed, with no cache, because they decide
whether a meeting may be captured. If they cannot be read, starting a meeting's
consent fails rather than falling back to off, and so does reading a meeting
that has no consent record; a meeting with a record is read without the
settings, since its record decides its own gate.

## Flows by capture method

The consent record, its rules and its API are shared by every way a meeting
reaches LMA. Each capture method decides how the notice is posted and how
replies are read.

| Capture method | Notice | Replies | Status |
|---|---|---|---|
| **Virtual Participant** | Posted in the meeting chat when the Virtual Participant is admitted | Read from the meeting chat, and from what is said, by display name; screenshots of the chat kept as evidence | Not available yet: the Virtual Participant does not run the consent flow. Planned next. |
| **Desktop Capture App** and **Stream Audio** | Shown to the LMA user to read out or post in their meeting | Attested by the meeting owner (the opt-in rule **The meeting owner attests**), or objections recorded by the LMA user | Not available yet: neither starts a meeting's consent. Planned next. |
| **Browser extension** | As for Stream Audio | As for Stream Audio | Not available yet. |
| **Upload Audio** | None: the recording already exists | — | Not applicable. A recording should only be uploaded with the consent its participants gave when it was made. |

Until a capture method runs the flow, its meetings have no consent record and
are captured as they are today, whatever the consent mode. In a deployment
whose mode is opt-in, `getMeetingConsent` reports such a meeting as not
allowed, but nothing acts on that answer yet.

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
   evidence folder and record it with `addMeetingConsentEvidence`.

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
| `Evidence` | The images kept: each one's id, kind (`screenshot` or `invitation`), where it is stored, when it was taken and a note |
| `StatusHistory` | Each change of status or of `CaptureAllowed`, with the time and reason; the latest 100 are kept |
| `StartedAt`, `StartedBy`, `UpdatedAt`, `Version` | When and by whom consent was started; when it last changed; a count that increases with every write |

A meeting keeps at most 250 participants present, 300 events and 30 evidence
images. When a long meeting reaches 300 events, older events are dropped, but
never a participant's latest reply or the latest attestation, so the status
does not change by being compacted.

The record shares one database item, of at most 400 KB, with the rest of the
meeting row, so it is also held to 150 KB as a whole. A write that would take it
past that first sheds what the status does not depend on, in this order: replies
that are no longer anybody's latest, the words of replies, the older status
history, the notices between the first and the latest, the names no longer
present, what a vision model read in each evidence image, and finally each
reply's other details and each image's note. A write that still would not fit is
refused with a message saying so.

Evidence images are private objects in the recordings bucket, under
`lma-consent-evidence/<meeting>/`, where `<meeting>` is the CallId with every
character other than letters, digits, `_` and `.` replaced by `_` and any
leading and trailing `_` removed (the same rule as the key frames' folder).
Only a backend role can add one, and only from that folder. `getMeetingConsent`
signs an hour-long link to each image for whoever may read the meeting.

Deleting a meeting deletes its consent record, which is part of the meeting
row, and every image in its evidence folder. A lifecycle rule on
`lma-consent-evidence/` also deletes the images after
`TranscriptionExpirationInDays`, as for the key frames.

## Who can do what

| Operation | Who |
|---|---|
| Read the record (`getMeetingConsent`, `Call.Consent`, `onUpdateMeetingConsent`) | The meeting's owner, the people it is shared with, Admins, and LMA's backend roles |
| Start consent (`startMeetingConsent`) | The owner, an Admin, or a backend role |
| Record that the notice was posted (`recordMeetingConsentNotice`) | The owner, an Admin, or a backend role |
| List the participants present (`setMeetingConsentParticipants`) | The owner, an Admin, or a backend role |
| Record a reply (`recordMeetingConsentEvent`) | A backend role, for a reply read from the chat or the speech. A signed-in user who can read the record may record an **objection**; only the owner or an Admin may record **consent** on a participant's behalf. A signed-in user's entry is recorded as source `ui` under their own username. (The meeting page has no control for this yet; it is planned with the page's Consent panel.) |
| Attest (`attestMeetingConsent`) | The meeting's owner, or a backend role relaying the owner's attestation and naming them |
| Add evidence (`addMeetingConsentEvidence`) | A backend role |

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
  Evidence { EvidenceId Kind S3Key CapturedAt AddedAt Note SignedUrl }
  StatusHistory { Status CaptureAllowed At Reason }
  StartedAt StartedBy UpdatedAt Version
}

query {
  getMeetingConsent(CallId: "<CallId>") {
    CallId
    CaptureAllowed
    Consent { ...ConsentRecord }
    Settings { Mode Rule NotifyNoticeText OptInNoticeText ConsentPhrases ObjectionPhrases AllowPerMeetingOverride }
  }
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
- A backend role's reply names its `Source`, `chat` or `voice`; an attestation
  by a backend role names `AttestedBy`.
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
- **Speech is read as words.** A spoken reply is only as reliable as its
  transcript; a reply in a language the transcription is not set up for may not
  be read at all.
- **What LMA records is evidence, not a determination.** The record shows what
  was posted and what was replied, when; whether that amounts to consent where
  the meeting took place is for your organisation to decide.
- **Not every capture method runs the flow yet.** See
  [Flows by capture method](#flows-by-capture-method).
- **The record is meeting data.** It is kept and deleted with the meeting, and
  anyone the meeting is shared with can read it, including participants' names
  and replies.

## Related

- [Configuration Guide](configuration-guide.md) — the Meeting Consent section
- [Virtual Participant](virtual-participant.md)
- [Stream Audio](stream-audio.md)
- [User-Based Access Control](user-based-access-control.md) — who can read a meeting
