/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import CASES from './inference-geography-cases.json';
import { GEOGRAPHY, GLOBAL, IN_REGION, describeScope, resolveForGeography, splitModelId } from './inference-geography';
import SectionNotice, { modelPlacements } from './section-notices';

const SONNET = 'global.anthropic.claude-sonnet-5-5';
const HAIKU = 'global.anthropic.claude-haiku-4-5-20251001-v1:0';

describe('resolveForGeography', () => {
  // The cases are the Python resolver's answers (lma-ai-stack/test/test_inference_geography.py).
  it.each(CASES.map((c) => [`${c.region} ${c.geography} ${c.model}`, c]))('agrees with Python: %s', (_name, c) => {
    const result = resolveForGeography(c.model, c.geography, c.region);
    expect(result.modelId).toBe(c.expected);
    expect(result.unavailable).toBe(c.expected === null);
    expect(result.verified).toBe(c.verified);
  });

  it('leaves every id as chosen under Global', () => {
    expect(resolveForGeography(SONNET, GLOBAL, 'eu-west-1').modelId).toBe(SONNET);
  });

  it('runs the shipped default within the EU from an EU Region', () => {
    expect(resolveForGeography(SONNET, GEOGRAPHY, 'eu-west-1').modelId).toBe('eu.anthropic.claude-sonnet-5-5');
  });

  it('refuses a Claude model in-region where only profiles offer it', () => {
    expect(resolveForGeography(SONNET, IN_REGION, 'us-east-1').unavailable).toBe(true);
  });

  it('splits only known profile prefixes', () => {
    expect(splitModelId('jp.amazon.nova-2-lite-v1:0')).toEqual(['jp', 'amazon.nova-2-lite-v1:0']);
    expect(splitModelId('amazon.nova-lite-v1:0')).toEqual([null, 'amazon.nova-lite-v1:0']);
  });

  it('describes where an id runs', () => {
    expect(describeScope(SONNET, 'us-east-1')).toBe('any commercial AWS Region');
    expect(describeScope('eu.anthropic.claude-sonnet-5-5', 'eu-west-1')).toBe('AWS Regions in Europe');
    expect(describeScope('amazon.nova-lite-v1:0', 'us-east-1')).toBe('us-east-1 only');
  });
});

describe('the Amazon Bedrock Models notice', () => {
  const values = {
    inferenceGeography: GEOGRAPHY,
    summaryModelId: SONNET,
    meetingAssistModelId: HAIKU,
    knowledgeBaseModelId: HAIKU,
    keyframeTextModelId: 'amazon.nova-lite-v1:0',
  };

  it('lists each feature with the id it will run as, including the consent check model', () => {
    const placements = modelPlacements(
      values,
      { meeting_consent: { verifierModelId: 'amazon.nova-lite-v1:0' } },
      'eu-west-1',
    );
    expect(placements.map((p) => p.modelId)).toEqual([
      'eu.anthropic.claude-sonnet-5-5',
      'eu.anthropic.claude-haiku-4-5-20251001-v1:0',
      'eu.anthropic.claude-haiku-4-5-20251001-v1:0',
      'eu.amazon.nova-lite-v1:0',
      'eu.amazon.nova-lite-v1:0',
    ]);
  });

  it('renders the preview for the section', () => {
    render(<SectionNotice section="language_models" values={values} region="eu-west-1" />);
    expect(screen.getByText('Summaries and meeting insights')).toBeTruthy();
    expect(screen.getByText('eu.anthropic.claude-sonnet-5-5')).toBeTruthy();
    expect(screen.queryByText(/no model in the selected geography/)).toBeNull();
  });

  it('warns about features that have no model in the geography', () => {
    render(
      <SectionNotice
        section="language_models"
        values={{ ...values, inferenceGeography: IN_REGION }}
        region="us-east-1"
      />,
    );
    expect(screen.getByText('Some features have no model in the selected geography')).toBeTruthy();
  });

  it.each([
    [IN_REGION, 'eu-west-1'],
    [GEOGRAPHY, 'ca-central-1'],
  ])('flags a plain id the Region does not offer under %s in %s', (geography, region) => {
    const micro = 'amazon.nova-micro-v1:0';
    const [placement] = modelPlacements({ inferenceGeography: geography, summaryModelId: micro }, {}, region);
    expect(placement).toMatchObject({ modelId: micro, chosen: micro, verified: false, unavailable: false });
    render(
      <SectionNotice
        section="language_models"
        values={{ inferenceGeography: geography, summaryModelId: micro }}
        region={region}
      />,
    );
    expect(screen.getByText(/not in this Region's Amazon Bedrock listing/)).toBeTruthy();
  });

  it('flags nothing under Global, where no id is checked', () => {
    render(
      <SectionNotice section="language_models" values={{ ...values, inferenceGeography: GLOBAL }} region="eu-west-1" />,
    );
    expect(screen.queryByText(/Amazon Bedrock listing/)).toBeNull();
  });

  it('does not flag ids the listing shows for the Region', () => {
    render(<SectionNotice section="language_models" values={values} region="eu-west-1" />);
    expect(screen.queryByText(/Amazon Bedrock listing/)).toBeNull();
  });
});
