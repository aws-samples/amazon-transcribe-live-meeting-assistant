/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the Estimated cost panel must say.
 *
 * The number is the backend's; what the panel owns is how honestly it is
 * presented -- labelled as an estimate, with its unpriced lines and the date of
 * its prices visible, and a meeting with no metering data never shown as free.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import CallCostPanel from './CallCostPanel';

const ESTIMATE = {
  CallId: 'Meeting-1',
  Currency: 'USD',
  TotalCost: 0.2468,
  ContextTotals: [{ Context: 'Transcription', Cost: 0.2468 }],
  Items: [
    {
      Context: 'Transcription',
      Service: 'transcribe/streaming',
      Region: 'us-east-1',
      Unit: 'seconds',
      Quantity: 1480,
      UnitPrice: 0.0001667,
      Cost: 0.2468,
      Chargeable: true,
      Priced: true,
      Note: null,
      Details: 'channels: 2',
    },
    {
      Context: 'Transcription',
      Service: 'transcribe/streaming',
      Region: 'us-east-1',
      Unit: 'requests',
      Quantity: 2,
      UnitPrice: 0,
      Cost: 0,
      Chargeable: false,
      Priced: true,
    },
  ],
  UnpricedCount: 0,
  RecordCount: 2,
  PricesAsOf: '2026-10-28',
  PriceSource: 'AWS public on-demand list prices.',
  Region: 'us-east-1',
  BaseRegion: 'us-east-1',
  RegionsWithoutListedPrices: [],
  NotIncluded: ['NAT gateways'],
  ComputedAt: '2026-10-28T12:00:00Z',
};

const open = async () => {
  fireEvent.click(screen.getAllByText('Estimated cost')[0]);
};

describe('CallCostPanel', () => {
  it('shows the total as an estimate in the header, and the lines when opened', async () => {
    const loadCost = vi.fn().mockResolvedValue(ESTIMATE);
    render(<CallCostPanel callId="Meeting-1" loadCost={loadCost} />);
    await waitFor(() => expect(screen.getByText('$0.2468 estimated')).toBeTruthy());
    expect(loadCost).toHaveBeenCalledWith('Meeting-1');
    await open();
    expect(screen.getByText('This is an estimate')).toBeTruthy();
    expect(screen.getByText(/as of 2026-10-28/)).toBeTruthy();
    expect(screen.getAllByText('Amazon Transcribe — streaming').length).toBe(2);
    expect(screen.getByText('not billed separately')).toBeTruthy();
    expect(screen.getByText('Total: $0.2468')).toBeTruthy();
  });

  it('shows units, unit price and cost per line, and where the price came from', async () => {
    const sourced = {
      ...ESTIMATE,
      Items: [
        {
          ...ESTIMATE.Items[0],
          PriceOrigin: 'shipped',
          ShippedUnitPrice: 0.0001667,
          PriceSource: 'AWS Price List API (offer transcribe, usage type USE1-StreamingAudio, read 2026-11-09)',
          PriceSourceUrl: 'https://aws.amazon.com/transcribe/pricing/',
        },
        ESTIMATE.Items[1],
      ],
    };
    render(<CallCostPanel callId="Meeting-1" loadCost={vi.fn().mockResolvedValue(sourced)} />);
    await waitFor(() => expect(screen.getByText('$0.2468 estimated')).toBeTruthy());
    await open();
    expect(screen.getByText('Usage')).toBeTruthy();
    expect(screen.getByText('× Unit price')).toBeTruthy();
    expect(screen.getByText('= Estimated cost')).toBeTruthy();
    expect(screen.getByText('1,480 seconds')).toBeTruthy();
    expect(screen.getByText('$0.0001667')).toBeTruthy();
    expect(screen.getByTitle('1,480 seconds × $0.0001667 = $0.2468')).toBeTruthy();
    fireEvent.click(screen.getByText('AWS Price List API'));
    expect(await screen.findByText(/usage type USE1-StreamingAudio, read 2026-11-09/)).toBeTruthy();
    expect(screen.getByText('Pricing page').closest('a').getAttribute('href')).toBe(
      'https://aws.amazon.com/transcribe/pricing/',
    );
  });

  it("shows a price set in the configuration as the administrator's, beside the shipped one", async () => {
    const custom = {
      ...ESTIMATE,
      CustomPriceCount: 1,
      PriceProfile: 'negotiated',
      Items: [
        {
          ...ESTIMATE.Items[0],
          UnitPrice: 0.0001,
          Cost: 0.148,
          PriceOrigin: 'custom',
          ShippedUnitPrice: 0.0001667,
          PriceSource: 'Set in the negotiated configuration profile. The shipped price is $0.0001667.',
        },
      ],
    };
    render(<CallCostPanel callId="Meeting-1" loadCost={vi.fn().mockResolvedValue(custom)} />);
    await waitFor(() => expect(screen.getByText('$0.2468 estimated')).toBeTruthy());
    await open();
    expect(screen.getByText('Your price')).toBeTruthy();
    expect(screen.getByText('shipped $0.0001667')).toBeTruthy();
    expect(screen.getByText(/1 line is priced at a price set in the negotiated configuration profile/)).toBeTruthy();
  });

  it('says the total is incomplete when a line has no price', async () => {
    const loadCost = vi.fn().mockResolvedValue({
      ...ESTIMATE,
      UnpricedCount: 1,
      Items: [{ ...ESTIMATE.Items[0], Priced: false, Cost: null, UnitPrice: null }],
    });
    render(<CallCostPanel callId="Meeting-1" loadCost={loadCost} />);
    await waitFor(() => expect(screen.getByText('$0.2468 estimated (incomplete)')).toBeTruthy());
    await open();
    expect(screen.getByText(/1 line has no price/)).toBeTruthy();
    expect(screen.getAllByText('No price').length).toBeGreaterThan(0);
  });

  it('does not show a meeting with no metering records as costing nothing', async () => {
    const loadCost = vi.fn().mockResolvedValue({ ...ESTIMATE, RecordCount: 0, Items: [], TotalCost: 0 });
    render(<CallCostPanel callId="Meeting-1" loadCost={loadCost} />);
    await waitFor(() => expect(screen.getByText('No metering data')).toBeTruthy());
    await open();
    expect(screen.getByText(/No usage has been recorded for this meeting/)).toBeTruthy();
    expect(screen.queryByText('$0.0000 estimated')).toBeNull();
  });

  it('reports a failed load rather than a figure', async () => {
    const loadCost = vi.fn().mockRejectedValue({ errors: [{ message: 'Unauthorized' }] });
    render(<CallCostPanel callId="Meeting-1" loadCost={loadCost} />);
    await waitFor(() => expect(screen.getByText('Unavailable')).toBeTruthy());
    await open();
    expect(screen.getByText('Unauthorized')).toBeTruthy();
  });

  it('does not keep showing an earlier table when a later load fails', async () => {
    const loadCost = vi
      .fn()
      .mockResolvedValueOnce(ESTIMATE)
      .mockRejectedValueOnce({ errors: [{ message: 'Throttled' }] });
    const { rerender } = render(<CallCostPanel callId="Meeting-1" refreshKey="a" loadCost={loadCost} />);
    await waitFor(() => expect(screen.getByText('$0.2468 estimated')).toBeTruthy());
    rerender(<CallCostPanel callId="Meeting-1" refreshKey="b" loadCost={loadCost} />);
    await waitFor(() => expect(screen.getByText('Unavailable')).toBeTruthy());
    expect(screen.queryByText('Total: $0.2468')).toBeNull();
  });

  it('keeps the newest answer when an older request finishes last', async () => {
    let releaseFirst;
    const first = new Promise((resolve) => {
      releaseFirst = () => resolve({ ...ESTIMATE, TotalCost: 1 });
    });
    const loadCost = vi.fn().mockReturnValueOnce(first).mockResolvedValueOnce(ESTIMATE);
    const { rerender } = render(<CallCostPanel callId="Meeting-1" refreshKey="a" loadCost={loadCost} />);
    rerender(<CallCostPanel callId="Meeting-1" refreshKey="b" loadCost={loadCost} />);
    await waitFor(() => expect(screen.getByText('$0.2468 estimated')).toBeTruthy());
    releaseFirst();
    await first;
    expect(screen.getByText('$0.2468 estimated')).toBeTruthy();
  });

  it('loads again when the meeting status changes', async () => {
    const loadCost = vi.fn().mockResolvedValue(ESTIMATE);
    const { rerender } = render(<CallCostPanel callId="Meeting-1" refreshKey="In Progress" loadCost={loadCost} />);
    await waitFor(() => expect(loadCost).toHaveBeenCalledTimes(1));
    rerender(<CallCostPanel callId="Meeting-1" refreshKey="Done" loadCost={loadCost} />);
    await waitFor(() => expect(loadCost).toHaveBeenCalledTimes(2));
  });
});
