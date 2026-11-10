---
title: "Meeting Cost Estimate"
---

<!-- Copyright (c) 2025 Amazon.com. This file is licensed under the MIT-0 License. -->
<!-- See the LICENSE file in the project root for full license information. -->

# Meeting Cost Estimate

LMA estimates what each meeting cost to run, line by line: the seconds of Amazon
Transcribe it used, the tokens every Amazon Bedrock call spent on it, and the
compute of the Virtual Participant that joined it. Usage is recorded where it
happens, as **metering records** against the meeting, and a **price table** turns
those records into dollars whenever the estimate is read. The approach is the
GenAI IDP Accelerator's estimated cost per document, including how prices are
kept: one centrally maintained table of unit prices, shipped with the release and
loaded into the configuration store at deploy time, which an administrator can
change from the web UI to use private pricing.

Every line of an estimate shows its arithmetic — usage, times a unit price,
equals a cost — and says where that unit price came from: the AWS Price List API
offer and usage type it was read from and the date, or the configuration profile
an administrator set it in, with the shipped price beside it.

It is an **estimate**, and it is labelled as one wherever it appears. The shipped
prices are public on-demand list prices, the first volume tier of any tiered
price, and only the usage LMA meters is counted.
[What is not included](#what-is-not-included) lists what is left out, and the
estimate itself carries the same list.

## Where to read it

### On the meeting page

The meeting page's header shows the total as **Est. cost** ("$0.1234", with
"(incomplete)" when some line has no price); selecting it opens the itemised
estimate. The same estimate is the **Estimated cost** section of the **Details**
tab — collapsed by default, below the Speakers and Consent panels — which shows
the total in its header ("$0.1234 estimated") and, opened, the itemised table. Its
columns read as the calculation each line is:

| Column | Shows |
|---|---|
| Service | The service and model or API, grouped by what the usage was for, with a subtotal row per group. Select a service to see how its usage was measured (launch type, sizes, why a figure is approximate). |
| Usage | What was counted and how much, for example "1,480 seconds". |
| × Unit price | US dollars per unit, or **No price** when the price table has none. |
| = Estimated cost | Usage times unit price. Below $0.0001 is written "< $0.0001", never rounded to zero. Units shown for reference only read "not billed separately". Pointing at a cost shows the whole calculation, for example "1,480 seconds × $0.0001667 = $0.2467". |
| Price source | Where the unit price came from. A shipped price shows its basis ("AWS Price List API", or "Estimate" where no list price exists); select it to read the offer, its version, the usage type, the date it was read, and a link to the service's pricing page. A price set in the configuration shows **Your price** with the shipped price beside it ("shipped $0.0001667"); selecting it names the profile it was set in. |

Above the table the panel says that this is an estimate, the date of the shipped
prices, how many lines use a price set in the configuration, and any line it could
not price or region it priced at another region's prices. Below it are what the
estimate does not include, the region, and when the figure was computed. The
estimate is re-read when the meeting's status changes and when its summary
arrives, because most of a meeting's usage is recorded as it ends. A Virtual
Participant writes its runtime as it leaves, which can be later still; the refresh
button picks that up. A meeting with no metering records shows **No metering
data** rather than a cost of zero.

### Through the API

`Query.getCallCost(CallId)` on the AppSync API returns the itemised estimate for one
meeting. It answers the same callers `getCall` answers: an Admin, the meeting's
owner, anyone the meeting is shared with, and a backend role signing with IAM.

The result has:

| Field | Meaning |
|---|---|
| `TotalCost` | The sum of every priced line, in `Currency` (USD). |
| `ContextTotals` | The same total split by what the usage was for (see the contexts below). |
| `Items` | One line per context, service and unit; the line fields are below. |
| `UnpricedCount` | Lines with no price in the table. They are left out of every total, so a non-zero count means the total is incomplete. |
| `CustomPriceCount`, `PriceProfile` | How many lines are priced at a price that differs from the shipped one, and the configuration profile in force. |
| `RecordCount` | How many metering records were read. Zero for a meeting held before metering was deployed. |
| `PricesAsOf`, `PriceSource` | When the shipped prices were read, and from where. |
| `Region`, `BaseRegion`, `RegionsWithoutListedPrices` | The deployment's region, and any region whose usage was priced at the base region's prices because the table lists none of its own. |
| `NotIncluded` | What the estimate leaves out. |
| `ComputedAt` | When this estimate was computed. |

Each line in `Items` has `Context`, `Service`, `Region`, `Unit`, `Quantity`,
`UnitPrice`, `Cost` (`Quantity × UnitPrice`), `Chargeable`, `Priced`, `Note` and
`Details`, and four fields that say where its price came from:

| Field | Meaning |
|---|---|
| `PriceOrigin` | `shipped` when the unit price is the one the release ships, `custom` when the configuration profile in force sets a different one (or one the release does not ship), `none` when there is no price. Null for a unit shown for reference only. |
| `PriceSource` | One sentence naming the source. For a shipped price, for example, "AWS Price List API (offer transcribe version 20260911124646, usage type USE1-StreamingAudio, read 2026-11-09)". For a custom price, "Set in the custom configuration profile. The shipped price is $0.0001667, from AWS Price List API (…)". |
| `PriceSourceUrl` | The AWS pricing page of a shipped price's service. |
| `ShippedUnitPrice` | The shipped price of the same unit, when there is one, so a custom price can be shown beside it. |

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
| Transcription | `transcribe/streaming-pii-redaction` | `seconds` | WebSocket transcriber, Virtual Participant | The same seconds again when content redaction was applied to the request, because redaction is billed per second on top. |
| Transcription | `transcribe/streaming-custom-language-model` | `seconds` | WebSocket transcriber | The same seconds again when a custom language model was used. |
| Transcription | `transcribe/batch` | `seconds` | Upload Audio finalizer | The batch job's duration, read as the end time of the last transcribed word, with the 15-second minimum. Trailing silence, which is billed, is not counted. |
| Transcription | `lambda-microvm/arm64` | `vcpuSeconds`, `gbSeconds` | WebSocket transcriber, Virtual Participant | When the [MicroVM ASR engine](microvm-asr.md) transcribed the meeting: the MicroVM's configured memory (reported by the launcher; 8 GiB for every bundle today) and its vCPUs at 2 GiB per vCPU, multiplied by the time from the launch request to release. |
| Summary | `bedrock/<model id>` | `inputTokens`, `outputTokens`, cache units | Meeting summary Lambda | The `usage` block of each Converse call — one per summary section. On a Claude model, when two or more sections share the transcript prefix and it reaches the model's minimum cacheable length, the first section that quotes it records it as `cacheWriteInputTokens` and the sections after it that quote it the same way as `cacheReadInputTokens`; see [Prompt Caching](transcript-summarization.md#prompt-caching). A regeneration runs every section again and is recorded again. |
| SpeakerNaming | `bedrock/<model id>` | as above | Speaker name resolver | Each naming pass's model call. |
| AgendaDerivation | `bedrock/<model id>` | as above | Agenda deriver | The model call of each pass that read the transcript for a stated agenda: the automatic one, at most once per meeting, and each press of **Derive agenda**. See [Meeting Agenda](meeting-agenda.md#deriving-an-agenda-from-the-transcript). |
| MeetingAssistant | `bedrock/<model id>` | as above | Strands meeting assistant | The tokens the Strands SDK accumulated over every model call one answer took (an answer that uses tools is several calls), whether asked from the chat panel, by wake phrase or by the voice assistant. When the answer looked at the shared screen, a second record counts the `images` sent; they are billed as the input tokens already counted, so the count is shown and not priced. See [Meeting Video and Key Frames](meeting-video.md#asking-the-assistant-about-the-shared-screen). |
| ConsentVerification | `bedrock/<model id>` | as above | Consent evidence verifier | The model call of each check of a consent evidence screenshot. See [Meeting Consent](meeting-consent.md#checking-evidence-with-a-vision-model). |
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

### The shipped price table

The prices are kept in one file in the repository,
`lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/CallPricing.json`.
At deploy time the configuration seeder writes it into the
[configuration store](configuration-store.md) as the `call_pricing` section of the
baseline record, `Config#default` — the counterpart of the IDP Accelerator's
`config_library/pricing.yaml` and its `DefaultPricing` record. It is JSON rather
than YAML because the store holds JSON and every other shipped default is JSON. A
release that changes a price therefore reaches every deployment on its next stack
update, as described in [Profiles and shipped prices](#profiles-and-shipped-prices).

Prices are US dollars per single unit — per token, per second, per vCPU-second, per
GB-second, per 100-character Amazon Comprehend unit. The table has two parts laid
out the same way, by region, then service, then unit:

```json
{
  "pricesAsOf": "2026-11-09",
  "baseRegion": "us-east-1",
  "pricedRegions": ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1", "us-gov-west-1"],
  "regions": {
    "default": { "transcribe/streaming": { "seconds": 0.0001667 } },
    "ap-northeast-1": { "fargate/x86_64": { "vcpuSeconds": 1.404444444e-05, "gbSeconds": 1.536111111e-06 } }
  },
  "sources": {
    "default": {
      "transcribe/streaming": {
        "basis": "price-list-api",
        "offer": "transcribe",
        "offerVersion": "20260911124646",
        "usageType": "USE1-StreamingAudio",
        "retrieved": "2026-11-09",
        "url": "https://aws.amazon.com/transcribe/pricing/"
      }
    },
    "ap-northeast-1": { "fargate/x86_64": { "basis": "price-list-api", "usageType": "APN1-Fargate-vCPU-Hours:perCPU, APN1-Fargate-GB-Hours", "...": "..." } }
  }
}
```

- `regions.default` holds the prices of the base region, `us-east-1`. Each other
  region lists only the prices that differ there, and a region's prices are
  `default` with its own entries laid over it.
- `sources` documents every price, under the same region and service it sits
  at. `basis` is `price-list-api` (read from the AWS Price List API), `pricing-page`
  (read from an AWS pricing page) or `estimate` (no list price exists, and `detail`
  says what was used instead). `offer`, `offerVersion` and `usageType` identify the
  Price List entry, `retrieved` is the date it was read, `url` is the service's
  pricing page, and `detail` says anything else a reader needs: the volume tier,
  a unit conversion, or why a price is an estimate. A region that lists a price of
  its own also lists that price's source, so the source a line names is always the
  one for the region its usage was billed in.
- `nonChargeableUnits`, `notes` and `notIncluded` are shown on estimates: the units
  counted for reference only, how each service's usage is measured, and what every
  estimate leaves out.

### Where the shipped prices come from

Every shipped price was read from the AWS Price List API
(`aws pricing get-products`, in us-east-1) on **2026-11-09**, from these offers:

| Offer | Version | Prices |
|---|---|---|
| `transcribe` | 20260911124646 | Streaming, streaming PII redaction, streaming custom language model, batch |
| `comprehend` | 20260911124625 | DetectSentiment |
| `AmazonECS` | 20261019224317 | AWS Fargate vCPU-hours and GB-hours, Linux x86 and ARM |
| `AWSLambda` | 20261030234255 | AWS Lambda MicroVM vCPU-seconds and GB-seconds, x86 and ARM |
| `AmazonBedrock` | 20261106184538 | Amazon Nova models, Nova Sonic |
| `AmazonBedrockFoundationModels` | 20261109180952 | Anthropic Claude models |

Each entry's `sources` record names its usage type, for example
`EU-MP:EU_InputTokenCount-Units` for the input tokens of a Claude model's `eu.`
profile, so any price can be checked again with one
`aws pricing get-products --service-code <offer> --filters Type=TERM_MATCH,Field=usagetype,Value=<usage type>`.

How the Price List's entries were turned into these prices:

- **Volume tiers.** Where a price is tiered by monthly volume — Transcribe's PII
  redaction and custom language model add-ons, and Comprehend — the first tier is
  listed, which is the highest price. Standard streaming and batch transcription
  are flat.
- **Units.** Fargate is listed per hour and is divided by 3,600; Bedrock is listed
  per thousand or per million tokens and is divided accordingly. Lambda MicroVMs
  are listed per second.
- **Claude profiles.** Each Claude model lists a Global price and a Regional one. A
  `global.` id is priced at the Global price, and a geographic profile (`us.`,
  `eu.`, `jp.`, `us-gov.`) at the Regional price, which is 10% higher. The Price
  List does not name the profile a SKU is for; the Regional SKU's description is
  what this mapping rests on, and it agrees with the Amazon Bedrock pricing page.
  Every Global Claude price was the same in each region that lists the model.
- **Amazon Nova profiles.** Nova Lite, Micro and Pro list one price per token type
  in a region, which is used for the plain id and for the region's `us.`, `eu.` or
  `apac.` profile alike. Nova 2 Lite lists its Global price per source region, and
  it differs: $0.30 per million input tokens from us-east-1 and us-west-2, $0.34
  from eu-west-1, $0.36 from ap-northeast-1.
- **Cache writes.** Claude models list a price for writing the prompt cache (the
  five-minute cache). Amazon Nova lists cache writes at $0 where it lists them at
  all.

### What was verified, and what was not

Every number in the table was read from the Price List API on the date above,
except these, each of which says so in its own `sources` record:

| Price | What it is instead |
|---|---|
| Nova Lite, Micro and Pro cache writes in eu-west-1, ap-northeast-1 and us-gov-west-1 | No cache-write price is listed there. They are priced at $0, the price Nova lists where it lists one, and the source's `detail` says so. |
| `global.anthropic.claude-sonnet-5-5` in ap-northeast-1 | The Price List has no Claude Sonnet 5.5 product for ap-northeast-1. The `ap-northeast-1` source is an `estimate` naming the us-east-1 Global price, which is used. |
| Nova Premier cache writes | No standard-tier price is listed, so none is shipped; a cache write on Nova Premier would show as unpriced. |

The Claude profile mapping in the list above is also an inference from the
Price List's descriptions rather than something the API states.

### Regions

Prices are listed for the four regions LMA can be launched in and for AWS
GovCloud (US-West):

| Region | What is listed of its own |
|---|---|
| `us-east-1` | Everything; it is the base region (`default`). |
| `us-west-2` | The same prices as us-east-1 except Nova 2 Sonic's text tokens. |
| `eu-west-1` | The `eu.` Claude and Nova profiles, Nova 2 Lite's Global price from Europe, and Lambda MicroVMs. Nova Sonic is not offered in eu-west-1. |
| `ap-northeast-1` | The `jp.` and `apac.` profiles, the in-region Nova Lite, Nova 2 Lite's Global price from Tokyo, Fargate, Lambda MicroVMs and Nova Sonic. |
| `us-gov-west-1` | Transcribe, Comprehend, Fargate, Nova Lite, Micro and Pro, and the `us-gov.` profiles of Claude Haiku 5.5, Opus 5.5, Sonnet 4.5 and Sonnet 5.5. Lambda MicroVMs, Nova Sonic and the other Claude models are not offered in GovCloud, so they have no GovCloud price; a GovCloud deployment cannot run them. |

A test (`lma_metering/test_shipped_prices.py`) holds the table to this: for each of
the four launch regions, every service a writer meters and every Amazon Bedrock id
a deployment there can call — under every Model inference geography setting — has
a price for every unit it is billed in, under its own key; and every price has a
source. Usage billed in a region the table does not list, such as Nova Sonic run
through `AmazonNovaSonicRegion` in eu-north-1, is priced at the base region's
prices, and the estimate names that region in `RegionsWithoutListedPrices`.

### How a line is priced

The lookup is deliberately strict, as it is in the IDP Accelerator: a line's service
must match a price entry exactly, and its unit must be named in that entry. Nothing
is guessed from a similar-looking entry. A line with no price is reported as
unpriced, shown without a cost, left out of every total and counted in
`UnpricedCount`. A plausible wrong number is worse than a visible gap.

One exception, for Amazon Bedrock models, is always named on the line it applies
to. The **Model inference geography** setting (see
[Where meeting content is processed](infrastructure-and-security.md#where-meeting-content-is-processed))
can run a model through a profile the table does not list for the region. A line
for such an id is priced at the same model's `global.` price, else its plain id's,
else its `us.` profile's, and its note says which. It is never priced as a
different model. For the four launch regions the table lists every id the setting
can produce, so this applies only elsewhere.

Where the price came from is decided per line. The estimate reads the price table
in force — the shipped table with the active configuration profile laid over it —
and the shipped table on its own. A unit price equal to the shipped one is
reported with the shipped price's source; one that differs is reported as set in
the active profile, with the shipped price and its source beside it.

A unit named in `nonChargeableUnits` (the `images` the meeting assistant sends,
for example) is shown with no cost and is not counted as unpriced.

## Changing prices

An Admin changes prices in **Configuration ▸ Meeting Cost Prices** in the
navigation, which opens the **Meeting Cost Prices** section of the Configuration
page (`/#/configuration/settings?section=call_pricing`). It is laid out as the IDP
Accelerator's Pricing page is: a table of every price, grouped by service.

- **Region.** The table shows one region's prices at a time, opening on the
  deployment's own region. A price shown for a region that is the base region's
  says "Base region price". A change made while a region is shown applies to that
  region only; a change made to the base region (`us-east-1`) applies wherever a
  region lists no price of its own.
- **Columns.** For each price: the API or model, the unit, the price in US dollars
  per unit (editable), the same price as the pricing pages quote it ("$3.00 per
  million input tokens", "$0.010002 per minute"), and its source. Select a source
  to see the offer, version, usage type and date, with a link to the pricing page.
- **Your price.** A price that differs from the shipped one is marked **Your
  price**, with the shipped price beside it, and has **Reset to default**, which
  puts the shipped price back. A price the release does not ship is marked
  **Added**, and has **Remove**. **Reset section to defaults** puts every price
  back.
- **Find a price** filters the table by service, model, unit or usage type.
- **Add a price** adds a price for a service or unit the table does not have, for
  the region shown: a service written `<service>/<api-or-model>`, a unit, and a
  price.
- **Export prices** downloads the whole table as the page shows it, unsaved
  changes included, as JSON or YAML (`"format": "lma-call-pricing"`).
  **Import prices** reads such a file, or a bare price table with a `regions`
  object, and lays it over the shipped table. An import saves nothing: its prices
  become unsaved changes on the page, marked and checked like any other.
- **Other settings of the estimate**, below the table, holds the section's other
  values: the base region, the regions with prices of their own, the units shown
  without a price, the notes on how usage is measured, and what every estimate
  leaves out.

Changes are saved with **Save changes**, like any other edit on the page: checked
against the section's [schema](configuration-store.md#section-schemas-and-validation)
first, saved into the active configuration profile as one revision with an
optional comment, and refused if someone else saved the profile since the page
loaded it. The save is refused while the shipped default or a Managed profile is
active; create a Custom profile and activate it first, as for any setting. The
schema refuses a price that is not a finite, non-negative number; a region,
service or unit name not in the table's form (a region is `default` or a code
such as `us-east-1`, a service `<service>/<api-or-model>`, a unit a word such as
`inputTokens`); a source with an unknown `basis`, a date not written
`YYYY-MM-DD` or a `url` that is not `https://`; and any key the estimate does not
read.

An estimate read within five minutes of a save may still use the previous price:
the section is on the store's standard cache tier.

### Profiles and shipped prices

A Custom profile is a full copy of the configuration, so it holds the prices of
the release it was created on. When a later release ships a different price, the
profile keeps the one it holds, and the editor marks that price as differing from
the shipped one — exactly as it marks a price an administrator changed — and the
meeting page shows its lines as **Your price** with the new shipped price beside
them. **Reset to default** on that price, or **Reset section to defaults**, takes
the release's price. Prices and sources a later release adds that the profile
does not hold come from the release. The source shown for a price is always the
shipped table's: the editor and the estimate read `sources` from the shipped
table only, so `sources` a profile holds (for example, from an imported export of
an earlier release) are stored but not read.

### Through the API

Both operations are Admin only.

- `Query.getCallPricing` returns the shipped table (`Default`), what the active
  profile stores for the section (`Custom`), the merged table every estimate uses
  (`Effective`), the active profile's name (`Profile`) and the `Version` to send
  back on save. While no profile is in force (the shipped default is active),
  `Profile` is `default`, `Custom` is empty and `Effective` is the shipped table,
  which is what every estimate then uses; `updateCallPricing` is refused in that
  state.
- `Mutation.updateCallPricing(input: {Pricing, ExpectedVersion, Comment})` stores
  `Pricing`, a JSON object in the shape of the shipped table, as the active
  profile's `call_pricing` section — for example
  `{"regions": {"default": {"transcribe/streaming": {"seconds": 0.00012}}}}` for a
  negotiated Transcribe rate. The save replaces the section, so include every
  price that should stay changed; `"{}"` returns every price to the shipped value.
  It is checked against the schema as the page's save is, requires
  `ExpectedVersion`, and is refused with `Conflict:` against a version someone has
  since saved and with `Refused:` while the default or a Managed profile is
  active.

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
- Amazon Bedrock Guardrails and Amazon Translate (live translation in the browser).
- The short Nova Sonic clip the voice assistant renders once at start-up to
  acknowledge tool calls.
- Lambda MicroVM snapshot read and storage charges.
- Third-party services: ElevenLabs, Simli and Tavily web search.
- Free tier, Savings Plans and volume tiers beyond the first. A private pricing
  agreement is applied only where an administrator sets its prices; see
  [Changing prices](#changing-prices).

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
| `ExpiresAfter` | The same retention as the meeting record (`MeetingRecordExpirationInDays`, or the shorter period set in the Retention section of the Configuration page), so a meeting's cost expires with it. |

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

Nothing to do. Meetings held before metering was deployed have no metering
records, so their estimate reads zero with `RecordCount: 0` — which means "not
metered", not "free" — and the meeting page shows **No metering data** for them.

A later release that changes a shipped price reaches a deployment as described in
[Profiles and shipped prices](#profiles-and-shipped-prices).

## Reference

| What | Where |
|---|---|
| Shipped price table, loaded as the default at deploy time | `lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/CallPricing.json` |
| Its schema, which every save is checked against | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/schemas/call_pricing.json` |
| Record writer and the estimator | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_metering/` |
| What the shipped table guarantees | `lma-ai-stack/source/lambda_layers/configuration_layer/lma_metering/test_shipped_prices.py` |
| `getCallCost`, `getCallPricing`, `updateCallPricing` | `lma-ai-stack/source/lambda_functions/call_cost_resolver/` |
| The Meeting Cost Prices editor | `lma-ai-stack/source/ui/src/components/configuration/PriceTable.jsx`, `price-table.js` |
| The estimate on the meeting page | `lma-ai-stack/source/ui/src/components/call-panel/CallCostPanel.jsx` |
| Transcriber writer | `lma-websocket-transcriber-stack/source/app/src/metering.ts` |
| Virtual Participant writer | `lma-virtual-participant-stack/backend/src/metering.ts` |
| Meeting assistant writer | `lma-meetingassist-setup-stack/src/call_metering.py` |
| Cross-writer contract test | `lma-ai-stack/test/test_metering_record_contract.py` |
