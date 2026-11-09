/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import { describe, expect, it } from 'vitest';

import {
  PRICING_FORMAT,
  filterRows,
  groupRows,
  initialRegion,
  priceRows,
  pricingDocument,
  pricingText,
  readPricingFile,
  regionOptions,
  sourceSummary,
} from './price-table';

const SHIPPED = {
  baseRegion: 'us-east-1',
  regions: {
    default: {
      'transcribe/streaming': { seconds: 0.0001667 },
      'bedrock/global.amazon.nova-2-lite-v1:0': { inputTokens: 3e-7, outputTokens: 2.5e-6 },
    },
    'ap-northeast-1': { 'bedrock/global.amazon.nova-2-lite-v1:0': { inputTokens: 3.6e-7 } },
  },
  sources: {
    default: { 'transcribe/streaming': { basis: 'price-list-api', offer: 'transcribe', retrieved: '2026-11-09' } },
  },
};

const withPrice = (table, path, price) => {
  const copy = JSON.parse(JSON.stringify(table));
  let node = copy;
  path.slice(0, -1).forEach((key) => {
    node[key] = node[key] || {};
    node = node[key];
  });
  node[path[path.length - 1]] = price;
  return copy;
};

describe('the price rows', () => {
  it("lists every price a region uses, its own over the base region's", () => {
    const rows = priceRows(SHIPPED, SHIPPED, 'ap-northeast-1');
    const input = rows.find((row) => row.unit === 'inputTokens');
    const output = rows.find((row) => row.unit === 'outputTokens');
    expect(input.price).toBe(3.6e-7);
    expect(input.inherited).toBe(false);
    expect(output.price).toBe(2.5e-6);
    expect(output.inherited).toBe(true);
    expect(rows.every((row) => !row.changed)).toBe(true);
  });

  it('lists the services in the price table order: Transcribe before Bedrock', () => {
    expect(groupRows(priceRows(SHIPPED, SHIPPED, 'default')).map((group) => group.family)).toEqual([
      'Amazon Transcribe',
      'Amazon Bedrock',
    ]);
  });

  it('marks a changed price, with the shipped price and its source beside it', () => {
    const edited = withPrice(SHIPPED, ['regions', 'default', 'transcribe/streaming', 'seconds'], 0.0001);
    const row = priceRows(edited, SHIPPED, 'default').find((r) => r.service === 'transcribe/streaming');
    expect(row.changed).toBe(true);
    expect(row.shippedPrice).toBe(0.0001667);
    expect(row.source.offer).toBe('transcribe');
    expect(row.resetPath).toEqual(['regions', 'default', 'transcribe/streaming', 'seconds']);
  });

  it('puts back an inherited price where it is stored, the base region', () => {
    const edited = withPrice(SHIPPED, ['regions', 'default', 'transcribe/streaming', 'seconds'], 0.0001);
    const row = priceRows(edited, SHIPPED, 'ap-northeast-1').find((r) => r.service === 'transcribe/streaming');
    expect(row.inherited && row.changed).toBe(true);
    expect(row.path).toEqual(['regions', 'ap-northeast-1', 'transcribe/streaming', 'seconds']);
    expect(row.resetPath).toEqual(['regions', 'default', 'transcribe/streaming', 'seconds']);
  });

  it('marks a price the release does not ship as added', () => {
    const edited = withPrice(SHIPPED, ['regions', 'default', 'transcribe/batch', 'seconds'], 0.0001);
    const row = priceRows(edited, SHIPPED, 'default').find((r) => r.service === 'transcribe/batch');
    expect(row.added && row.changed).toBe(true);
    expect(row.shippedPrice).toBeUndefined();
  });

  it('finds rows by service, model or unit, ignoring case', () => {
    const rows = priceRows(SHIPPED, SHIPPED, 'default');
    expect(filterRows(rows, 'NOVA').map((row) => row.unit)).toEqual(['inputTokens', 'outputTokens']);
    expect(filterRows(rows, 'seconds')).toHaveLength(1);
    expect(filterRows(rows, '')).toHaveLength(3);
  });
});

describe('the regions', () => {
  it("opens on the deployment's region, or the base region's prices when it is the base", () => {
    expect(initialRegion(SHIPPED, 'ap-northeast-1')).toBe('ap-northeast-1');
    expect(initialRegion(SHIPPED, 'us-east-1')).toBe('default');
    expect(initialRegion(SHIPPED, '')).toBe('default');
  });

  it("offers the deployment's region even where the table lists no prices of its own", () => {
    const options = regionOptions(SHIPPED, SHIPPED, 'eu-central-1');
    expect(options.map((option) => option.value)).toEqual(['default', 'ap-northeast-1', 'eu-central-1']);
    expect(options[0].label).toBe('us-east-1');
    expect(options[2].label).toBe('eu-central-1 (this deployment)');
    expect(options[2].description).toMatch(/base region’s apply/);
  });
});

describe('a price source', () => {
  it('names the offer, its version, the usage type and the date it was read', () => {
    expect(
      sourceSummary({
        basis: 'price-list-api',
        offer: 'AmazonECS',
        offerVersion: '20261019224317',
        usageType: 'APN1-Fargate-vCPU-Hours:perCPU',
        retrieved: '2026-11-09',
        detail: 'Listed per hour; divided by 3,600.',
      }),
    ).toBe(
      'AWS Price List API (offer AmazonECS version 20261019224317, usage type APN1-Fargate-vCPU-Hours:perCPU, ' +
        'read 2026-11-09). Listed per hour; divided by 3,600.',
    );
    expect(sourceSummary({ basis: 'estimate', detail: 'Not listed.' })).toBe('Estimate. Not listed.');
    expect(sourceSummary(undefined)).toBe('');
  });
});

describe('a price file', () => {
  it('reads back what it exports, as JSON and as YAML', () => {
    const document = pricingDocument({ values: SHIPPED, stackName: 'LMA', now: '2026-11-09T00:00:00Z' });
    expect(document.format).toBe(PRICING_FORMAT);
    expect(readPricingFile(pricingText(document, 'json'), {})).toEqual(SHIPPED);
    expect(readPricingFile(pricingText(document, 'yaml'), {})).toEqual(SHIPPED);
  });

  it('lays a bare price table over the shipped one, keeping what it leaves out', () => {
    const read = readPricingFile('{"regions": {"default": {"transcribe/streaming": {"seconds": 0.0001}}}}', SHIPPED);
    expect(read.regions.default['transcribe/streaming']).toEqual({ seconds: 0.0001 });
    expect(read.regions.default['bedrock/global.amazon.nova-2-lite-v1:0']).toEqual({
      inputTokens: 3e-7,
      outputTokens: 2.5e-6,
    });
    expect(read.sources).toEqual(SHIPPED.sources);
  });

  it('refuses a file that is not a price table', () => {
    expect(() => readPricingFile('{"format": "lma-configuration-profile"}', SHIPPED)).toThrow(/not an exported price/);
    expect(() => readPricingFile('{"prices": []}', SHIPPED)).toThrow(/no price table/);
    expect(() => readPricingFile('{nope', SHIPPED)).toThrow(/not a JSON or YAML file/);
  });
});
