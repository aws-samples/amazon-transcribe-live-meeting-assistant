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
  - [Chat and Shared Links](#chat-and-shared-links)
  - [Meeting Content Is Quoted Data](#meeting-content-is-quoted-data)
  - [Prompt Caching](#prompt-caching)
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

The BEDROCK option uses the selected Amazon Bedrock foundation model (default: Claude Sonnet 5.5) to generate meeting summaries from prompt templates. The model is **Summaries and meeting insights** in the Amazon Bedrock Models section of the [Configuration page](configuration-guide.md), which an Admin can change while the stack runs, with no stack update. See [CloudFormation Parameters](cloudformation-parameters.md#choosing-amazon-bedrock-models) for the full list of choices and what the Claude 5 generation costs.

### Prompt Template Storage

Prompt templates live in the stack's shared configuration table, as two records:

1. **The deployment baseline** (`Config#default`) -- the prompts this release ships. CloudFormation rewrites it whenever a release changes the shipped prompts, so an edit made here is replaced the next time that happens and belongs in your profile instead. The stack output **LLMDefaultPromptSummaryTemplate** opens it in the DynamoDB console.
2. **Your profile** (`Config#custom`) -- the prompts an administrator has edited. Written only when it does not already exist, so it survives every stack update and every upgrade. The stack output **LLMCustomPromptSummaryTemplate** opens it.

Your profile is a *delta*: it holds only the sections you changed, and everything else keeps coming from the baseline, so a release that improves a prompt you never touched still reaches you. Where both hold the same section name, yours is the one used.

**Prompts are edited in the web UI, not in DynamoDB.** Each record stores its prompts compressed into a single `Body` attribute, so an attribute added beside it in the console has no effect at all — the summarizer does not read it. The console links above are for seeing what is stored and who last saved it; the **Transcript Summary** section of the Configuration page, below, is the way to change it, and it is also the only path that refuses a save somebody else has superseded.

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
- Use the `{slides}` placeholder (or `{screen_share}`, another name for it) where what was shared on screen should be inserted: each [key frame of the shared screen](meeting-video.md) with its time, a picture reference, its text and what was said while it was on screen. A section whose prompt names either is generated only for a meeting that has key frames. The shipped **SCREEN SHARE SUMMARY** section uses it; see [Screen share summary](meeting-video.md#screen-share-summary).
- Use the `{timed_transcript}` placeholder for the transcript with the time of each line, and a line where the shared screen changed. The shipped **CHAPTERS** section uses it, and the meeting page reads that section's `- [MM:SS] Title` lines as the meeting's chapters; see [Chapters](meeting-video.md#chapters).
- Use the `{chat}` placeholder for the messages typed in the meeting chat, which a [Virtual Participant](virtual-participant.md#meeting-chat-in-the-transcript) adds to the transcript: one `[MM:SS] <sender>: <message>` line per message, in the order sent. A section whose prompt names `{chat}` is generated only for a meeting whose transcript holds chat. The shipped **CHAT & SHARED LINKS** section uses it; see [Chat and Shared Links](#chat-and-shared-links).
- Use the `{chat_links}` placeholder for the list of links shared in the chat, in the form shown under [Chat and Shared Links](#chat-and-shared-links), or *No links were shared in the chat.* It is likewise used only for a meeting with chat, and a section that names it is handled as a chat section.
- Use `<br>` for newlines within the prompt template.

In `{transcript}` and `{timed_transcript}`, each chat message also appears among the speech at the time the VP read it, as a line that begins `[chat]`: `[chat] Alice: the deck is on the team site`. Every shipped prompt that reads either of them says so: **SUMMARY**, **DETAILS**, **ACTIONS**, **AGENDA COVERAGE** and **CHAPTERS** in the default templates, and **SUMMARY**, **DETAILS**, **SOAP** and **BIRP** in the healthcare ones. A message's line breaks are written as ` / ` and its `<` and `>` as `‹` and `›`, so a message stays on its one line and cannot close the `<transcript>` or `<chat>` tags around it.

The edits below are all made in the **Transcript Summary** section of **Configuration ▸ Configuration**, which a link can open directly:

```
/#/configuration/settings?section=summary_prompt_templates
```

The earlier address, `/#/configuration/transcript-summary`, opens the same section.

**To add a new summary section:** select **Add summary section** and give it a title. It is stored under the next sequence number (for example `8#Follow-Up Questions`).

**To override a default section:** edit the prompt of the section with that name. The section is marked **Changed**, and only your version of it is stored in your profile.

**To turn off a default section:** select **Turn off**. It is stored as `NONE`, the summarizer skips it, and skipping it also saves a model call. **Turn on** brings back that one section with the shipped prompt. A section whose prompt is empty is skipped too.

**To go back to the shipped prompts:** **Reset to default** on one section, or **Reset section to defaults** for all of them; then save. See the [Configuration Guide](configuration-guide.md).

Saving requires membership of the **Admin** group, and each save records which account made it.

### Chat and Shared Links

A meeting whose transcript holds chat gets a **CHAT & SHARED LINKS** section (`7#CHAT & SHARED LINKS`): at most eight bullet points on what was discussed in the chat, followed by the links participants shared. A meeting without chat does not get the section and costs no model call for it. When the chat holds nothing beyond greetings or reactions, the section reads *Nothing of substance was discussed in the chat.*

The links are not written by the model. The model is asked not to write out any address, and what it returns for a section whose prompt names `{chat}` or `{chat_links}` then has every link taken out by code: a Markdown link or picture keeps only its text, an address written out (with any scheme, or starting `www.`, or a lower-case domain name followed by a path, such as `docs.example.com/page`, where the domain has at least two dots or ends in a common top-level domain such as `.com`, `.org`, `.io` or `.dev`) becomes `(link)`, while a name such as `Node.js/TypeScript` or a label such as `**File:**` is left as written, and HTML tags and reference-style link definitions are removed. The same is done to every other section of a meeting with chat, because the transcript those sections read holds the chat's `[chat]` lines; only a key-frame picture (`![…](keyframe:…)`, which names a frame of the shared screen rather than an address) is kept, with any address in its text removed. A meeting without chat keeps its sections as the model wrote them. So in a meeting with chat, the only addresses in its summary are the ones listed at the end of this section, as they were found in the stored messages:

```markdown
**Links shared**
- [00:20] Alice: `https://example.com/deck`
- [04:05] Bob: `https://www.example.com/notes`
```

or with *No links were shared in the chat.* Each address is listed once, under the person who shared it first and at that time, in code formatting so that it is shown as text to copy rather than as a link to follow. An address is found by these rules, which the meeting page applies to chat lines too: it starts `http://`, `https://` or `www.` (given `https://`); trailing punctuation (including curly quotes and backticks) and an unmatched closing bracket are not part of it; only `http` and `https` addresses with a host are kept, up to 2,048 characters; and a backtick inside it is written `%60`. Up to 50 links are listed, then the number of the rest. A link the VP added after a message as `(link: <address>)` is listed like one typed in the message.

The Healthcare summary preset and a stack deployed with the healthcare prompt domain do not include the section. Every other profile gets it as it gets any section a release adds: a profile is merged over the baseline when it is read, so a Custom profile that does not hold the section has it on, including one copied from the Healthcare summary profile before this release. A Custom profile copied from that profile later holds it turned off. Turn it off or on in the **Transcript Summary** section like any other. See [Named profiles](configuration-store.md#named-profiles).

### Meeting Content Is Quoted Data

What a summary is written from comes from the people in the meeting: what they said, what they typed in the chat, and what their shared screen showed. Every request to the model carries a system prompt saying that this content is quoted data to summarise, that an instruction appearing in it is never to be followed, and that a transcript line beginning `[chat]` was typed rather than spoken. Every model the Configuration page offers for summaries takes a system prompt; a model that refuses one is sent the same text at the head of its prompt instead. This makes prompt injection through meeting content less likely to succeed; it does not rule it out, which is why, in a meeting with chat, links are taken out of every section and the chat section's links are listed by code rather than by the model.

### Prompt Caching

Each section of a summary is a separate model call, and the shipped **SUMMARY**, **DETAILS**, **ACTIONS** and **AGENDA COVERAGE** prompts all begin with the same words followed by the whole quoted transcript. The summarizer sends that shared beginning once at the full input price and lets the other sections read it from the [Amazon Bedrock prompt cache](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html) at a tenth of the input price or less (a twentieth for Claude Sonnet 5.5, in the shipped price table):

- Each prompt template is divided after its first placeholder, and after the closing tag that follows it if there is one (`</transcript>` in the shipped prompts). The part before that point, filled in for the meeting, is the section's prefix; the rest is its instruction.
- When two or more sections of one summary have the same prefix, word for word, each of them is sent as the prefix, a cache point, and the instruction, as separate text blocks of the one user message. The first such section writes the prefix and the system prompt ahead of it to the cache, and the others read them.
- A section whose prefix no other section shares is sent as one text block, as before, because a cache write is billed at 1.25 times the input price and nothing would read it. With the shipped prompts that is **CHAPTERS** (which quotes the timed transcript), **SCREEN SHARE SUMMARY** and **CHAT & SHARED LINKS**. So is a prompt with no placeholder, or with nothing after its first one.

The model is asked exactly what it was asked before: the blocks carry the prompt's own text in its own order, and a custom prompt needs no change to benefit, provided it begins the same way as another section's. Caching applies to the summary models that take a cache point in the user message, which are the Claude 4 and later models; the Amazon Nova models are sent each prompt whole. A prefix shorter than the model's minimum cacheable length (from 512 to 4,096 tokens, depending on the model) is answered normally and nothing is cached.

Measured on the transcript of a real 57-minute standup (about 27,500 tokens) with Claude Sonnet 5.5 and the shipped prompts, the meeting had no agenda, slides or chat, so four sections ran. Without caching the four calls sent 114,663 input tokens, about $0.23 of input. With caching the same 114,663 tokens were billed as 27,558 cache-write tokens on **SUMMARY**, 55,116 cache-read tokens on **DETAILS** and **ACTIONS**, and 31,989 ordinary input tokens (almost all of them **CHAPTERS**), about $0.14 of input, a reduction of about 40%. Output, about 4,100 to 4,500 tokens in both runs, is unaffected, so the whole summary went from about $0.27 to about $0.18 in that run. A meeting with an agenda adds a fourth section reading the cache, and saves more. The time each section took was unchanged within the run-to-run variation, since it is dominated by the model's output. The cache tokens are recorded in the meeting's [cost estimate](meeting-cost-estimate.md) at their own prices.

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
| `IncludeChat` | Boolean | No | When `true`, the messages typed in the meeting chat are interleaved with the speech as `[chat] <sender>: <message>` lines (`[chat] <message>` without `IncludeSpeaker`), and the response also carries `chat`, one `[MM:SS] <sender>: <message>` line per message, and `chatLinks`, a list of `{"url", "sender", "time"}` for each link shared, `time` in seconds from the start of the meeting. Off by default, so a function that reads the transcript for what was said gets speech only. A message is cut at 1,000 characters, and past 100,000 characters of chat the later messages are left out of the text (and counted in the last line of `chat`); their links are still listed, up to 200. |

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
