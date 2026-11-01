---
title: "Configuration Guide"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Configuration Guide

This page is for administrators. It explains which of LMA's settings you can
change from the web UI while the stack is running, how to change them on the
**Configuration** page, what happens when you save, and what to do when a save is
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

The runtime settings are grouped into sections. Each section has a default and
your changes over it. Only what you change is stored, so a setting you never
touched keeps following its default. For most settings the default ships with
LMA and is updated by later releases. For some, the default is the value of a
stack parameter; see
[Settings whose default is a stack parameter](#settings-whose-default-is-a-stack-parameter).

| Section | What it controls | A saved change applies |
|---|---|---|
| Transcript Summary | The sections of the end-of-meeting summary and the prompt behind each | To summaries generated within about five minutes of the save. **Regenerate** on a meeting page uses it at once |
| Meeting Assist Chat Buttons | The quick-action buttons in the chat panel. A saved list replaces the shipped buttons as a whole | At once, to chat panels opened after the save |
| Nova Sonic Voice Assistant | The voice assistant's system prompt and prompt mode, model, voice, turn-taking sensitivity, meeting mode and translator settings | To the next meeting the voice assistant joins |
| Transcription Engine | Virtual Participant voice separation, which works on Amazon Transcribe, and, where the experimental on-demand speech engine is deployed, which engine transcribes streaming meetings and which transcribes Virtual Participants. Without that engine only the voice separation switch is shown | To meetings that start within 30 seconds of the save or later |
| Meeting Cost Prices | The unit prices the per-meeting [cost estimate](meeting-cost-estimate.md) is computed with | To estimates drawn within about five minutes of the save |
| Amazon Bedrock Models | The Amazon Bedrock model used for summaries and meeting insights, for the meeting assistant and the troubleshooting agent, for the meeting assistant's knowledge bases, and for reading the text of key frames | To each summary, answer or key frame started about five minutes after the save or later |
| Amazon Transcribe | The Amazon Transcribe options of meetings streamed from the Stream Audio page, the browser extension and the desktop app: language or language identification, redaction, custom vocabulary and language model, and the default for identifying speakers within a channel | To streaming meetings that start after the save |
| Meeting Recording | Whether those meetings' audio is recorded when the client does not say | To streaming meetings that start after the save |
| Meeting Assistant | The regular expression that wakes the meeting assistant in a transcript | To transcript segments processed about five minutes after the save or later |
| Meeting Processing | Whether a transcript Lambda hook is sent partial segments, and how far back the inactive-meeting reaper looks | Within about five minutes of the save |

Every section is on **Configuration ▸ Configuration**. The pages that each edited
one section — Transcript Summary, Nova Sonic Config and Transcription Engine — are
now sections of it, and a bookmark to one of them opens its section. The **Edit
Chat Buttons** dialog in a meeting's chat panel stays where it is and saves into
the same configuration.

Only members of the **Admin** Cognito group can change any of these. Add someone
to the group on [User Management](user-management.md) if they need to.

## Settings whose default is a stack parameter

Some settings used to be chosen only with a stack parameter. They are now on the
Configuration page, and the stack parameter is kept as the setting's
**default**:

- A new deployment shows the values you gave the parameters, marked as
  defaults, and uses them until you change them on the page.
- An existing deployment that is updated keeps the values its parameters had:
  the stack update writes them into the configuration as the defaults. Nothing
  needs to be done on upgrade.
- A value you save on the page takes precedence over the parameter. **Reset to
  default** puts the parameter's value back.
- A later stack update that changes the parameter changes the default. That
  applies wherever the setting has not been changed on the page; where it has,
  the saved value stays in force until you reset it. If a stack update seems to
  have had no effect on one of these settings, look for a **Changed** mark on
  the page.

The parameters are not removed, so existing deploy commands and pipelines that
pass them keep working. Each one's entry in
[CloudFormation Parameters](cloudformation-parameters.md) names the setting it
is the default of.

| Section › setting | Default from |
|---|---|
| Amazon Bedrock Models › Summaries and meeting insights | `BedrockModelId` |
| Amazon Bedrock Models › Meeting assistant | `MeetingAssistServiceBedrockModelID` |
| Amazon Bedrock Models › Meeting assistant knowledge bases | `MeetingAssistKnowledgeBaseModelID` |
| Amazon Bedrock Models › Key frame text | `KeyframeTextModelId` |
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

The Amazon Transcribe and Meeting Recording settings apply to meetings streamed
to LMA from the Stream Audio page, the browser extension and the desktop app.
A Virtual Participant reads the stack parameters, as it always has.

Some things differ from setting these with the parameters:

- While the stack deploys, it checks that the parameters' Bedrock models can be
  invoked (unless `ModelValidation` is `false`). A model chosen on the page is
  not checked, so enable access to it in the Amazon Bedrock console first.
- While the stack deploys, it also checks that the parameters' Amazon Transcribe
  options fit together: language identification needs language options, at most
  one dialect per language, a preferred language among them, and no custom
  vocabulary or language model. The page cannot check that across settings, so
  the WebSocket transcriber does as each meeting starts: options saved on the
  page that do not fit together are logged, and the meeting is transcribed with
  the deployment's parameter values for the language, language options,
  preferred language, custom vocabulary and custom language model until they are
  corrected. Redaction and recording keep the values saved on the page.
- If the WebSocket transcriber cannot read the configuration when a meeting
  starts, that meeting uses the stack parameters' values for all of these
  settings, redaction and recording included, and the next meeting reads the
  configuration again.
- Nothing checks that a custom vocabulary or language model exists. Amazon
  Transcribe checks when a meeting starts, and a meeting whose vocabulary or
  model it does not recognise is not transcribed, so make sure the name exists
  in this account and region, and matches the language, before saving it.
- Two optional transcriber features outside the default deployment, Call
  Analytics' post-call output and the PCA integration, follow the
  `IsContentRedactionEnabled` parameter rather than the page, because they have
  to agree with each other about where redacted output goes.
- The page offers the same choices the parameter does. A model a later release
  stops offering stays in force where it was saved, and the next save of the
  section asks for another.

## Using the Configuration page

Open **Configuration ▸ Configuration** in the navigation. Each section is a panel
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
and label. **View history** opens **Configuration ▸ Configuration History**,
where you can see the revisions, compare any two of them setting by setting,
label or relabel one, or restore an earlier one. A restore puts back every section
as it was in that revision and is itself recorded as a new revision, so it can be
undone the same way. See
[Revisions and audit](configuration-store.md#revisions-and-audit).

## Copying configuration between deployments, and presets

The **Import, export and presets** menu at the top of the Configuration page
offers three things. None of them saves anything: what they change appears on the
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
  comment is filled in with the file's name; change it if you like.
- **Apply a preset** applies one of the configurations LMA ships:
  - **Healthcare summary** — the summary sections of the healthcare prompt domain,
    with the other shipped sections turned off;
  - **Translator voice assistant** — Nova Sonic's Translator meeting mode between
    English and Spanish;
  - **On-demand speech engine with voice separation** — offered only where the
    engine is deployed. A preset is offered only where the deployment has every
    setting it changes.

  The dialog says which sections a preset changes, and whether it replaces a
  section or changes only the settings it names.

## Changing a setting outside the web UI

The page saves through the `updateConfiguration` GraphQL operation, which an
Admin can also call directly, with the same checks. It and the older per-section
operations are described in the
[Unified Configuration Store](configuration-store.md) reference. Editing the
configuration table in the DynamoDB console does not work: the records are
stored compressed, and an attribute added beside a compressed record is ignored.

## Related

- [Unified Configuration Store](configuration-store.md) — the storage, caching,
  validation and revision history behind these settings
- [Transcript Summarization](transcript-summarization.md),
  [Nova Sonic 2 Setup](nova-sonic-setup.md), [MicroVM ASR](microvm-asr.md) and
  [Meeting Cost Estimate](meeting-cost-estimate.md) — what each section's
  settings do
