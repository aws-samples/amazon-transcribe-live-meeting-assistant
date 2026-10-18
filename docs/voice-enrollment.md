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
- [Inviting people to enrol](#inviting-people-to-enrol)
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

The hard part of this feature is not the matching. It is that **the person whose
voice is being enrolled is frequently not an LMA user** — a participant from
another company, a contractor in a meeting room, a candidate in an interview.
Letting the meeting host assert "they agreed" on their behalf would make the
consent record worthless.

So consent is **first-party and self-service**. The enrollee authenticates as
themselves and performs the consent in their own session:

1. An LMA user **invites** an email address to enroll. This creates a Cognito
   user in the `VoiceEnrollee` group, which can reach the enrollment portal and
   nothing else — no meetings, no transcripts, no configuration. The invitation
   is itself recorded, so who asked whom is auditable.
2. The invitee signs in to **`/#/voice-enrollment`** and is shown the consent
   terms in full, including what is stored, what it is used for, how long it is
   kept and how to withdraw.
3. They consent by an explicit affirmative action — a checkbox they must tick
   plus a button they must press. There is no pre-ticked box and no implied
   consent from continuing.
4. Only then does the portal offer to record voice samples.

Consent is recorded **before** any audio is captured, so a record exists even if
the person abandons the recording step. Declining is a normal outcome: the portal
is a dead end with nothing stored.

## What is stored, and where

Two separate tables, for two different reasons.

**`VoiceEnrollmentTable`** — the current state, and the only place biometric data
lives:

| Field | Purpose |
|---|---|
| `SubjectId` | The enrollee's Cognito `sub`. Never an email, so the key is not itself personal data. |
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
| `Action` | `INVITED`, `GRANTED`, `RE_CONSENTED`, `REVOKED`, `EXPIRED`, `DELETED`. |
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

**The terms are pinned by hash.** `TermsSha256` is the SHA-256 of the exact text
the person was shown. If the terms are later edited, previous consents still
prove what was agreed to, and the mismatch is detectable. A changed
`TermsVersion` requires a fresh consent — an `active` enrollment whose terms
version is no longer current is treated as `pending` until the enrollee
re-consents.

**Point-in-time recovery is on**, so the log can be restored to any moment in the
retention window.

## Inviting people to enrol

Somebody who already has an LMA account can go straight to the portal at
`#/voice-enrollment` and enrol themselves; they do not need to be invited, and no
admin has to be involved.

Inviting matters for everybody else — the participant from another company, the
contractor in a meeting room — because they need an identity of their own before
they can consent for themselves. **Configuration → Voice Enrollment** (admin
only, and only present when `EnableVoiceEnrollment` is `true`) takes a list of
email addresses, one per line or separated by commas; pasting a recipient line
from a meeting invite works, and a `Sarah Lee <sarah@example.com>` form is
reduced to the address. Each address is invited separately and reported
separately, so one that cannot be invited does not cost the others theirs. If
the deployment restricts sign-ups to one email domain, invitations are held to
the same rule.

An invitation creates a Cognito user in the `VoiceEnrollee` group, which can
reach the enrollment portal and nothing else — no meetings and no configuration.
Inviting somebody who already has an account is fine and common; it just records
the ask.

**An invitation is not a consent.** It creates the ability for somebody to
decide, and records who asked them. Declining is a normal outcome and leaves
nothing stored. There is no control on that page, and cannot be one, that
enrolls anybody: the backend refuses a consent granted by anyone other than its
subject.

The page does not list who has enrolled. An enrollment is the enrollee's own
record, and they manage it from the portal.

## Enrolling a voice

The portal records a short, fixed script — enough speech for a stable voiceprint,
which in practice is about 20–30 seconds across several utterances. The audio is
streamed to an [ASR MicroVM](microvm-asr.md) session in `embed` mode, which
returns one embedding per utterance; those are averaged into the stored centroid.

**The sample audio is not kept.** It is held in memory for the length of the
request and never written to S3 or to a table. What persists is the centroid,
which is a one-way summary: it is enough to recognise the voice and not enough to
reconstruct the speech.

## How a match is used

When a meeting starts in a deployment with enrollment on, the transcription
session is seeded with the active centroids it is allowed to use, each labelled
with its `DisplayName`. From then on the engine assigns speech to those names
directly instead of minting `spk_0`.

A match arrives in the transcript as a **suggestion**, not a settled name, with
`source: voiceprint` — so a reader can always tell how somebody was identified,
and a wrong match is one click to reject. It is treated exactly like a name
inferred from the transcript: see
[finding the names automatically](transcription-and-translation.md#finding-the-names-automatically).

### Which voices a meeting is given

A meeting cannot know who is in the room. A transcript captured from a browser
tab or a desktop app has no attendee list — that is the whole reason the Speakers
panel offers a pasted one — so a starting session is given every enrollment that
consent currently permits, up to 32 of them. An enrollment counts only while all
four of these hold: it is `active`, it has a voiceprint, its `TermsVersion` is
the one in force, and its consent has not lapsed. The transcriber asks for the
list at the start of every meeting and keeps no copy, so a withdrawal applies to
the very next meeting.

A voiceprint that is sent and never speaks does nothing. Matching is a
similarity comparison against the voices actually heard, so sending somebody's
centroid to a meeting they are not in produces no label for them.

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
- **An admin cannot consent for somebody else.** An admin can invite, and can
  revoke; only the enrollee can grant.
- **No cross-deployment sharing.** A centroid stays in the account and table it
  was created in.

## Deploying it

| Parameter | Default | Effect |
|---|---|---|
| `EnableVoiceEnrollment` | `false` | Creates the tables, the portal, the invitation path and the matching step. While `false`, none of it exists. |
| `VoiceEnrollmentRetentionDays` | `365` | How long a consent stands before it must be renewed. |
| `VoiceEnrollmentTermsVersion` | `1` | Bump to require everybody to re-consent to new terms. |

Matching also needs the on-demand
[ASR MicroVM engine](microvm-asr.md) (`EnableMicrovmAsr=true`), because that is
the engine that computes embeddings. Amazon Transcribe exposes no speaker
embedding, so a deployment using it can enroll voices but cannot match them.

## Related Documentation

- [Transcription & Translation](transcription-and-translation.md) — speaker separation and naming voices by hand
- [On-demand ASR & Speaker Diarization (MicroVM)](microvm-asr.md) — the engine that produces the embeddings
- [User Management](user-management.md) — Cognito groups and invitations
- [Infrastructure & Security](infrastructure-and-security.md) — encryption and key management
