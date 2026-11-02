/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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

/** What the Service column shows: the model or API, without the service prefix. */
export const serviceLabel = (service) => {
  if (!service) return '';
  const [prefix, ...rest] = service.split('/');
  const name = rest.join('/');
  const prefixes = {
    bedrock: 'Amazon Bedrock',
    transcribe: 'Amazon Transcribe',
    'lambda-microvm': 'AWS Lambda MicroVM',
    fargate: 'AWS Fargate',
    ec2: 'Amazon EC2',
    comprehend: 'Amazon Comprehend',
  };
  return prefixes[prefix] ? `${prefixes[prefix]} — ${name}` : service;
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
  (estimate.RegionsWithoutListedPrices || []).forEach((region) => {
    caveats.push(
      `The price table lists no prices for ${region}; ${estimate.BaseRegion || 'base region'} prices were used ` +
        'for its usage.',
    );
  });
  return caveats;
};
