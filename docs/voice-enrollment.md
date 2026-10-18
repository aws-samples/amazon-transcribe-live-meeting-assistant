---
title: "Voice Enrollment (Speaker Recognition)"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Voice Enrollment (Speaker Recognition)

> **Off by default, and off unless you deliberately turn it on.** A voiceprint is
> biometric data about a person. `EnableVoiceEnrollment` defaults to `false`, and
> while it is false the enrollment table, the consent log, the portal route and
> the matching code path do not exist in the deployment at all — not merely
> hidden, not deployed.
>
> Turning it on creates obligations that are yours, not this project's. Biometric
> data is separately regulated in many jurisdictions (Illinois BIPA, Texas CUBI,
> the EU GDPR's Article 9, and others), typically requiring informed written
> consent before collection, a published retention schedule, and deletion on
> request. This feature is built to make those obligations *possible* to meet —
> it captures consent explicitly and keeps an append-only record of it — but
> deciding that you may lawfully collect voiceprints from the people in your
> meetings is a decision for you and your legal counsel.

## Table of Contents

- [What it does](#what-it-does)
- [The consent model](#the-consent-model)
- [What is stored, and where](#what-is-stored-and-where)
- [The audit trail](#the-audit-trail)
- [Who can enrol](#who-can-enrol)
- [Enrolling a voice](#enrolling-a-voice)
- [How a match is used](#how-a-match-is-used)
  - [Which voices a meeting is given](#which-voices-a-meeting-is-given)
- [Withdrawing consent](#withdrawing-consent)
- [Retention](#retention)
- [Limits we hold deliberately](#limits-we-hold-deliberately)
- [Deploying it](#deploying-it)
- [Related Documentation](#related-documentation)

## What it does

[Speaker separation](transcription-and-translation.md#speaker-identification-within-a-channel)
can tell voices apart but not who they are, so a conference-room transcript reads
`Other Participant (spk_0)`. [Naming the voices](transcription-and-translation.md#naming-the-voices)
lets you fix that by hand, and
[finding the names automatically](transcription-and-translation.md#finding-the-names-automatically)
reads the transcript for introductions.

Both of those start from zero in every meeting. Voice enrollment lets somebody
record a short voice sample **once**, with their explicit consent, so that
afterwards they are labelled by name from their first word in every meeting —
including meetings where nobody introduced themselves.

## The consent model

The hard part of this feature is not the matching. It is that a voiceprint is
biometric data about a person, and letting anybody else assert "they agreed" on
their behalf would make the consent record worthless.

So consent is **first-party and self-service**. Nobody can enroll anybody else,
and there is no administrative action that creates a voiceprint:

1. An LMA user opens **Enrol My Voice** in the navigation (or goes straight to
   **`/#/voice-enrollment`**) and is shown the consent terms in full, including
   what is stored, what it is used for, how long it is kept and how to withdraw.
2. They consent by an explicit affirmative action — a checkbox they must tick
   plus a button they must press. There is no pre-ticked box and no implied
   consent from continuing.
3. Only then does the portal offer to record voice samples.

**Enrollment is limited to people who already have an account in the
deployment.** Somebody attending from another company cannot currently enroll,
because doing so would mean giving them an account here; see
[limits we hold deliberately](#limits-we-hold-deliberately).

Consent is recorded **before** any audio is captured, so a record exists even if
the person abandons the recording step. Declining is a normal outcome: the portal
is a dead end with nothing stored.

## What is stored, and where

Two separate tables, for two different reasons.

**`VoiceEnrollmentTable`** — the current state, and the only place biometric data
lives:

| Field | Purpose |
|---|---|
| `SubjectId` | The enrollee's Cognito `sub`, rather than an email, so the key carries no contact details. |
| `DisplayName` | The name to label transcripts with, as the enrollee typed it. |
| `Centroid` | The voiceprint: a vector of floats. **This is the biometric.** |
| `Status` | `pending`, `active` or `revoked`. |
| `SampleCount`, `EnrolledAt`, `ConsentExpiresAt` | Operational metadata. |

**`VoiceConsentLogTable`** — an append-only history, holding **no biometric data
at all**:

| Field | Purpose |
|---|---|
| `SubjectId` (PK) | Who the record is about. |
| `RecordedAt` (SK) | ISO timestamp, so the history is ordered and unique per event. |
| `Action` | `GRANTED`, `RE_CONSENTED`, `REVOKED`, `EXPIRED`, `DELETED`. |
| `TermsVersion`, `TermsSha256` | Which terms text, and its exact hash. |
| `ActorSub`, `ActorEmail` | The authenticated identity that performed the action. |
| `SourceIp`, `UserAgent` | Where from. |

The split is the point. Revoking consent **deletes the voiceprint** but keeps the
record that consent was given and then withdrawn — which is exactly what an
auditor needs and exactly what the person asking to be forgotten should not have
to leave behind.

## The audit trail

Three properties make the log an audit trail rather than a logfile:

**It is append-only in IAM, not by convention.** The application role is granted
`dynamodb:PutItem` and `dynamodb:Query` on the consent log and is *not* granted
`UpdateItem` or `DeleteItem`. The application cannot rewrite its own history even
if its code is wrong or compromised. Only an operator with separate privileges
can.

**The terms are pinned by hash, and the hash is compared.** `TermsSha256` is the
SHA-256 of the exact text the person was shown, stored both in the consent log
and on the enrollment itself. An `active` enrollment stops being used for
matching, and the portal asks its owner to agree again, as soon as either the
`TermsVersion` or the text behind it changes. Previous consents still prove what
was agreed to.

Comparing the text and not only the version matters because **the retention
period appears in the terms**: the text says how many days a consent stands. So
changing `VoiceEnrollmentRetentionDays` changes what people agreed to, and
everybody is asked to agree again at the new period — without an operator having
to remember to bump `VoiceEnrollmentTermsVersion` as well. Until they do, their
voiceprints are not used; they are not deleted, and agreeing again *and
re-recording* restores them. Plan a retention change accordingly: it asks every
enrolled person to repeat the enrollment, so it is not a parameter to tune
casually once people have enrolled.

**Point-in-time recovery is on**, so the log can be restored to any moment in the
retention window.

**Both tables survive stack deletion** (`DeletionPolicy: Retain`), and that cuts
both ways, so it is worth being explicit. It means a stack deleted by accident,
or replaced, does not take the consent history with it — an audit record that a
`cdk destroy` can erase is not much of an audit record. It also means **deleting
the stack leaves the voiceprints behind**, in a table nothing is managing any
more. If you are decommissioning a deployment and people have enrolled, delete
those two tables deliberately after the stack, or ask people to withdraw first,
which destroys the voiceprints and leaves only the log.

## Who can enrol

Anyone with an account in the deployment can enrol their own voice, and nobody
can enrol anybody else's. **Enrol My Voice** appears in the navigation for every
user when `EnableVoiceEnrollment` is `true`.

There is no administrative enrollment action, and that is a property of the
design rather than a gap in the UI: a consent somebody else gives on your behalf
is not consent. An admin can revoke on somebody's behalf — for a person who has
left — which is recorded with the admin as the actor.

**People without an account in the deployment cannot currently enrol.** The
obvious use for this feature is the participant from another company or the
contractor in a meeting room, and they are exactly who it does not yet serve,
because enrolling would mean issuing them an account here. See
[limits we hold deliberately](#limits-we-hold-deliberately).

## Enrolling a voice

The portal records a short, fixed script — enough speech for a stable voiceprint,
which in practice is about 20–30 seconds across several utterances. The audio is
streamed to an [ASR MicroVM](microvm-asr.md) session in `embed` mode, which
returns one embedding per utterance; those are averaged into the stored centroid.

The session is acquired for the enrolment and released as soon as it finishes,
including when it fails. It is the same engine and the same short-lived MicroVM
a meeting uses, so **enrolling requires `EnableMicrovmAsr=true`**: the embedding
model lives in that image and Amazon Transcribe exposes no speaker embedding. In
a deployment with enrolment on but that engine absent, the portal says so rather
than telling somebody their recordings were unusable.

**The sample audio is not kept.** It is held in memory for the length of the
request and never written to S3 or to a table. What persists is the centroid,
which is a one-way summary: it is enough to recognise the voice and not enough to
reconstruct the speech.

## How a match is used

When a meeting starts in a deployment with enrollment on, the transcription
session is seeded with the active centroids it is allowed to use, each labelled
with its `DisplayName`. From then on the engine assigns speech to those names
directly instead of minting `spk_0`.

A match is written as the segment's speaker, in place of the voice id the
segment would otherwise have carried. It is not currently marked as a match
rather than a name somebody typed, and it is not offered for confirmation the
way a name
[inferred from the transcript](transcription-and-translation.md#finding-the-names-automatically)
is. Correcting one is therefore the same job as renaming any other speaker, from
the Speakers panel on the meeting page.

Nothing is named on a guess. A seeded voice is only ever assigned speech whose
similarity score clears the engine's threshold — it is never the nearest
remaining option when a speaker cap is reached, and it does not consume cap
room, because a deployment having more enrolled people than a meeting has
participants is the normal case.

### Which voices a meeting is given

A meeting cannot know who is in the room. A transcript captured from a browser
tab or a desktop app has no attendee list — that is the whole reason the Speakers
panel offers a pasted one — so a starting session is given every enrollment that
consent currently permits, up to 32 of them. An enrollment counts only while all
five of these hold: it is `active`, it has a voiceprint, its `TermsVersion` is
the one in force, the hash of the terms it agreed to matches the terms in force,
and its consent has not lapsed. The transcriber asks for the
list at the start of every meeting and keeps no copy, so a withdrawal applies to
the very next meeting.

A voiceprint that is sent and never speaks does nothing. Matching is a
similarity comparison against the voices actually heard, and a score below the
threshold produces no name, so sending somebody's centroid to a meeting they are
not in produces no label for them.

Where more than 32 enrollments are permitted, the most recently enrolled are
sent; the rest of the meeting's voices are named the way they would be without
enrollment. The bound exists so session setup stays quick, not as a limit on how
many people may enrol.

If the lookup fails or is slow, the meeting starts with voice ids instead. A
transcript matters more than a label, so nothing about this step can stop a
meeting being transcribed.

## Withdrawing consent

The enrollee can withdraw at any time from the same portal, without asking
anyone. Withdrawal is immediate and does three things: the centroid is deleted,
the status becomes `revoked`, and a `REVOKED` row is appended to the log. No
future meeting can match the voice, because there is no longer a voiceprint to
match against.

Transcripts already produced keep the names they were given. Those are ordinary
speaker labels on a stored transcript, not biometric data, and they are editable
on the meeting page like any other name.

An admin can also revoke on somebody's behalf — for a person who has left, say —
which records the admin as the actor.

## Retention

`VoiceEnrollmentRetentionDays` (default 365) bounds how long a consent stands
without being renewed. A nightly pass deletes the centroid of any enrollment
whose consent is older than that, sets it to `revoked` and appends an `EXPIRED`
row. Consent that never expires is not meaningfully informed, and an
organisation that wants a shorter cycle can set one.

## Limits we hold deliberately

- **Meeting audio is never enrolled.** A voiceprint is only ever created from
  audio somebody recorded in the portal after consenting. The system does not
  build a voiceprint from a meeting, however much speech it has, and there is no
  setting that makes it do so.
- **Enrollment is never implied.** Attending a meeting, being named in a
  transcript, or having an LMA account does not enroll anybody.
- **The sample audio is discarded**, as above.
- **An admin cannot consent for somebody else.** An admin can revoke; only the
  enrollee can grant, and there is no administrative action that creates a
  voiceprint.
- **No cross-deployment sharing.** A centroid stays in the account and table it
  was created in.
- **People outside the deployment cannot enrol yet.** Enrolling requires an
  account here, so the external participant and the contractor in a meeting
  room — the people this feature is most useful for — are not served by it
  today. Inviting them would mean issuing them credentials to this deployment,
  which is a larger decision than enrollment, and it is deliberately not part
  of this feature. Their voices are named the way they were before: by hand on
  the Speakers panel, or from
  [what they say in the meeting](transcription-and-translation.md#finding-the-names-automatically).
- **A match is not yet distinguishable from a typed name.** See
  [how a match is used](#how-a-match-is-used).

## Deploying it

| Parameter | Default | Effect |
|---|---|---|
| `EnableVoiceEnrollment` | `false` | Creates the tables, the portal and the matching step, and adds **Enrol My Voice** to the navigation. While `false`, none of it exists. |
| `VoiceEnrollmentRetentionDays` | `365` | How long a consent stands before it must be renewed. |
| `VoiceEnrollmentTermsVersion` | `1` | Bump to require everybody to re-consent to new terms. |

Both enrolling and matching need the on-demand
[ASR MicroVM engine](microvm-asr.md) (`EnableMicrovmAsr=true`), because that is
the engine that computes speaker embeddings — one to build a voiceprint, the
other to compare against it. Amazon Transcribe exposes none, so with enrolment
on and that engine off the tables, the portal and the consent log all exist and
work, and the step that would create a voiceprint reports that the engine is
missing.

## Related Documentation

- [Transcription & Translation](transcription-and-translation.md) — speaker separation and naming voices by hand
- [On-demand ASR & Speaker Diarization (MicroVM)](microvm-asr.md) — the engine that produces the embeddings
- [User Management](user-management.md) — Cognito groups and invitations
- [Infrastructure & Security](infrastructure-and-security.md) — encryption and key management
