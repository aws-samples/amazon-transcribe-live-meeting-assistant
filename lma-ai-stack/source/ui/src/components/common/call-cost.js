/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Presentation helpers for the meeting cost estimate (getCallCost).
 *
 * The backend does the pricing; this only turns its lines into what the panel
 * shows. Kept free of React so the rules that decide what a reader sees -- how a
 * small cost is written, which lines are grouped under which heading, what an
 * unpriced line says -- are tested directly.
 */

/** Human labels for the contexts the backend groups usage by, in display order. */
export const CONTEXT_LABELS = {
  Transcription: 'Transcription',
  VirtualParticipant: 'Virtual Participant',
  VoiceAssistant: 'Voice assistant',
  MeetingAssistant: 'Meeting assistant',
  Summary: 'Summary',
  SpeakerNaming: 'Speaker naming',
  AgendaDerivation: 'Agenda derivation',
  SlideText: 'Slide text',
  ConsentVerification: 'Consent evidence check',
  Sentiment: 'Sentiment analysis',
  PageAutomation: 'Page automation',
};

const CONTEXT_ORDER = Object.keys(CONTEXT_LABELS);

export const contextLabel = (context) => CONTEXT_LABELS[context] || context || 'Other';

/** Human labels for the units the price table prices. */
const UNIT_LABELS = {
  inputTokens: 'input tokens',
  outputTokens: 'output tokens',
  cacheReadInputTokens: 'cache read tokens',
  cacheWriteInputTokens: 'cache write tokens',
  speechInputTokens: 'speech input tokens',
  speechOutputTokens: 'speech output tokens',
  textInputTokens: 'text input tokens',
  textOutputTokens: 'text output tokens',
  seconds: 'seconds',
  vcpuSeconds: 'vCPU-seconds',
  gbSeconds: 'GB-seconds',
  runtimeSeconds: 'runtime seconds',
  units: '100-character units',
  requests: 'requests',
  sessions: 'sessions',
  images: 'images',
};

export const unitLabel = (unit) => UNIT_LABELS[unit] || unit;

/**
 * A dollar amount for the estimate.
 *
 * Meeting costs are routinely fractions of a cent, so two decimal places would
 * show most lines as $0.00. Four places are shown, and anything smaller than
 * that is written "< $0.0001" rather than rounded to zero -- a reader comparing
 * lines should never be told a priced line cost nothing.
 */
export const formatCost = (value, currency = 'USD') => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return 'N/A';
  }
  const amount = Number(value);
  const symbol = currency === 'USD' ? '$' : `${currency} `;
  if (amount > 0 && amount < 0.0001) {
    return `< ${symbol}0.0001`;
  }
  return `${symbol}${amount.toFixed(4)}`;
};

/** A unit price, which is usually far smaller than any cost: shown in full precision. */
export const formatUnitPrice = (value, currency = 'USD') => {
  if (value === null || value === undefined) {
    return 'No price';
  }
  const symbol = currency === 'USD' ? '$' : `${currency} `;
  // Fixed notation with four significant digits: per-token prices go well below
  // 1e-6, where toPrecision would switch to exponent form.
  const text = Number(value).toLocaleString('en-US', {
    maximumSignificantDigits: 4,
    maximumFractionDigits: 20,
  });
  return `${symbol}${text}`;
};

/** A quantity with thousands separators and no more than two decimals. */
export const formatQuantity = (value) =>
  Number(value || 0).toLocaleString('en-US', { maximumFractionDigits: 2, minimumFractionDigits: 0 });

/**
 * The estimate's lines grouped by context, in display order, each group with
 * its subtotal. Lines keep the order the backend sent within a group.
 */
export const groupLines = (estimate) => {
  const items = estimate?.Items || [];
  const totals = Object.fromEntries((estimate?.ContextTotals || []).map((t) => [t.Context, t.Cost]));
  const byContext = new Map();
  items.forEach((item) => {
    const key = item.Context || 'Other';
    if (!byContext.has(key)) byContext.set(key, []);
    byContext.get(key).push(item);
  });
  const rank = (context) => {
    const index = CONTEXT_ORDER.indexOf(context);
    return index === -1 ? CONTEXT_ORDER.length : index;
  };
  return [...byContext.entries()]
    .sort(([a], [b]) => rank(a) - rank(b) || a.localeCompare(b))
    .map(([context, lines]) => ({
      context,
      label: contextLabel(context),
      lines,
      subtotal: totals[context] ?? 0,
      unpriced: lines.filter((line) => !line.Priced).length,
    }));
};

/**
 * Flatten the groups into the rows the table renders: one per line, then a
 * subtotal row per group. A row's `kind` says which it is.
 */
export const tableRows = (estimate) =>
  groupLines(estimate).flatMap((group) => [
    ...group.lines.map((line, index) => ({
      kind: 'line',
      id: `${group.context}-${line.Service}-${line.Region}-${line.Unit}-${index}`,
      ...line,
    })),
    {
      kind: 'subtotal',
      id: `${group.context}-subtotal`,
      Context: group.context,
      label: group.label,
      Cost: group.subtotal,
      unpriced: group.unpriced,
    },
  ]);

/** The AWS service each price-table prefix names, in the order the price table lists them. */
export const SERVICE_NAMES = {
  transcribe: 'Amazon Transcribe',
  bedrock: 'Amazon Bedrock',
  'lambda-microvm': 'AWS Lambda MicroVM',
  fargate: 'AWS Fargate',
  comprehend: 'Amazon Comprehend',
  ec2: 'Amazon EC2',
};

/** What the Service column shows: the model or API, without the service prefix. */
export const serviceLabel = (service) => {
  if (!service) return '';
  const [prefix, ...rest] = service.split('/');
  const name = rest.join('/');
  return SERVICE_NAMES[prefix] ? `${SERVICE_NAMES[prefix]} — ${name}` : service;
};

// How many of a unit a readable price is quoted for, and what that many is called.
const READABLE_SCALES = {
  inputTokens: [1e6, 'per million input tokens'],
  outputTokens: [1e6, 'per million output tokens'],
  cacheReadInputTokens: [1e6, 'per million cache read tokens'],
  cacheWriteInputTokens: [1e6, 'per million cache write tokens'],
  speechInputTokens: [1e6, 'per million speech input tokens'],
  speechOutputTokens: [1e6, 'per million speech output tokens'],
  textInputTokens: [1e6, 'per million text input tokens'],
  textOutputTokens: [1e6, 'per million text output tokens'],
  seconds: [60, 'per minute'],
  vcpuSeconds: [3600, 'per vCPU-hour'],
  gbSeconds: [3600, 'per GB-hour'],
  units: [1, 'per 100-character unit'],
};

/**
 * A unit price as the AWS pricing pages quote it -- "$3.00 per million input
 * tokens", "$0.010002 per minute" -- so a reader can compare it with them.
 * Empty for a unit with no usual scale.
 */
export const readablePrice = (unit, price, currency = 'USD') => {
  const scale = READABLE_SCALES[unit];
  if (!scale || price === null || price === undefined || Number.isNaN(Number(price))) return '';
  const symbol = currency === 'USD' ? '$' : `${currency} `;
  const amount = Number(price) * scale[0];
  // As a pricing page writes it: "$3.00", "$0.10", "$0.04048". Six places at
  // most, which also drops the binary noise of 1e-7 × 1e6.
  const text = amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 6 });
  return `${symbol}${text} ${scale[1]}`;
};

/** The calculation a priced line shows: "1,480 seconds × $0.0001667 = $0.2467". */
export const lineCalculation = (line, currency = 'USD') => {
  if (!line || !line.Chargeable || !line.Priced || line.UnitPrice === null || line.UnitPrice === undefined) {
    return '';
  }
  return (
    `${formatQuantity(line.Quantity)} ${unitLabel(line.Unit)} × ${formatUnitPrice(line.UnitPrice, currency)}` +
    ` = ${formatCost(line.Cost, currency)}`
  );
};

/**
 * A few words naming where a line's price came from, for the table cell; the
 * full sentence is the line's PriceSource.
 */
export const priceSourceLabel = (line) => {
  if (!line || !line.Chargeable) return '';
  if (line.PriceOrigin === 'custom') return 'Your price';
  if (line.PriceOrigin === 'none' || !line.Priced) return 'No price';
  if (!line.PriceSource) return 'Shipped price';
  // "AWS Price List API (offer ..., read ...)" is shown as "AWS Price List API".
  return line.PriceSource.split(/[(.]/)[0].trim() || 'Shipped price';
};

/** Short, factual statements about what limits this estimate, for the panel's notes. */
export const estimateCaveats = (estimate) => {
  if (!estimate) return [];
  const caveats = [];
  if (estimate.UnpricedCount > 0) {
    caveats.push(
      `${estimate.UnpricedCount} line${estimate.UnpricedCount === 1 ? ' has' : 's have'} no price in the ` +
        'price table and ' +
        `${estimate.UnpricedCount === 1 ? 'is' : 'are'} not included in the total.`,
    );
  }
  if (estimate.CustomPriceCount > 0) {
    const profile = estimate.PriceProfile ? `the ${estimate.PriceProfile} configuration profile` : 'this deployment';
    caveats.push(
      `${estimate.CustomPriceCount} line${estimate.CustomPriceCount === 1 ? ' is' : 's are'} priced at a price ` +
        `set in ${profile} rather than the shipped list price; each shows the shipped price beside it.`,
    );
  }
  (estimate.RegionsWithoutListedPrices || []).forEach((region) => {
    caveats.push(
      `The price table lists no prices for ${region}; ${estimate.BaseRegion || 'base region'} prices were used ` +
        'for its usage.',
    );
  });
  return caveats;
};
