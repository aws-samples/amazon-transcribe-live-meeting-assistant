/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Meeting Cost Prices table on its own, inside the section provider the
 * Configuration page gives it. What is asserted: a region shows its own prices
 * over the base region's, a change made while viewing a region is stored for
 * that region, and a price file is exported whole and imported as unsaved
 * changes, never saved by itself.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { readFileSync } from 'fs';
import { join } from 'path';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SectionProvider } from './fields';
import PriceTable from './PriceTable';
import { PRICING_FORMAT } from './price-table';

const SCHEMA = JSON.parse(
  readFileSync(
    join(import.meta.dirname, '../../../../lambda_layers/configuration_layer/lma_config/schemas/call_pricing.json'),
    'utf8',
  ),
);

const DEFAULTS = {
  currency: 'USD',
  baseRegion: 'us-east-1',
  pricedRegions: ['us-east-1', 'eu-west-1'],
  regions: {
    default: { 'transcribe/streaming': { seconds: 0.0001667 }, 'fargate/x86_64': { vcpuSeconds: 1.1e-5 } },
    'eu-west-1': { 'fargate/x86_64': { vcpuSeconds: 1.2e-5 } },
  },
  sources: {
    default: { 'fargate/x86_64': { basis: 'price-list-api', usageType: 'USE1-Fargate', retrieved: '2026-11-09' } },
    'eu-west-1': { 'fargate/x86_64': { basis: 'price-list-api', usageType: 'EU-Fargate', retrieved: '2026-11-09' } },
  },
};

// The page's part: holds the section's values and hands them to the provider.
const Harness = ({ initial = DEFAULTS, onValues = () => {}, deploymentRegion = '' }) => {
  const [values, setValues] = useState(initial);
  return (
    <SectionProvider
      values={values}
      defaults={DEFAULTS}
      problems={[]}
      onChange={(next) => {
        setValues(next);
        onValues(next);
      }}
    >
      <PriceTable schema={SCHEMA} deploymentRegion={deploymentRegion} />
    </SectionProvider>
  );
};

Harness.propTypes = {
  initial: PropTypes.shape({}),
  onValues: PropTypes.func,
  deploymentRegion: PropTypes.string,
};

describe('PriceTable', () => {
  it("opens on the deployment's region, showing its own prices over the base region's", async () => {
    render(<Harness deploymentRegion="eu-west-1" />);
    expect(screen.getByLabelText('Price of vCPU-seconds of fargate/x86_64').value).toBe('0.000012');
    // The Transcribe price is the base region's, and says so.
    expect(screen.getByLabelText('Price of seconds of transcribe/streaming').value).toBe('0.0001667');
    expect(screen.getByText('Base region price')).toBeTruthy();
    // The region's own price names the region's own source.
    fireEvent.click(screen.getByText('AWS Price List API, 2026-11-09'));
    expect(await screen.findByText(/usage type EU-Fargate/)).toBeTruthy();
  });

  it('stores a change made while viewing a region for that region only', () => {
    const onValues = vi.fn();
    render(<Harness deploymentRegion="eu-west-1" onValues={onValues} />);
    fireEvent.change(screen.getByLabelText('Price of seconds of transcribe/streaming'), {
      target: { value: '0.0002' },
    });
    const values = onValues.mock.calls.at(-1)[0];
    expect(values.regions['eu-west-1']['transcribe/streaming']).toEqual({ seconds: 0.0002 });
    expect(values.regions.default['transcribe/streaming']).toEqual({ seconds: 0.0001667 });
  });

  it('exports every price as the page holds it, sources included', async () => {
    const created = [];
    const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
    URL.createObjectURL = vi.fn((blob) => {
      created.push(blob);
      return 'blob:prices';
    });
    URL.revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    try {
      render(<Harness />);
      fireEvent.click(screen.getByRole('button', { name: 'Export prices' }));
      fireEvent.click(await screen.findByText('As JSON'));
      const exported = JSON.parse(await created[0].text());
      expect(exported.format).toBe(PRICING_FORMAT);
      expect(exported.pricing).toEqual(DEFAULTS);
      expect(click).toHaveBeenCalled();
    } finally {
      click.mockRestore();
      URL.createObjectURL = original.create;
      URL.revokeObjectURL = original.revoke;
    }
  });

  it('imports a price file as unsaved changes over the shipped prices', async () => {
    const onValues = vi.fn();
    render(<Harness onValues={onValues} />);
    const file = new File(
      [
        JSON.stringify({
          format: PRICING_FORMAT,
          formatVersion: 1,
          pricing: { regions: { default: { 'transcribe/streaming': { seconds: 0.00009 } } } },
        }),
      ],
      'negotiated.json',
      { type: 'application/json' },
    );
    fireEvent.change(screen.getByLabelText('Price file to import'), { target: { files: [file] } });
    await waitFor(() =>
      expect(screen.getByText(/Read the prices in negotiated.json. Nothing is saved yet/)).toBeTruthy(),
    );
    const values = onValues.mock.calls.at(-1)[0];
    expect(values.regions.default['transcribe/streaming']).toEqual({ seconds: 0.00009 });
    // What the file leaves out keeps its shipped value.
    expect(values.regions.default['fargate/x86_64']).toEqual({ vcpuSeconds: 1.1e-5 });
    expect(screen.getByText('Your price')).toBeTruthy();
  });

  it('refuses a file that holds no price table, and changes nothing', async () => {
    const onValues = vi.fn();
    render(<Harness onValues={onValues} />);
    const file = new File(['{"format": "lma-configuration-profile", "sections": {}}'], 'profile.json');
    fireEvent.change(screen.getByLabelText('Price file to import'), { target: { files: [file] } });
    await waitFor(() => expect(screen.getByText(/Could not import profile.json/)).toBeTruthy());
    expect(onValues).not.toHaveBeenCalled();
  });

  it('finds a price by model or unit, opening the groups that hold it', () => {
    render(<Harness />);
    fireEvent.change(screen.getByLabelText('Find a price'), { target: { value: 'fargate' } });
    expect(screen.queryByLabelText('Price of seconds of transcribe/streaming')).toBeNull();
    expect(screen.getByLabelText('Price of vCPU-seconds of fargate/x86_64')).toBeTruthy();
  });

  it('offers no edit, import or add while the profile is read-only', () => {
    render(
      <SectionProvider values={DEFAULTS} defaults={DEFAULTS} problems={[]} readOnly onChange={() => {}}>
        <PriceTable schema={SCHEMA} />
      </SectionProvider>,
    );
    expect(screen.queryByRole('button', { name: 'Import prices' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Add a price' })).toBeNull();
    expect(screen.getByLabelText('Price of seconds of transcribe/streaming').disabled).toBe(true);
  });
});
