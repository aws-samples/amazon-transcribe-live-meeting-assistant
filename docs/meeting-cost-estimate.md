---
title: "Meeting Cost Estimate"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Cost Estimate

LMA estimates what each meeting cost to run, line by line: the seconds of Amazon
Transcribe it used, the tokens every Amazon Bedrock call spent on it, and the
compute of the Virtual Participant that joined it. The approach follows the GenAI
IDP Accelerator's estimated cost per document: usage is recorded where it happens
as **metering records** against the meeting, and a configurable **price table**
turns those records into dollars whenever the estimate is read.

It is an **estimate**, and it is labelled as one wherever it appears. It uses public
on-demand list prices dated in the price table, the first volume tier of any tiered
price, and only the usage LMA meters. [What is not included](#what-is-not-included)
lists what is left out, and the estimate itself carries the same list.

## Where to read it

### On the meeting page

The **Estimated cost** section of a meeting's page — collapsed by default, below the
Speakers panel — shows the total in its header ("$0.1234 estimated", with
"(incomplete)" when some line has no price) and, opened, the itemised table:

| Column | Shows |
|---|---|
| Service | The service and model or API, grouped by what the usage was for, with a subtotal row per group. Select a service to see how its usage was measured (launch type, sizes, why a figure is approximate). |
| Unit, Quantity | What was counted and how much. |
| Unit price | US dollars per unit from the price table, or **No price** when the table has none. |
| Estimated cost | Quantity times unit price. Below $0.0001 is written "< $0.0001", never rounded to zero. Units that are shown for reference only read "not billed separately". |

Above the table the panel says that this is an estimate, the date of its prices, and
any line it could not price or region it priced at another region's rates. Below it
are what the estimate does not include, the region, and when the figure was
computed. The estimate is re-read when the meeting's status changes and when its
summary arrives, because most of a meeting's usage is recorded as it ends. A Virtual
Participant writes its runtime as it leaves, which can be later still; the refresh
button picks that up. A meeting with no metering records shows **No metering data**
rather than a cost of zero.

### Through the API

`Query.getCallCost(CallId)` on the AppSync API returns the itemised estimate for one
meeting. It answers the same callers `getCall` answers: an Admin, the meeting's
owner, anyone the meeting is shared with, and a backend role signing with IAM.

The result has:

| Field | Meaning |
|---|---|
| `TotalCost` | The sum of every priced line, in `Currency` (USD). |
| `ContextTotals` | The same total split by what the usage was for (see the contexts below). |
| `Items` | One line per context, service and unit: quantity, unit price and cost. |
| `UnpricedCount` | Lines with no price in the table. They are left out of every total, so a non-zero count means the total is incomplete. |
| `RecordCount` | How many metering records were read. Zero for a meeting held before this feature was deployed. |
| `PricesAsOf`, `PriceSource` | When the default prices were taken, and from where. |
| `Region`, `BaseRegion`, `RegionsWithoutListedPrices` | The deployment's region, and any region whose usage was priced at the base region's prices because the table lists none of its own. |
| `NotIncluded` | What the estimate leaves out. |
| `ComputedAt` | When this estimate was computed. |

The estimate is computed **on every read**, not frozen when the meeting ends. Usage
keeps arriving after the END event: the summary is generated after it, the Virtual
Participant writes its runtime as it leaves, and a summary can be regenerated days
later. A meeting's records are one DynamoDB query on one partition, so reading them
each time is cheap, and the figure is always the current one.

## What is metered, and how

Each line belongs to a **context** — what the usage was for — and a **service**, the
key the price table is looked up by.

| Context | Service | Unit(s) | Written by | How it is measured |
|---|---|---|---|---|
| Transcription | `transcribe/streaming` | `seconds` | WebSocket transcriber, Virtual Participant | Seconds of audio sent in each `StartStreamTranscription` request, rounded up, with Transcribe's 15-second minimum per request. A two-channel stream with channel identification is billed at the stream's duration, not twice it, so it is measured once. Written per request, so reconnects during a long meeting each appear. |
| Transcription | `transcribe/streaming-call-analytics` | `seconds` | WebSocket transcriber | As above, when `TRANSCRIBE_API_MODE` is `analytics`. |
| Transcription | `transcribe/streaming-pii-redaction` | `seconds` | WebSocket transcriber, Virtual Participant | The same seconds again when content redaction was applied to the request, because redaction is billed per second on top. |
| Transcription | `transcribe/streaming-custom-language-model` | `seconds` | WebSocket transcriber | The same seconds again when a custom language model was used. |
| Transcription | `transcribe/batch` | `seconds` | Upload Audio finalizer | The batch job's duration, read as the end time of the last transcribed word, with the 15-second minimum. Trailing silence, which is billed, is not counted. |
| Transcription | `lambda-microvm/arm64` | `vcpuSeconds`, `gbSeconds` | WebSocket transcriber, Virtual Participant | When the [MicroVM ASR engine](microvm-asr.md) transcribed the meeting: the MicroVM's configured memory (reported by the launcher; 8 GiB for every bundle today) and its vCPUs at 2 GiB per vCPU, multiplied by the time from the launch request to release. |
| Summary | `bedrock/<model id>` | `inputTokens`, `outputTokens`, cache units | Meeting summary Lambda | The `usage` block of each Converse call — one per summary section. A regeneration runs every section again and is recorded again. |
| SpeakerNaming | `bedrock/<model id>` | as above | Speaker name resolver | Each naming pass's model call. |
| AgendaDerivation | `bedrock/<model id>` | as above | Agenda deriver | The model call of each pass that read the transcript for a stated agenda: the automatic one, at most once per meeting, and each press of **Derive agenda**. See [Meeting Agenda](meeting-agenda.md#deriving-an-agenda-from-the-transcript). |
| MeetingAssistant | `bedrock/<model id>` | as above | Strands meeting assistant | The tokens the Strands SDK accumulated over every model call one answer took (an answer that uses tools is several calls), whether asked from the chat panel, by wake phrase or by the voice assistant. When the answer looked at the shared screen, a second record counts the `images` sent; they are billed as the input tokens already counted, so the count is shown and not priced. See [Meeting Video and Key Frames](meeting-video.md#asking-the-assistant-about-the-shared-screen). |
| SlideText | `bedrock/<model id>` | as above | Key frame text extractor | The tokens of reading the text of each key frame of the shared screen, once per frame. See [Meeting Video and Key Frames](meeting-video.md#slide-text). |
| VoiceAssistant | `bedrock/amazon.nova-2-sonic-v1:0` | `speechInputTokens`, `speechOutputTokens`, `textInputTokens`, `textOutputTokens` | Virtual Participant | The deltas of Nova Sonic's own `usageEvent`s, written every five minutes and when the assistant stops. Recorded in the region Nova Sonic ran in, which `AmazonNovaSonicRegion` can make different from the stack's. |
| VirtualParticipant | `fargate/x86_64`, `fargate/arm64` | `vcpuSeconds`, `gbSeconds` | Virtual Participant | The task's vCPU and memory (from the ECS task metadata endpoint) multiplied by how long the process ran, with Fargate's one-minute minimum. Image pull and task start-up before the process starts are not included. |
| VirtualParticipant | `lambda-microvm/arm64` | `vcpuSeconds`, `gbSeconds` | Virtual Participant | The MicroVM image's 4 GiB (2 vCPUs) multiplied by how long the process ran. |
| VirtualParticipant | `ec2/virtual-participant-host` | `runtimeSeconds` | Virtual Participant | The EC2 launch type's hosts are billed by the hour whether or not a meeting is running, so no share of a host is attributed to one meeting. The runtime is shown for reference with no cost. |
| PageAutomation | `bedrock/<model id>` | as above | Virtual Participant | The Claude call the page-automation fallback makes when a meeting platform's UI changed and a selector had to be found again. Cached for 30 days, so most meetings have none. |
| Sentiment | `comprehend/detect-sentiment` | `units` | Call event processor | One Amazon Comprehend `DetectSentiment` request per final transcript segment, at 100 characters per unit with a three-unit minimum. Kept as a running counter per meeting rather than a record per segment. |

Some units are shown for reference and never priced on their own: `requests`,
`sessions`, `audioSeconds` and `runtimeSeconds`. They appear on the estimate with
`Chargeable: false`.

## Prices

The default price table is
`lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/CallPricing.json`.
Every price in it is a public on-demand list price read from the AWS Price List API
(`AmazonBedrock`, `AmazonBedrockFoundationModels`, `transcribe`, `AWSLambda`,
`AmazonECS`, `comprehend`) on **2026-10-28**, in US dollars per single unit — per
token, per second, per vCPU-second. Where a price is tiered by monthly volume, the
first tier is listed, which is the highest price; a deployment that transcribes more
than 250,000 minutes a month pays less than this shows for Call Analytics and the
Transcribe add-ons. Standard streaming transcription is a flat price.

A few examples of what is in it, for `us-east-1`:

| Service | Unit | Price (USD) |
|---|---|---|
| Amazon Transcribe streaming | second | 0.0001667 |
| Amazon Transcribe streaming PII redaction (add-on) | second | 0.00004 |
| `global.anthropic.claude-haiku-4-5-20251001-v1:0` | input / output token | 0.000001 / 0.000005 |
| `global.anthropic.claude-sonnet-5-5` | input / output token | 0.000002 / 0.00001 |
| Nova 2 Sonic | speech input / speech output token | 0.000003 / 0.000012 |
| AWS Fargate (x86_64) | vCPU-hour / GB-hour | 0.04048 / 0.004445 |
| AWS Lambda MicroVMs (arm64) | vCPU-second / GB-second | 0.0000276944 / 0.0000036667 |
| Amazon Comprehend DetectSentiment | 100-character unit | 0.0001 |

The table holds a price for every Bedrock model the stack parameters offer, under its
exact model or inference-profile id. A `global.` and a `us.` profile of the same
model have different prices (where both exist, the regional `us.` profile is 10%
higher), so each is listed separately.
A test fails if an Amazon Nova or Anthropic model id is added to `lma-main.yaml`
without a price.

### Regions

Prices are listed for **us-east-1** and **us-west-2**. `regions.default` holds the
us-east-1 prices, and `regions.us-west-2` lists only what differs there (Nova Sonic's
text tokens). A deployment in any other region is priced at the us-east-1 prices,
and its estimate names the region in `RegionsWithoutListedPrices`. To price another
region, add a `regions.<region>` entry holding its differences from the base; a region whose prices equal the base is recognised by
listing it in `pricedRegions`. Either one is enough for the region to count as
priced.

### How a line is priced

The lookup is deliberately strict, as it is in the IDP Accelerator: a line's service
must match a price entry exactly, and its unit must be named in that entry. Nothing
is guessed from a similar-looking entry. A line with no price is reported as
unpriced, shown without a cost, left out of every total and counted in
`UnpricedCount`. A plausible wrong number is worse than a visible gap.

A unit named in `nonChargeableUnits` (the `images` the meeting assistant sends,
for example) is shown with no cost and is not counted as unpriced. Changes to the
price table replace a list rather than merging into it, so a custom
`nonChargeableUnits` must list every such unit, `images` included.

### Changing a price

The price table is the `call_pricing` section of the
[unified configuration store](configuration-store.md). The shipped table is part of
the baseline record, so a release that updates a price reaches every deployment on
its next stack update. An Admin's changes are stored as a delta in the `custom`
profile and keep winning over the baseline:

- `Query.getCallPricing` returns the shipped table (`Default`), the Admin's changes
  (`Custom`), the merged table every estimate uses (`Effective`) and the `Version` to
  send back on save.
- `Mutation.updateCallPricing(input: {Pricing, ExpectedVersion})` stores the changes.
  `Pricing` is a JSON object in the same shape as the shipped table, naming only what
  differs — for example
  `{"regions": {"default": {"transcribe/streaming": {"seconds": 0.00012}}}}` for a
  negotiated Transcribe rate. `"{}"` returns every price to the shipped value. The
  save replaces all of the Admin's earlier price changes with this object, so
  include every change that should stay. A price that is not a non-negative number,
  or a key the table does not have, is refused. `ExpectedVersion` is required, and a
  save against a version another Admin has since saved is refused rather than
  overwriting their change.
  The version is that of the whole `custom` profile, which the Transcript Summary
  page also writes, so a price save made after an unrelated prompt-template save
  is refused too; reload and save again.

Both are Admin only. An estimate read within five minutes of a price change may
still use the previous price: the section is on the store's standard cache tier.

## What is not included

The estimate covers the usage that can be attributed to one meeting. It does not
include:

- Resources that run whether or not a meeting is in progress: the WebSocket
  transcriber's Fargate service, NAT gateways, the Application Load Balancer,
  CloudFront, and Virtual Participant EC2 hosts.
- Per-request charges of the services LMA is built on: AppSync, DynamoDB, Kinesis
  Data Streams, LMA's own Lambda functions, S3 storage of recordings, and CloudWatch
  Logs.
- Amazon Bedrock Knowledge Base queries made by the meeting assistant's document and
  meeting-history tools (RetrieveAndGenerate does not report token usage), and the
  vector store behind them.
- Amazon Bedrock Guardrails, Amazon Translate (live translation in the browser) and
  Amazon Transcribe Post-Call Analytics.
- The short Nova Sonic clip the voice assistant renders once at start-up to
  acknowledge tool calls.
- Lambda MicroVM snapshot read and storage charges.
- Third-party services: ElevenLabs, Simli and Tavily web search.
- Free tier, Savings Plans, volume tiers beyond the first, and any private pricing
  agreement.

### Known ways the figure can be off

- **Lost records read as lower cost.** A metering write that fails is logged and
  dropped rather than allowed to interrupt transcription or a summary, and a process
  that is killed outright (out of memory, a host reboot) writes nothing for its open
  request or its runtime.
- **Redelivery can count Comprehend twice.** The sentiment counter adds on each
  delivery of a transcript segment, so a Kinesis batch that is redelivered adds its
  segments' units again. The amount is a fraction of a cent.
- **A re-run upload finalizer records its batch job again.** The finalizer writes
  the job's seconds each time it finalizes the meeting, so finalizing the same
  upload twice shows the batch transcription twice.
- **Compute is measured from inside the process.** Fargate's image pull and start-up,
  and the seconds between a MicroVM launch request and the Virtual Participant
  process starting, are billed but not counted.

## How the records are stored

Metering records are items in the meeting table (`EventSourcingTable`) under
`PK = mtr#<CallId>`, one item per metered event:

| Attribute | Meaning |
|---|---|
| `SK` | `<Context>#<RecordedAt>#<RecordId>`, unique per record. The Comprehend counter uses the fixed `Sentiment#counter#comprehend/detect-sentiment`. |
| `CallId`, `Context`, `Service` | The meeting, what the usage was for, and the price-table key. |
| `Usage` | Map of unit to quantity. The counter item keeps each unit as a top-level `u_<unit>` number instead, because DynamoDB's `ADD` cannot create a member of a map that does not exist yet. |
| `Details` | Strings for the reader: launch type, sizes, how a duration was measured. Never priced. |
| `Region` | The region the usage was billed in, when not the deployment's. |
| `Source`, `RecordedAt` | The component that wrote it, and when. |
| `ExpiresAfter` | The same retention as the meeting record (`MeetingRecordExpirationInDays`), so a meeting's cost expires with it. |

The grant each component gains for metering is `dynamodb:PutItem` on the meeting
table, conditioned on `dynamodb:LeadingKeys` matching `mtr#*`, in addition to
whatever access that component already had, so the only write it adds is of
metering records. (The call event
processor, which already has full access to the table, uses `UpdateItem` for its
counter.) Deleting a meeting from the UI deletes its metering records with it; a
record a Virtual Participant writes while it is being stopped by that same deletion
can land after the delete, and expires with its TTL.

The writers are `lma_metering` in the AI stack's configuration layer (the Lambda
functions), a copy of its writer in the meeting-assist stack, and `metering.ts` in
the WebSocket transcriber and the Virtual Participant.
`lma-ai-stack/test/test_metering_record_contract.py` asserts they all produce the
same item, and that every service the TypeScript writers name has a price.

## Upgrading

Nothing to do. Meetings held before the upgrade have no metering records, so their
estimate reads zero with `RecordCount: 0` — which means "not metered", not "free" — and the meeting page shows **No metering data** for them.

## Reference

| What | Where |
|---|---|
| Record writer and the estimator | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_metering/` |
| Default price table | `lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/CallPricing.json` |
| `getCallCost`, `getCallPricing`, `updateCallPricing` | `lma-ai-stack/source/lambda_functions/call_cost_resolver/` |
| Transcriber writer | `lma-websocket-transcriber-stack/source/app/src/metering.ts` |
| Virtual Participant writer | `lma-virtual-participant-stack/backend/src/metering.ts` |
| Meeting assistant writer | `lma-meetingassist-setup-stack/src/call_metering.py` |
| Cross-writer contract test | `lma-ai-stack/test/test_metering_record_contract.py` |
