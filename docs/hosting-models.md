---
title: "Hosting Models"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Hosting Models

LMA is deployed in two ways that call for different defaults, and it ships a
[configuration preset](configuration-guide.md#configuration-presets) for each.
This page describes the two models, what each preset sets and why, the settings
no preset can set, and how to move meetings from one model to the other.

## Table of Contents

- [The two models](#the-two-models)
- [The Team-hosted (external meetings) preset](#the-team-hosted-external-meetings-preset)
- [The Customer account preset](#the-customer-account-preset)
- [Settings no preset can set](#settings-no-preset-can-set)
- [Isolating a team-hosted account](#isolating-a-team-hosted-account)
- [What the meeting platform's administrators may need to allow](#what-the-meeting-platforms-administrators-may-need-to-allow)
- [Deploying in the customer's account on day one](#deploying-in-the-customers-account-on-day-one)
- [Moving from one model to the other](#moving-from-one-model-to-the-other)
- [Related](#related)

## The two models

**Team-hosted.** A team runs LMA in an AWS account of its own and uses it for
meetings with people from other organizations, for example before any project
exists in those organizations' own accounts. One deployment then holds meetings
with several unrelated external organizations. Their people join the meetings
as guests, and must never reach the team's other meetings or the team's own
knowledge through LMA. Use the **Team-hosted (external meetings)** preset
(`team-hosted-external`).

**Customer account.** LMA is deployed in the account of the organization whose
meetings it records, and that organization's policies apply. Use the **Customer
account** preset (`customer-account`) as a starting point, and decide each of
its settings with that organization.

Choose a preset at deploy time with the `ConfigurationPreset` stack parameter,
or apply it later from **Configuration ▸ Configuration ▸ Actions ▸ Apply a
preset…**. Each preset is also a read-only **Managed** profile that can be
activated as it is, or copied to a Custom profile to change. Like every preset,
each one sets the same list of settings and leaves every other setting as it is;
see [Configuration presets](configuration-guide.md#configuration-presets) for
the full table.

## The Team-hosted (external meetings) preset

| Area | Setting | Value | Why |
|---|---|---|---|
| Consent | Meeting Consent › Consent mode | Opt-in | Nothing is recorded or transcribed until consent is given. |
| | Meeting Consent › Consent rule | Each participant consents | Every participant present replies, rather than the owner attesting for them. A participant who does not consent is not transcribed, under the [opt-in rules](meeting-consent.md#opt-in). |
| | Meeting Consent › Allow a different choice per meeting | Off | A meeting may only use this mode and rule or a stricter one, so it cannot switch to the owner attesting for everyone. |
| | Meeting Consent › Streaming clients without a consent step | Refuse the meeting | A client that cannot ask for consent does not capture. |
| | Meeting Consent › Virtual Participant leaves after an objection | On | An objection ends the Virtual Participant's part in the meeting. |
| | Meeting Consent › Keep evidence screenshots | On | The consent record keeps the evidence of each reply. |
| Recordings | Meeting Recording › Record meeting audio | Off | Only the transcript is kept. Meeting video is a stack parameter; see [Settings no preset can set](#settings-no-preset-can-set). |
| | Meeting Recording › Record screen video from Stream Audio and the browser extension | Off | Those two clients record no screen video. |
| | Retention › Delete the transcript after the summary | Off | The transcript is kept for the retention period, 90 days by default. |
| | Virtual Participant › Add the meeting chat to the transcript | On | The chat is part of the meeting's record and is deleted with it. |
| In-meeting requests | Meeting Assistant › In-meeting requests: who may ask | Off | No request made in the meeting is answered, and a Virtual Participant does not start its voice assistant. See [Who may ask](#who-may-ask). |
| | Meeting Assistant › In-meeting requests | Current meeting only | Held to the current meeting in case a copied profile allows requests again. |
| | Meeting Assistant › In-meeting requests: meeting context and checklist | None | The owner's preparation notes are never read out to guests. |
| Knowledge | Meeting Assistant › Web search | Off | Nothing from a meeting is sent to a web search provider, from the meeting page's chat either. |
| | Meeting Assistant › MCP servers | Off | None of the MCP servers installed on the MCP Servers page is connected to or offered. LMA's own MCP server is not affected. |
| | Meeting Assistant › Live guidance › On for new meetings | On | The people running a meeting are offered [live guidance](live-guidance.md) in its Notifications drawer, never in the meeting chat and never to its guests. Each run is a model call billed to the meeting; a meeting can still turn it off. |
| Summaries | Meeting Processing › Default summary profile for new meetings | `discovery-session` | New meetings are summarized with decisions, action items, open questions, risks and requirements heard; see [Summary profiles](transcript-summarization.md#summary-profiles). |
| Other | Amazon Transcribe › Redact personal information; Amazon Bedrock Models › Model inference geography | Off; Global | The shipped defaults. Change them in a copy of the profile if your policy needs them. |

The consent settings are those of the Regulated – consent required preset, so
they have the same consequences: Stream Audio and the Desktop Capture App cannot
collect each participant's consent, so **Start** is refused from them; a Virtual
Participant on the Webex join cannot tell who is present, so a Webex meeting is
never granted; and on the Teams browser join the chat senders and the
participant list are read from the Teams page. Use a Virtual Participant on a
join that supports each-participant consent; see
[What each platform supports](meeting-consent.md#what-each-platform-supports).
The consent request is posted in the meeting chat. It is not spoken: no join
speaks the consent notice, so a participant who cannot see the chat hears no
request to reply (see [Not done yet](meeting-consent.md#not-done-yet)).

### Who may ask

A Virtual Participant cannot tell the meeting's owner from a guest. It knows a
chat sender only by the display name the meeting shows, which a guest who joins
without signing in types themselves, and on the Teams browser join off the
light-meetings client it sees no sender at all. It knows a speaker only by a
diarization label. So **In-meeting requests: who may ask** has no value that lets
only the owner ask through a Virtual Participant: **Owner only** answers only a
wake phrase spoken into the owner's own microphone from Stream Audio, the Desktop
Capture App or the browser extension, and nothing in a meeting a Virtual
Participant captures. The Team-hosted preset therefore uses **Off**. The owner
still asks the meeting assistant from the meeting page, which is signed in as
them. See [Who may ask](meeting-assistant.md#who-may-ask).

### The meeting-history knowledge base

The transcript knowledge base is not shared across users. Each meeting's files
carry its owner, and every search is filtered on the owner, except an Admin's on
the Meetings Query page and through the MCP server; see
[Knowledge Base Search](user-based-access-control.md#knowledge-base-search). So
one user's assistant never finds another user's meetings, and the preset does
not need to keep transcripts out of the knowledge base.

The owner's own past meetings are a different matter: a team member who hosts
meetings with several organizations owns all of them, and a request made in a
meeting that is allowed past meetings would search them all. That is why
**In-meeting requests** stays at **Current meeting only**, and requests made in
the meeting are off. Keep the Admin group to the people who run the deployment,
since an Admin's search reaches every meeting, as an Admin's meeting list does.
To keep transcripts out of the knowledge base entirely, deploy with
`TranscriptKnowledgeBase` set to `DISABLED`.

### Meeting events

The stack's [meeting lifecycle events](meeting-events.md) carry identifiers and
states only, never meeting content or participants' names, so the presets set
nothing for them. A rule added to the event bus is the team's own integration,
in the team's account.

## The Customer account preset

A starting point for the organization whose meetings are recorded. Each setting
is one to **decide with the customer**.

| Setting | Value | Decide with the customer |
|---|---|---|
| Meeting Consent › Consent mode, rule | Opt-in, each participant consents | Whether their policy needs consent first, notice with the right to object (Notify), or neither. |
| Meeting Consent › Allow a different choice per meeting | On | Whether the owner of a meeting may choose another mode or rule for it, such as owner attestation for an internal meeting. |
| Meeting Consent › Streaming clients without a consent step | Refuse the meeting | Whether the browser extension and older clients may capture without asking. |
| Meeting Recording › Record meeting audio | Off | Whether recordings are wanted. Meeting video is the `EnableVideoRecording` stack parameter. |
| Meeting Recording › Record screen video from Stream Audio and the browser extension | Off | Whether users may record the screen or tab they share from those two clients, where `EnableVideoRecording` is `true`. |
| Retention | Transcript kept for the retention period | The periods their policy requires: the retention stack parameters, and **Retention › Delete the transcript after the summary**. |
| Knowledge base | Allowed | The document knowledge base holds the customer's own documents; it is chosen with the `MeetingAssistService` stack parameter and its data sources. |
| Meeting Assistant › Web search | Off | Whether meeting content may be sent to a web search provider. |
| Meeting Assistant › MCP servers | On | Which of their systems the assistant may reach; each server is installed by an Admin. |
| Meeting Assistant › In-meeting requests: who may ask | Anyone | Whether participants may ask the assistant in the meeting, answered from the current meeting only. |
| Meeting Processing › Default summary profile for new meetings | `default` | The `discovery-session` profile is available for each meeting; make it the default if most meetings are discovery sessions. |

Stream Audio and the Desktop Capture App have no participant list, so they
cannot collect each participant's consent. Because this preset allows a
different choice per meeting, their meetings run under the rule **The meeting
owner attests** instead: the LMA user confirms that everyone in the meeting
agreed before **Start**. If the customer turns **Allow a different choice per
meeting** off, **Start** is refused from both, and their meetings need a Virtual
Participant. See
[Stream Audio and the Desktop Capture App](meeting-consent.md#stream-audio-and-the-desktop-capture-app).

## Settings no preset can set

These are stack parameters, or are not part of LMA, so they are set when the
stack is deployed or updated, or in the account. For a team-hosted deployment:

1. **Meeting video off.** Set `EnableVideoRecording` to `false`, so no
   Virtual Participant, Desktop Capture App, Stream Audio page or browser
   extension records video and no key frames
   are taken. Key frames exist only where video is recorded; where it is, their
   items are kept as long as the transcript and their images follow
   `TranscriptionExpirationInDays`.
2. **Transcripts kept for 90 days, then deleted.** `TranscriptionExpirationInDays`
   and `MeetingRecordExpirationInDays` are 90 by default. The Retention section
   of the Configuration page can shorten them, never lengthen them; see
   [Retention controls](privacy-and-data-handling.md#retention-controls).
   `AudioRecordingExpirationInDays` covers any recording made anyway.
3. **No self sign-up.** Leave `AllowedSignUpEmailDomain` empty, so the web UI
   offers no sign-up and an Admin creates every user from
   [User Management](user-management.md). Guests from other organizations never
   need an LMA account.
4. **Multi-factor authentication.** The stack does not configure MFA on its
   Amazon Cognito user pool. We recommend turning it on for the user pool in the
   Amazon Cognito console. Whether a later stack update keeps a setting made in
   the console has not been verified, so check it after every stack update.
5. **No IP allowlist for the web UI.** The stack has no setting that restricts
   the web UI by IP address. `WAFAllowedIPv4Ranges` applies only to the MCP
   server's API (see [WAF](infrastructure-and-security.md#waf-web-application-firewall)),
   and `CloudFrontAllowedGeos` restricts the web UI by country, not by address.
6. **Logging.** Keep `LogLevel` at `INFO` or `WARN`; `DEBUG` logs meeting content.

## Isolating a team-hosted account

- **A dedicated AWS account.** Deploy the team-hosted LMA in an account that
  runs nothing else, so that nothing else in it can read the meetings table, the
  recordings bucket or the logs, and so its IAM permissions can be reviewed on
  their own.
- **AWS CloudTrail on**, with the trail's logs kept outside the account or
  protected from change, so access to the meeting data is on record.
- **Console access** only for the people who operate the deployment. Anyone with
  broad IAM permissions in the account can read every meeting directly,
  whatever LMA's own access rules say, and can run
  [`lma meetings delete`](lma-cli.md#lma-meetings-delete--delete-meetings-by-label).
- **The LMA Admin group** only for the same people: an Admin reads and searches
  every meeting.
- Amazon Bedrock model invocation logging, if the account turns it on, logs
  every prompt, including whole transcripts; see
  [Copies that follow their own lifetime](privacy-and-data-handling.md#copies-that-follow-their-own-lifetime).

## What the meeting platform's administrators may need to allow

The Virtual Participant joins a meeting as a participant from outside the
meeting's organization, under the display name **Virtual Participant › Display
name** gives it. When the meeting belongs to the other organization, its
platform administrators may need to allow that:

- **Zoom.** Participants from outside the account may join; a meeting that only
  admits signed-in Zoom users needs the LMA user's saved Zoom credentials (see
  [Zoom credentials](zoom-credentials-and-join-reliability.md)). A waiting room
  holds the Virtual Participant until someone admits it.
- **Microsoft Teams.** Anonymous participants may join the meeting, and the
  lobby holds them until admitted unless *Who can bypass the lobby* lets them
  in. The Azure Communication Services join has tenant settings of its own; see
  [Teams through Azure Communication Services](teams-acs-sdk.md).
- **Google Meet.** The meeting's access setting admits guests who are not in
  the organization, after someone in the meeting lets them in.
- **Webex.** Guests may join the meeting. Each-participant consent cannot be
  granted on the Webex join, so under the Team-hosted preset a Webex meeting is
  not transcribed.

In every case, tell the other organization before the meeting that LMA will
join and what it keeps, so that their participants can expect the consent
request in the chat.

## Deploying in the customer's account on day one

The deployment takes about 35 to 40 minutes, so it can run on the first day of
a project. Before it starts, the customer's account needs:

- an IAM principal allowed to deploy the stack, or a
  [CloudFormation service role](cloudformation-service-role.md);
- access to the [Amazon Bedrock models](prerequisites-and-deployment.md#amazon-bedrock-model-access)
  LMA uses, in the chosen Region;
- the decisions in [The Customer account preset](#the-customer-account-preset),
  and the stack parameters in [Settings no preset can set](#settings-no-preset-can-set),
  made with the customer.

Then deploy with `ConfigurationPreset` set to `customer-account`; see
[Prerequisites & Deployment](prerequisites-and-deployment.md).

The customer's security review can start before that, from material the
repository already has: the threat model in `threat-modeling/` (its executive
summary and implementation guide are under `threat-modeling/deliverables/`),
[Privacy and Data Handling Controls](privacy-and-data-handling.md), which lists
every kind of data LMA stores, where, for how long and what controls it, and
[Infrastructure & Security](infrastructure-and-security.md).

## Moving from one model to the other

When an organization's project moves from the team-hosted deployment to its own
account:

1. **Export what has to move**, if anything. Each meeting's
   [meeting export](meeting-export.md) holds its transcript, summary and the rest
   of its record as one file, from **Download › Meeting export (.json)** on the
   meeting page or the MCP `get_meeting_export` tool.
2. **Delete that organization's meetings** from the team-hosted deployment. Give
   its meetings a [label](meeting-context-and-checklist.md) when they are
   scheduled, for example `engagement=acme`, and then:

   ```bash
   # Lists what would be deleted; nothing is deleted
   lma meetings delete --label engagement=acme --user-name admin@example.com

   # Deletes it
   lma meetings delete --label engagement=acme --user-name admin@example.com --confirm
   ```

   As an Admin, the command deletes every user's matching meetings; as anyone
   else, only their own. Each meeting goes through the same deletion as the
   meetings list, so its transcript, summary, key frames, recordings, consent
   record, notifications and archives are removed with it. A meeting still in
   progress is skipped. See
   [`lma meetings delete`](lma-cli.md#lma-meetings-delete--delete-meetings-by-label).
3. Deleting does not reach the copies listed in
   [Copies that follow their own lifetime](privacy-and-data-handling.md#copies-that-follow-their-own-lifetime):
   in particular, the DynamoDB tables' point-in-time recovery can restore a
   deleted item for 35 days, and downloads stay where they were saved.

## Related

- [Configuration Guide › Configuration presets](configuration-guide.md#configuration-presets)
- [Meeting Consent](meeting-consent.md)
- [Meeting Assistant › Who may ask](meeting-assistant.md#who-may-ask)
- [Privacy and Data Handling Controls](privacy-and-data-handling.md)
- [User-Based Access Control](user-based-access-control.md)
- [Infrastructure & Security](infrastructure-and-security.md)
- [LMA CLI](lma-cli.md)
