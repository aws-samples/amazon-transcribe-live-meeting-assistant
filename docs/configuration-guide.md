---
title: "Configuration Guide"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Configuration Guide

This page is for administrators. It explains which of LMA's settings you can
change from the web UI while the stack is running, how to change them on the
**View / Edit Configuration** page (the Configuration page), what happens when you save, and what to do when a save is
refused. How the settings are stored, cached and versioned underneath is
described in the [Unified Configuration Store](configuration-store.md) reference.

## What you can change without redeploying

Two kinds of setting control LMA. **Stack parameters** are chosen when the
CloudFormation stack is deployed or updated; they decide which resources exist,
such as whether the Virtual Participant or the on-demand speech engine is
deployed at all, and changing one means a stack update. They are listed in
[CloudFormation Parameters](cloudformation-parameters.md). **Runtime settings**
are what this page is about: they are stored in the stack's configuration table,
an Admin changes them from the web UI, and the change takes effect without a
deployment.

The runtime settings are grouped into sections, and held in configuration
profiles (see [Configuration profiles](#configuration-profiles)). The deployment
default profile holds every setting's default, which ships with LMA and is
updated by later releases; a Custom profile holds a full copy you change. Settings
that were stack parameters before 0.4.0 are listed in
[Settings that were stack parameters before 0.4.0](#settings-that-were-stack-parameters-before-040).
For the retention settings, a stack parameter is still the default and a limit;
see [Retention: settings the stack limits](#retention-settings-the-stack-limits).

| Section | What it controls | A saved change applies |
|---|---|---|
| Transcript Summary | The sections of the end-of-meeting summary and the prompt behind each | To summaries generated within about five minutes of the save. **Regenerate** on a meeting page uses it at once |
| Meeting Assist Chat Buttons | The quick-action buttons in the chat panel. A saved list replaces the shipped buttons as a whole | At once, to chat panels opened after the save |
| Nova Sonic Voice Assistant | The voice assistant's system prompt and prompt mode, model, voice, turn-taking sensitivity, meeting mode and translator settings | To the next meeting the voice assistant joins |
| Transcription Engine | Virtual Participant voice separation, which works on Amazon Transcribe, and, where the experimental on-demand speech engine is deployed, which engine transcribes streaming meetings and which transcribes Virtual Participants. Without that engine only the voice separation switch is shown | To meetings that start within 30 seconds of the save or later |
| Meeting Cost Prices | The unit prices the per-meeting [cost estimate](meeting-cost-estimate.md) is computed with, by region, shown as a table with the source of every shipped price; change one to use your own rate, and export or import the whole table (see [Changing prices](meeting-cost-estimate.md#changing-prices)). Also opened from **Configuration ▸ Meeting Cost Prices** in the navigation | To estimates drawn within about five minutes of the save |
| Amazon Bedrock Models | The Amazon Bedrock model used for summaries and meeting insights, for the meeting assistant and the troubleshooting agent, for the meeting assistant's knowledge bases, and for reading the text of key frames, and the **Model inference geography**: whether those models, the consent evidence check model and the Virtual Participant's page-automation model may run in any commercial AWS Region, only within this deployment's geography, or only in its Region (see [Where meeting content is processed](infrastructure-and-security.md#where-meeting-content-is-processed)) | To each summary, answer or key frame started about five minutes after the save or later |
| Amazon Transcribe | The Amazon Transcribe options of meetings streamed from the Stream Audio page, the browser extension and the desktop app, and of Virtual Participants: language or language identification, redaction, custom vocabulary and language model, and the default for identifying speakers within a channel | To streaming meetings that start after the save, and to the next Virtual Participant to start |
| Meeting Recording | Whether those meetings' audio is recorded when the client does not say | To streaming meetings that start after the save, and to the next Virtual Participant to start |
| Meeting Assistant | The regular expression that wakes the meeting assistant in a transcript, whether its answers use Amazon Bedrock [prompt caching](meeting-assistant.md#prompt-caching), its reasoning effort, and what a request made in the meeting (the meeting chat, the voice assistant, the wake phrase) is answered from ([In-meeting requests](meeting-assistant.md#in-meeting-requests)) | To transcript segments processed, and answers started, about five minutes after the save or later |
| Meeting Processing | Whether a transcript Lambda hook is sent partial segments, how long a meeting may go without speech before the inactive-meeting reaper ends it, and how far back the reaper looks | Within about five minutes of the save |
| Virtual Participant | The Virtual Participant's voice assistant provider, activation mode, wake phrases and activation duration, the ElevenLabs agent and Simli face, how a Teams Virtual Participant decides its meeting has ended, and whether the meeting chat is added to the transcript (see [Meeting Chat in the Transcript](virtual-participant.md#meeting-chat-in-the-transcript)) | To the next Virtual Participant to start |
| Retention | How many days meeting records and transcript segments are kept, up to the limit the stack sets | To records written about five minutes after the save or later; a record already written keeps its expiry |
| Meeting Consent | Whether participants are told a meeting is recorded and transcribed and whether their consent is needed first (off, notify or opt-in, and the opt-in rule), the notice texts, the phrases a reply is read by, whether a meeting may be started with another choice, whether evidence screenshots are kept, the model that checks them, what the WebSocket transcriber does with a streaming client that shows no consent step, whether a Virtual Participant leaves after an objection, and whether it reads replies said aloud. See [Meeting Consent](meeting-consent.md) | To meetings whose consent starts after the save |
| Meeting Agenda | How far into a meeting with no agenda its transcript is read for one somebody stated, or whether it is read at all | To meetings that reach the threshold about five minutes after the save or later |

Every section is on **Configuration ▸ Configuration**. The pages that each edited
one section — Transcript Summary, Nova Sonic Config and Transcription Engine — are
now sections of it, and a bookmark to one of them opens its section. The **Edit
Chat Buttons** dialog in a meeting's chat panel stays where it is and saves into
the same configuration.

Only members of the **Admin** Cognito group can change any of these. Add someone
to the group on [User Management](user-management.md) if they need to.

## Settings that were stack parameters before 0.4.0

Until 0.4.0 these settings were also stack parameters, and the parameter was the
setting's default. From 0.4.0 they are only settings: the parameters are gone, and
each setting ships with a default in the deployment's default profile.

The table lists the parameters a released version had; three settings new in
0.4.0 (the knowledge-base model, the key frame text model and the agenda timing)
were never released as parameters.

| Section › setting | Stack parameter before 0.4.0 |
|---|---|
| Amazon Bedrock Models › Summaries and meeting insights | `BedrockModelId` |
| Amazon Bedrock Models › Meeting assistant | `MeetingAssistServiceBedrockModelID` |
| Amazon Transcribe › Language | `TranscribeLanguageCode` |
| Amazon Transcribe › Language options | `TranscribeLanguageOptions` |
| Amazon Transcribe › Preferred language | `TranscribePreferredLanguage` |
| Amazon Transcribe › Redact personal information | `IsContentRedactionEnabled` |
| Amazon Transcribe › Entity types to redact | `TranscribePiiEntityTypes` |
| Amazon Transcribe › Custom vocabulary | `CustomVocabularyName` |
| Amazon Transcribe › Custom language model | `CustomLanguageModelName` |
| Amazon Transcribe › Identify speakers within a channel by default | `ShowSpeakerLabel` |
| Meeting Recording › Record meeting audio | `EnableAudioRecording` |
| Meeting Assistant › Wake phrase (regular expression) | `AssistantWakePhraseRegEx` |
| Meeting Processing › Send only finished segments to the transcript Lambda hook | `TranscriptLambdaHookFunctionNonPartialOnly` |
| Meeting Processing › Days to look back for meetings that were never ended | `MeetingInactivityLookbackInDays` |
| Meeting Processing › Minutes without speech before a meeting is ended | `MeetingInactivityTimeoutInMinutes` |
| Virtual Participant › Voice assistant | `VoiceAssistantProvider` |
| Virtual Participant › Voice assistant activation mode | `VoiceAssistantActivationMode` |
| Virtual Participant › Wake phrases | `VoiceAssistantWakePhrases` |
| Virtual Participant › Seconds the voice assistant stays active | `VoiceAssistantActivationDuration` |
| Virtual Participant › ElevenLabs agent ID | `ElevenLabsAgentId` |
| Virtual Participant › Simli face ID | `SimliFaceId` |
| Virtual Participant › Teams: milliseconds between meeting checks | `VPAttendeePollMs` |
| Virtual Participant › Teams: checks alone before leaving | `VPPollsBeforeEnd` |
| Virtual Participant › Teams: checks without a meeting before leaving | `VPPollsBeforeEndMissing` |

**Upgrading a deployment that set any of these parameters.** The values the
parameters held are not carried across. After the update, a deployment upgraded
from v0.3.10 uses the shipped default for every one of these settings. To keep a value you had set with a parameter:

- create a Custom profile (**Create profile** in the profile list), set the
  values there, save and **Activate** it; or
- write the values into a configuration file and deploy with the
  `CustomConfigPath` stack parameter, which makes them the deployment default; see
  [Choosing the default configuration at deploy time](configuration-store.md#choosing-the-default-configuration-at-deploy-time).

Remove the parameters from deploy commands and pipelines: CloudFormation refuses
an update that names a parameter the template does not declare. `lma deploy`
leaves them out on its own.

The integration API keys stay stack parameters, and a provider or avatar chosen on
the page without its key on the stack does not start. The Nova Sonic Voice
Assistant section says whether its Group meeting and Translator meeting modes
apply, from the activation mode as saved in the Virtual Participant section: they
need **Always listening**.

A few things to know about these settings:

- A model chosen on the page is not checked before it is used, so enable access to
  it in the Amazon Bedrock console first.
- The WebSocket transcriber checks, as each meeting starts, that the Amazon
  Transcribe options fit together: language identification needs language
  options, at most one dialect per language, a preferred language among them, and
  no custom vocabulary or language model. Options that do not fit together are
  logged, and the meeting is transcribed with the shipped defaults for those
  options until they are corrected. A Virtual Participant makes the same check as
  it starts.
- If the WebSocket transcriber cannot read the configuration when a meeting
  starts, that meeting uses the shipped defaults, and the next meeting reads the
  configuration again.
- Nothing checks that a custom vocabulary or language model exists. Amazon
  Transcribe checks when a meeting starts, and a meeting whose vocabulary or
  model it does not recognise is not transcribed.
- The page offers only the choices the schema lists. A model a later release stops
  offering stays in force where it was saved, and the next save of the section
  asks for another.
- **Meeting Processing › Minutes without speech before a meeting is ended** set to
  0 makes the inactive-meeting reaper end nothing; the reaper is always deployed.

## Retention: settings the stack limits

The **Retention** section sets how many days a meeting's records are kept:

| Section › setting | Default and upper limit from | What it covers |
|---|---|---|
| Retention › Days to keep meeting records | `MeetingRecordExpirationInDays` | The meeting, its summary and its usage records |
| Retention › Days to keep transcript segments | `TranscriptionExpirationInDays` | Each turn of the transcript, and each meeting assistant answer in it |

For these two, the stack parameter is the **upper limit** as well as the
default. You can keep records for less time than the deployment allows, but not
for more:

- The page shows the parameter's value as the default, says it is the limit, and
  marks a longer value as an error before you save. The server checks again, and
  refuses the save if a value exceeds the limit.
- Each function and task that writes these records applies the limit itself,
  from the parameter. So a value above it is never used, even one that reached
  the configuration another way, such as a restore of an older revision, or one
  saved before a stack update lowered the parameter. A stack update that lowers
  the parameter applies at once; one that raises it changes the default, and a
  shorter value saved on the page stays in force until you reset it.
- A change applies to records written after it, within about five minutes. A
  record already written keeps the expiry it was given, so shortening retention
  does not delete anything early, and lengthening it does not keep anything
  longer.
- Fractions of a day are allowed, as for the parameters, down to 0.1 day.
- **Delete the transcript after the summary**, in the same section, has no
  stack parameter: it ships set to Off, and any of its choices can be saved. It
  deletes each meeting's transcript
  permanently once the meeting is summarized, transcript files included; see
  [Transcript Deletion After the Summary](transcript-deletion.md).
- The recordings bucket's lifecycle rules, which delete transcript files and
  key-frame images after `TranscriptionExpirationInDays` days and recordings
  after `AudioRecordingExpirationInDays` days, follow the stack parameters only.
  Shortening transcript retention on the page does not delete those files
  sooner. Nor does shortening meeting-record retention shorten the expiry of
  Virtual Participant records, which follows
  `MeetingRecordExpirationInDays` only. See
  [What LMA stores, and for how long](privacy-and-data-handling.md#what-lma-stores-and-for-how-long).

## Configuration profiles

A deployment holds several **profiles**, each a complete configuration, and one
of them is **active**: the one every meeting uses. They work as IDP's do.

- **default** is the deployment default: the release's shipped settings, the
  preset and file the `ConfigurationPreset` and `CustomConfigPath`
  stack parameters name, and the stack parameters that are defaults of settings.
  Every stack update rewrites it, so it is read-only. A new deployment runs on it.
- **Managed** profiles are the shipped presets (`standard`,
  `internal-meetings-notify`, `regulated-consent-required` and
  `regulated-notify`; see
  [Configuration presets](#configuration-presets)), each
  the default with the preset applied. They are read-only and rewritten by every
  stack update too. Activate one to use it as it is.
- **Custom** profiles are yours. Choose **Create profile** in the profile list
  to copy any profile, change its settings and save them, then **Activate** it. A Custom
  profile is a full copy: a later release's new defaults do not change a setting
  it holds, and a setting the release adds comes from the default.

The **Configuration Profiles** list at the top of the page shows every profile
with its type (Managed or Custom), description and when it was last saved. It
marks the active one, and the one open in the editor below it; the page opens the
active profile by default, and the address names the profile open
(`#/configuration/settings?profile=trial`), so a link can open another. Choose a
profile's name to open it in the editor; if the editor holds unsaved changes, the
page asks first. The list can be collapsed to give the editor more room.

Select profiles in the list to act on them:

- **Compare profiles** (two or more selected) lists every setting the selected
  profiles do not all hold alike, with a column for each profile's value; a
  setting a profile does not hold reads `(not set)`. Profiles are compared as
  saved, so edits you have not saved are not included. **Export** downloads the
  comparison as CSV or JSON.
- **Activate** (one selected) makes it the active profile.
- **Delete** removes the selected Custom profiles; the active profile and the
  Managed ones cannot be deleted. A deleted profile's revisions stay in the
  history.
- **Create profile** copies a profile, as it is saved, to a new Custom one: the
  one selected, or else the one open.
- **Import** makes a new Custom profile of a file exported from LMA (see
  [Copying configuration between deployments](#copying-configuration-between-deployments-and-presets)).

The editor's **Actions** menu works on the profile open. Besides export, import
and presets, **Save current edits as new profile** makes a new Custom profile of
what the editor holds, unsaved edits included, and leaves the open profile as it
is saved; **Reset all sections to defaults** does what each section's **Reset
section to defaults** does, for every section, as unsaved changes. The
configuration history is kept per profile, and **Revisions…** opens the history
of the profile open.

The pages that edit one setting outside the Configuration page, such as the
meeting page's chat buttons, save into the active profile, and refuse while the
default or a Managed profile is active: create and activate a Custom profile
first.

## Using the Configuration page

Open **Configuration ▸ View / Edit Configuration** in the navigation, and open a
profile from the list at the top; the editor below the list shows it. Each section is a panel
that says what it controls and when a saved change applies; open one to edit it.
A link to `#/configuration/settings?section=call_pricing` (or any other section's
name) opens that section directly.

**Seeing what you changed.** A value that differs from the release default is
marked **Changed**, or **Added** for an entry the release does not ship, with a
coloured rule beside it, and a section's heading counts how many of its settings
differ. A section with edits you have not saved yet is marked **Unsaved
changes**.

**Putting a default back.** **Reset to default**, beside a changed value, puts
back the shipped value of that one setting. **Reset section to defaults** does it
for a whole section, and **Undo changes** discards what you have edited in a
section since the page loaded. **Discard changes**, at the top, does that for every
section.

**Lists of entries.** The summary sections and the chat buttons are lists you can
add to. A summary section the release ships cannot be deleted, because the next
release would bring it back; **Turn off** it instead, and the summary skips it.
**Turn on** brings back that one section with the shipped prompt. Renaming a
shipped section turns off the old title and adds the new one in the same place.
A chat button list is saved whole: you can remove any button and reorder the list,
and while you have saved no list of your own, the shipped buttons apply.

**Prices.** Prices are listed by region — `default` applies everywhere — then by
service, then by unit. You can change a shipped price, or add a region, service
or unit the table does not list. A shipped entry can be reset but not removed.

**Editing as JSON or YAML.** The **Form / JSON / YAML** switch above the
sections shows the whole profile as one JSON or YAML document, keyed by section
name, each section holding every one of its settings. Edit it there to change
several settings at once or to paste a section from another deployment: what you
type is checked as you type against the same schemas as the form, the problems
are listed under the text, and **Save changes** saves it as one revision as it
would a form edit. A section you leave out of the text keeps its values. In
most sections a setting you remove returns to its default, as **Reset to
default** would; to turn a shipped summary section off, set its prompt to
`NONE`. The chat buttons are the exception: that section is saved as a whole
list, so the buttons the text holds are the list, and removing them all brings
back the shipped buttons. Settings this deployment does not offer are left out
of the text, as the form leaves them out, and are not taken from it if you add
them. While the text does not parse, the page neither saves nor switches view; correct it,
or choose **Discard changes**. The text of the default and Managed profiles is
read-only, as their form is.

**Mistakes are caught as you type.** A value a save would refuse — a negative
price, an empty voice id, a region name that is not a region code — is marked
in red with what is wrong, and the page does not offer to save until it is
corrected.

## What happens when you save

Select **Save changes**. The dialog lists the sections you changed and asks for
two optional things:

- a **comment**, recorded with the save, so the history says why it was made;
- a **label**, which keeps this revision in the history however many saves
  follow it.

**The save is checked before anything is stored.** Every section has a schema
that says which settings it has and what values each accepts. The page checks
your changes against it as you edit, and the server checks again: a save that
breaks a rule is refused as a whole, nothing is stored, and the error names each
setting that was wrong. For example:

```
call_pricing › regions › default › transcribe/streaming › seconds: must be at least 0; got -1
```

Read the path from left to right: the section, then each key down to the setting.
A refusal can name a setting you did not touch: what an upgrade carried across
from an earlier release, and what a restore put back, are stored without this
check, and a later save of that section checks them. Correct the setting it
names, then save again.

**The save is checked against the version you loaded.** All the sections are
stored together, and a save is refused if anyone saved any of them after your
page loaded — on this page, in the Edit Chat Buttons dialog, by a restore from the history, or through the API — rather than silently
replacing their change. The page then offers two ways on:

- **Reload and keep my changes** loads what they saved and applies your changes
  over it. Settings you both changed now hold your value, and the page lists
  them so you can check them. The chat buttons count as one setting: if the
  other save changed them, your whole list is kept. Nothing is saved until you select **Save changes**
  again.
- **Discard my changes and reload** shows the configuration as it is now saved.

**Every save is recorded.** A save that changes something becomes a numbered
revision recording who saved it, when, which sections it changed, and your comment
and label. **Revisions…** opens the revision history of the profile open, where
you can see the revisions, compare any two of them setting by setting,
label or relabel one, or restore an earlier one. A restore puts back every section
as it was in that revision and is itself recorded as a new revision, so it can be
undone the same way. See
[Revisions and audit](configuration-store.md#revisions-and-audit).

## Copying configuration between deployments, and presets

The editor's **Actions** menu offers three things for the profile open. None of
them saves anything: what they change appears on the
page as unsaved changes, marked like your own edits, and is stored only when you
select **Save changes**.

- **Export to a file** downloads the configuration as saved — your changes over
  the release defaults, not the defaults themselves — as a JSON file. Unsaved
  edits on the page are not in it.
- **Import from a file** loads such a file, for example one exported from a
  staging deployment, onto the page. Because the file holds changes rather than
  whole settings, importing it into a deployment of a later release keeps that
  release's defaults for everything the file does not change. A section the file
  holds that this deployment does not have is listed and left out, and so is a
  setting the page does not show here, such as the engine choices on a deployment
  without the on-demand speech engine, which keeps its current value. The save
  comment is filled in with the file's name; change it if you like. To make a
  new profile of the file instead, leaving every existing profile as it is, use
  **Import** in the Configuration Profiles list: it reads the file the same way,
  asks for the new profile's name, and creates it with the file's changes over
  the deployment defaults.
- **Apply a preset…** opens a dialog listing the presets LMA ships, each with
  its description (see [Configuration presets](#configuration-presets)). A
  preset is offered only where the deployment has every setting it changes.
  Once you choose one, the dialog lists the settings it changes in each
  section.

### Configuration presets

A **preset** is a complete organizational default for consent, retention and
privacy. All four set the same settings, listed below, and change only those,
so applying one sets every one of them whichever preset was applied before, and every other setting stays as it is. They are the choices
of the `ConfigurationPreset` stack parameter, and each is also stored as a
read-only **Managed** profile of the same name, which the deployment rewrites on
every stack update; activate it as it is, or copy it to a Custom profile to
change it.

| Setting | Standard (`standard`, the default) | Internal meetings – notify participants (`internal-meetings-notify`) | Regulated – consent required, transcript not retained (`regulated-consent-required`) | Regulated – notify with opt-out, transcript not retained (`regulated-notify`) |
|---|---|---|---|---|
| Meeting Consent › Consent mode | Off | Notify | Opt-in | Notify |
| Meeting Consent › Consent rule (opt-in) | Each participant consents | Each participant consents | Each participant consents | Each participant consents |
| Meeting Consent › Allow a different choice per meeting | On | On | Off (a meeting may only use this mode or a stricter one) | Off (a meeting may only use this mode or a stricter one) |
| Meeting Consent › Keep evidence screenshots | On | On | On | On |
| Meeting Consent › Streaming clients without a consent step | Accept with a warning | Accept with a warning | Refuse the meeting | Refuse the meeting |
| Meeting Consent › Virtual Participant leaves after an objection | Off | Off | On | On |
| Retention › Delete the transcript after the summary | Off | Off | 15 minutes after the summary | 15 minutes after the summary |
| Meeting Recording › Record meeting audio | On | On | Off | Off |
| Virtual Participant › Add the meeting chat to the transcript | On | On | Off | Off |
| Amazon Transcribe › Redact personal information | Off | Off | On | On |
| Amazon Bedrock Models › Model inference geography | Global | Global | Same geography as this deployment | Same geography as this deployment |

Under the Regulated – consent required preset (`regulated-consent-required`)
nothing is recorded or kept until each participant
consents. While it waits, a Virtual Participant listens for consent said aloud:
the meeting audio is processed by the meeting's transcription engine, and only
the reply events are kept on the consent record. Turn **Meeting Consent › Read
spoken replies** off to have it read the chat only and send no meeting audio
anywhere before consent; the presets leave that setting as it is (see
[Spoken replies](meeting-consent.md#spoken-replies)). Stream Audio and the Desktop Capture App cannot collect
each participant's consent, so Start is refused from them; use a Virtual
Participant on a join that supports each-participant consent. A Virtual
Participant on the Webex join cannot tell who is present, so it never meets the
each-participant rule, and with a different choice per meeting turned off the
meeting cannot move to the less strict owner-attests rule, so such a meeting is
not transcribed. On the Teams browser join, which shows no chat sender, the rule
is met only by consent said aloud. See
[What each platform supports](meeting-consent.md#what-each-platform-supports).

The Regulated notify preset keeps the Regulated preset's retention and privacy
settings but uses Notify: recording and transcription run from the start, the
participants are told with the notice, and an objection stops them. A meeting
may use Notify or the stricter Opt-in, not Off. A Virtual Participant posts the
notice in the meeting chat and reads objections there, and those said aloud and
addressed to LMA (read from the meeting's own transcription, unless **Read
spoken replies** is off), on every join, and it leaves the meeting after an
objection, so nothing is recorded or kept after it. Stream Audio and the Desktop Capture App
show the notice for the LMA user to share in the meeting and capture from
Start; an objection takes effect when the user records it with **Participant
objected**. Clients that show no consent step (the browser extension, the
Stream Audio embed and Desktop Capture App builds from before the consent step)
are refused, as under Regulated. See
[Stream Audio and the Desktop Capture App](meeting-consent.md#stream-audio-and-the-desktop-capture-app).

Standard holds the shipped defaults, so applying it in the editor puts these
settings back after another preset changed them. The layer's tests
hold each preset to exactly the values in this table.

Some controls are stack parameters rather than settings. They are not part of
any profile, so no preset can set them, and a deployment that needs the
posture of either Regulated preset also sets them when the stack is deployed or updated:

- `LogLevel` — `INFO` (the default) or `WARN`. `DEBUG` logs meeting content.
- `MeetingRecordExpirationInDays` and `TranscriptionExpirationInDays` — the
  longest meeting records and transcript segments are kept, which the Retention
  section can shorten but not lengthen (see
  [Retention: settings the stack limits](#retention-settings-the-stack-limits)).
- `AudioRecordingExpirationInDays` and `CloudWatchLogsExpirationInDays` — how
  long recordings and logs are kept.
- `EnableVideoRecording` — meeting video from the Virtual Participant and the
  Desktop Capture App. A Virtual Participant's video is also chosen per meeting.

Where each service that receives meeting content runs is listed in
[Where meeting content is processed](infrastructure-and-security.md#where-meeting-content-is-processed).
The consent settings are described in [Meeting Consent](meeting-consent.md), and
the deletion in [Transcript Deletion After the Summary](transcript-deletion.md).

To start a new deployment from a preset, or from a configuration
file of your own, set the `ConfigurationPreset` and `CustomConfigPath` stack
parameters; see
[Choosing the default configuration at deploy time](configuration-store.md#choosing-the-default-configuration-at-deploy-time).

## Changing a setting outside the web UI

The page saves through the `updateConfiguration` GraphQL operation, which an
Admin can also call directly, with the same checks. It and the older per-section
operations are described in the
[Unified Configuration Store](configuration-store.md) reference. Editing the
configuration table in the DynamoDB console does not work: the records are
stored compressed, and an attribute added beside a compressed record is ignored.

## Notes on specific settings

### Choosing Amazon Bedrock models

**Summaries and meeting insights** (summaries, pasted meeting invitations, the speaker-naming pass,
agenda derivation and the transcript knowledge-base query) and
**Meeting assistant** (the meeting assistant and the
troubleshooting agent) offer the same list but have different defaults, because
they are chosen for different jobs. The first defaults to
`global.anthropic.claude-sonnet-5-5`: summarisation is batch work where the
quality of the answer is what matters. The second
defaults to `global.anthropic.claude-haiku-5-5`, run at a low reasoning effort
(**Meeting Assistant › Reasoning effort**). The assistant answers mid-meeting and
is sometimes spoken aloud, so its default is chosen on answer quality, time to the
first word and cost together. Over thirteen meeting-assistant requests against
real meeting transcripts, Claude Haiku 5.5 at low effort gave the answer judged
better than Claude Haiku 4.5's on ten, at about a tenth of the cost, because its
per-token prices are a tenth of Haiku 4.5's. The trade-off is latency: its median
time to the first word of an answer was 3.3 s, against 2.1 s for Haiku 4.5, since
the Claude 5 generation reasons before it answers. A deployment where the fastest
reply matters more than answer quality can choose
`global.anthropic.claude-haiku-4-5-20251001-v1:0` instead.

Summaries cost more on Sonnet 5.5 than on Haiku 4.5 — roughly 27% more input
tokens, and two to three times the output tokens, for a noticeably longer
summary. The Claude 5 generation reasons before it answers and the reasoning is
billed as output, so one summary of the same meeting consumes roughly three to
four times the output tokens it does on Claude 4.5 at the same size, and its
tokenizer counts a given transcript as roughly a quarter more input tokens. If
cost matters more than the last increment of quality, use
`global.anthropic.claude-haiku-4-5-20251001-v1:0`.

**Meeting assistant knowledge bases** offers a shorter list. It names the model the
meeting assistant's knowledge bases generate an answer with, and a Bedrock
Knowledge Base can be driven with fewer models than the agent itself runs on —
per individual model, not by family or version — so the list is a record of what
has been verified rather than a rule. See
[The knowledge-base model](meeting-assistant.md#the-knowledge-base-model).

**Key frame text** offers only image-capable models, because the model reads a
picture of the shared screen. See [Meeting Video and Key Frames](meeting-video.md).

A `global.` profile routes to whichever region has capacity; a `us.` profile keeps
inference within the United States. Pick the one your data-residency position
allows. Whichever you choose, enable access to that model in the Amazon Bedrock
console for the region you deploy into first — see
[Amazon Bedrock Model Access](prerequisites-and-deployment.md#amazon-bedrock-model-access).
The Configuration page offers only the models listed here.

### Teams meeting-end detection

The Virtual Participant section's three **Teams** settings (milliseconds between meeting checks, and the two counts of checks before leaving) apply to the
Teams browser join path only, and no deployment should normally need to change
them — see
[How the VP decides a meeting has ended](virtual-participant.md#how-the-vp-decides-a-meeting-has-ended)
for what each signal measures and why the two thresholds differ so much.

### Closing meetings that were never ended

**Meeting Processing › Minutes without speech before a meeting is ended** backs the scheduled reaper that ends meetings
whose client disconnected without sending its end-of-meeting event; otherwise
those meetings show as "In Progress" for as long as their record is retained. The
reaper runs every 15 minutes, so a meeting can remain in progress for the timeout
plus one interval. Its liveness signal is the meeting record's last update, which
a transcript segment refreshes, so a meeting that is still connected but has
produced no finalized speech for longer than the timeout is also ended; keep the
value comfortably longer than the longest quiet stretch your meetings have.
Meetings still being processed by the upload pipeline are left to the upload
finalizer. The reaper is always deployed; a timeout of 0 makes it end nothing.

**Days to look back for meetings that were never ended** bounds how much history each run examines. A
meeting that started before the window is never closed automatically, so if you
are updating a deployment that has accumulated older meetings stuck in progress,
raise it once on the Configuration page, let a run
complete, and put it back; no stack update is needed. Each run also stops after
reading 500 candidate meetings, newest first. See
[Troubleshooting → Meeting Stuck In Progress](troubleshooting.md#meeting-stuck-in-progress).

## Related

- [Unified Configuration Store](configuration-store.md) — the storage, caching,
  validation and revision history behind these settings
- [Transcript Summarization](transcript-summarization.md),
  [Nova Sonic 2 Setup](nova-sonic-setup.md), [MicroVM ASR](microvm-asr.md) and
  [Meeting Cost Estimate](meeting-cost-estimate.md) — what each section's
  settings do
