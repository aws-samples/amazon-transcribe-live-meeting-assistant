---
title: "Transcript Summarization"
---

# Transcript Summarization

## Table of Contents

- [Overview](#overview)
- [Regenerating and Editing a Summary](#regenerating-and-editing-a-summary)
  - [Regenerate](#regenerate)
  - [Edit](#edit)
  - [Who Can Do It](#who-can-do-it)
- [BEDROCK Option (Default)](#bedrock-option-default)
  - [Prompt Template Storage](#prompt-template-storage)
  - [Attribute Naming Convention](#attribute-naming-convention)
  - [Customizing Prompts](#customizing-prompts)
  - [Admin UI](#admin-ui)
- [LAMBDA Option](#lambda-option)
  - [Lambda Function Requirements](#lambda-function-requirements)
  - [Multi-Section Summaries](#multi-section-summaries)
  - [Minimal Python Example](#minimal-python-example)
- [FetchTranscript Utility Lambda](#fetchtranscript-utility-lambda)
- [Related Documentation](#related-documentation)

## Overview

LMA summarizes meeting transcripts automatically when a meeting ends, and on-demand via UI buttons during or after meetings. Two summarization options are available: **BEDROCK** (default) and **LAMBDA**.

A meeting's summary is generated once, from the transcript and the prompt templates as they were at the time. The Meeting Summary panel on the meeting page lets you run it again, or write the summary yourself.

## Regenerating and Editing a Summary

### Regenerate

**Regenerate** on the Meeting Summary panel re-runs summarization against the transcript and the prompt templates **as they are now**. Use it when the stored summary no longer reflects the meeting:

- Someone has named the separated speaker voices on the [Speakers panel](transcription-and-translation.md#naming-the-voices), so the transcript now reads "Sarah Lee" where it read `spk_0`. The summary was written before those names existed.
- An administrator has changed the summary prompt templates, by adding a section or rewording one.
- The meeting was summarized while it was still running, and has since continued.

Regenerating goes through the same summarizer the end-of-meeting summary uses -- the Bedrock summarizer, or your own function under the LAMBDA option -- with the meeting id and a request to re-read configuration, so it reads the current transcript and the current prompts rather than a copy of either. The Bedrock summarizer caches the prompt templates for a few minutes, which is right for the end-of-meeting runs it does unattended; that request is what makes **Regenerate** pressed straight after a save use the prompt that was just saved.

Under the BEDROCK option it also **refreshes the Bedrock Knowledge Base snapshot**. The copy of the transcript and summary written to S3 for the Knowledge Base to ingest is taken when the summary runs, and the Meeting Assistant's meeting-history search reads that copy; regenerating writes it again with whatever speaker names are stored now. It is the summarizer that writes that copy, so under the LAMBDA option the snapshot is refreshed only if your own function writes it. Ingestion into the Knowledge Base itself runs on a schedule the stack fixes at every 15 minutes, so the refreshed copy becomes searchable at the next sync rather than immediately.

Four things are worth knowing about how it behaves:

- **It is not instant.** Summarization calls the model once per prompt template, which routinely takes longer than a web request allows, so pressing the button starts a run in the background and the panel shows **Regenerating** until the new summary arrives. The new summary appears by itself -- there is nothing to reload.
- **Only one run at a time.** While a run is going the button is disabled, including in another browser tab and for another user looking at the same meeting. A run that never reports back stops blocking new ones after twenty minutes. It is a guard against a second click rather than a lock, so two presses in the same instant can both get through; the cost of that is a duplicated set of model calls, and the later result wins. For the same reason, a summary edited by hand while a run is going is replaced by that run's result.
- **A failed run keeps the previous summary.** If the model cannot be reached or the summarizer returns nothing usable, the panel shows the error and the stored summary is left exactly as it was. A summary that was there is never replaced by an error message.
- **A run the meeting outlives is discarded.** You can regenerate while a meeting is still going -- that is one of the reasons above -- but such a run summarizes the transcript as it stood when the run started. If the meeting ends before the run finishes, the summary it produced is thrown away rather than stored, because the end-of-meeting summary covers the whole transcript and this one would not. The Knowledge Base copy is the exception: under the BEDROCK option the summarizer writes that to S3 itself as it runs, so it can be left holding the partial summary. **Press Regenerate once more after the meeting has ended** if you regenerated during it and rely on meeting-history search; that writes both the stored summary and the Knowledge Base copy from the whole transcript.

### Edit

**Edit** opens the summary as text you can change, and **Save summary** stores it. What you save is what every reader of the stored summary then shows: the meeting page, the summary download, the Word and email exports, and the embeddable meeting panel.

**Edit** revises an existing summary rather than writing one from nothing, so it needs a summary to be there: on a meeting that has never been summarized -- under the LAMBDA option with no hook function configured, for instance -- the button is disabled.

It does **not** reach the copy of the summary written to S3 for the Bedrock Knowledge Base, which is the copy the Meeting Assistant's meeting-history search reads. That copy is written by the summarizer, so only a summary run refreshes it: a search across past meetings keeps returning the generated summary until you press **Regenerate**, which replaces your edit. **Edit** is available once a meeting has ended. While a meeting is still running the button is disabled, because the end-of-meeting summary would replace what you wrote when the meeting ends. **Wait for that summary to appear before editing.** The meeting is marked as ended as soon as the meeting itself finishes, but its summary takes a few minutes longer -- the speaker-naming pass and a model call per prompt template run first -- and an edit saved before it arrives is replaced by it, without the confirmation that **Regenerate** asks for. The panel also goes on showing the **Edited** badge over that generated text until the summary is edited or regenerated again.

The editor is seeded with the summary as you are reading it, in Markdown. A summary produced from several prompt templates is stored as a structured object with one entry per section; saving an edit replaces that with the plain Markdown document, which is what a hand-written summary is. The section headings are ordinary Markdown bold text in the editor and you can add, remove or reword them.

A hand-edited summary is marked **Edited** on the panel, and regenerating one asks for confirmation first, because regenerating replaces it and the edited text cannot be recovered. Regenerating a generated summary does not ask.

### Who Can Do It

Both controls are available to a meeting's **owner**, a user the meeting has been **shared with**, and an **administrator** -- the same rule that decides who can see a meeting at all and who can name its speakers. For anyone else both controls appear disabled, and the write is refused by the API even if it is attempted directly.

## BEDROCK Option (Default)

The BEDROCK option uses the selected Amazon Bedrock foundation model (default: Claude Haiku 4.5) to generate meeting summaries from prompt templates. Set the model with the **BedrockModelId** parameter; see [CloudFormation Parameters](cloudformation-parameters.md#end-of-call-summary) for the full list of choices and what the Claude 5 generation costs.

### Prompt Template Storage

Prompt templates live in the stack's shared configuration table, as two records:

1. **The deployment baseline** (`Config#default`) -- the prompts this release ships. CloudFormation rewrites it whenever a release changes the shipped prompts, so an edit made here is replaced the next time that happens and belongs in your profile instead. The stack output **LLMDefaultPromptSummaryTemplate** opens it in the DynamoDB console.
2. **Your profile** (`Config#custom`) -- the prompts an administrator has edited. Written only when it does not already exist, so it survives every stack update and every upgrade. The stack output **LLMCustomPromptSummaryTemplate** opens it.

Your profile is a *delta*: it holds only the sections you changed, and everything else keeps coming from the baseline, so a release that improves a prompt you never touched still reaches you. Where both hold the same section name, yours is the one used.

**Prompts are edited in the web UI, not in DynamoDB.** Each record stores its prompts compressed into a single `Body` attribute, so an attribute added beside it in the console has no effect at all — the summarizer does not read it. The console links above are for seeing what is stored and who last saved it; the Transcript Summary page below is the way to change it, and it is also the only path that refuses a save somebody else has superseded.

See [Unified Configuration Store](configuration-store.md) for the key layout, the caching tiers and the conditional-save behaviour.

### Attribute Naming Convention

Summary attributes use the `N#Label` format, where:

- **N** is a sequence number that controls the display order in the UI.
- **Label** is the human-readable name shown as the section heading.

Examples:
- `1#Summary`
- `2#Action Items`
- `3#Key Decisions`

### Customizing Prompts

Each attribute's value contains the prompt template text that instructs the model how to generate that section of the summary.

- Use the `{transcript}` placeholder in your prompt template where the meeting transcript should be inserted.
- Use the `{agenda}` placeholder where the meeting's [agenda](meeting-agenda.md) should be inserted, as numbered lines. A section whose prompt names `{agenda}` is generated only for a meeting that has an agenda, and left out of the summary of one that does not. The shipped **AGENDA COVERAGE** section uses it; see [Agenda coverage in the summary](meeting-agenda.md#agenda-coverage-in-the-summary).
- Use the `{slides}` placeholder where the meeting's slides should be inserted: each [key frame of the shared screen](meeting-video.md) with its time, a picture reference, its text and what was said while it was on screen. A section whose prompt names `{slides}` is generated only for a meeting that has key frames. The shipped **SLIDES PRESENTED** section uses it; see [Slides presented in the summary](meeting-video.md#slides-presented-in-the-summary).
- Use `<br>` for newlines within the prompt template.

The edits below are all made on the **Transcript Summary** page, which is at:

```
/#/configuration/transcript-summary
```

**To add a new summary section:** add a template on the page and give it a label. It is stored under the next sequence number (for example `4#Follow-Up Questions`).

**To override a default section:** edit the prompt of the section with that name. The page starts from the baseline's sections, so changing one and saving stores your version of it in your profile.

**To remove a default section:** set its prompt to `NONE`, or leave it empty. The summarizer skips a section whose prompt is empty or `NONE`, and skipping it also saves a model call.

**To go back to the shipped prompts:** **Reset to Defaults** clears your profile, so every section comes from the baseline again.

Saving requires membership of the **Admin** group, and each save records which account made it.

## LAMBDA Option

The LAMBDA option lets you provide your own Lambda function to generate meeting summaries with complete control over the summarization logic.

### Lambda Function Requirements

Configure the LAMBDA option by setting the **EndOfCallLambdaHookFunctionArn** CloudFormation parameter to the ARN of your Lambda function.

Your Lambda function:
- Receives the `CallId` in the event payload. When the run comes from the **Regenerate** button, the payload also carries `"RefreshConfiguration": true`, which asks the function to re-read any configuration it caches rather than use a cached copy. A function that caches nothing can ignore it, but one that validates its input against a fixed schema must allow the extra field, or every Regenerate will fail.
- Must return a JSON object with a `summary` key: `{"summary": "..."}`.
- The summary value supports Markdown formatting.

### Multi-Section Summaries

To return a summary with multiple sections, return a JSON-encoded string as the `summary` value. The string should contain key-value pairs where keys are section labels and values are the section content. LMA will parse this and display each section separately in the UI.

### Minimal Python Example

```python
import json

def lambda_handler(event, context):
    print(json.dumps(event))
    call_id = event.get("CallId")
    # Implement your summarization logic here
    summary = "Placeholder for actual summary"
    return {"summary": summary}
```

## FetchTranscript Utility Lambda

LMA provides a utility Lambda function for retrieving processed meeting transcripts. This is especially useful when building custom summarization Lambda functions.

The function ARN is available in the stack output **FetchTranscriptLambdaArn**.

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `CallId` | String | Yes | The unique identifier of the meeting/call. |
| `ProcessTranscript` | Boolean | No | When `true`, condenses sequential utterances from the same speaker, removes filler words, and strips HTML tags. |
| `TokenCount` | Integer | No | Trims the returned transcript to approximately N tokens. Useful for staying within model context limits. |

**Example invocation payload:**

```json
{
  "CallId": "2359fb61-...",
  "TokenCount": 1024,
  "ProcessTranscript": true
}
```

## Related Documentation

- [Meeting Assistant](meeting-assistant.md)
- [Lambda Hook Functions](lambda-hook-functions.md)
