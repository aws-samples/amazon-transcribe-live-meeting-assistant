/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting page's Live guidance panel on its own: the switch, and the
 * Digest so far button with each of its answers. CallPanel.test.jsx checks
 * that the page sends the switch as an updateCallGuidance change.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import LiveGuidancePanel, { DIGEST_FAILED_MESSAGE, DIGEST_PUBLISHED_MESSAGE } from './LiveGuidancePanel';
import { PREPARATION_MUTATIONS } from '../../hooks/use-calls-graphql-api';

vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));

const ITEM = { callId: 'm1', guidanceEnabled: false };

const renderPanel = (props = {}) =>
  render(
    <LiveGuidancePanel
      item={ITEM}
      isLive
      onToggle={vi.fn(async () => ({ ok: true }))}
      onRequestDigest={vi.fn(async () => ({ Success: true }))}
      // eslint-disable-next-line react/jsx-props-no-spreading
      {...props}
    />,
  );

describe('LiveGuidancePanel', () => {
  it('turns guidance on through the guidance change, which is updateCallGuidance', async () => {
    const onToggle = vi.fn(async () => ({ ok: true }));
    renderPanel({ onToggle });
    fireEvent.click(screen.getByRole('checkbox'));
    await waitFor(() => expect(onToggle).toHaveBeenCalledWith(true));
    const [field, query] = PREPARATION_MUTATIONS.guidance;
    expect(field).toBe('updateCallGuidance');
    expect(query).toContain('updateCallGuidance(input: $input)');
    expect(query).toContain('GuidanceEnabled');
  });

  it('shows the server message when the switch is refused', async () => {
    renderPanel({ onToggle: vi.fn(async () => ({ ok: false, message: 'You do not have permission.' })) });
    fireEvent.click(screen.getByRole('checkbox'));
    expect(await screen.findByText('You do not have permission.')).toBeTruthy();
  });

  it('cannot be switched once the meeting has ended, or by a viewer who may not change it', () => {
    renderPanel({ isLive: false });
    expect(screen.getByRole('checkbox').disabled).toBe(true);
    expect(screen.getByText('Live guidance runs only while the meeting is in progress.')).toBeTruthy();
  });

  it('is left out for a read-only viewer while guidance is off, and shown while it is on', () => {
    const { container, rerender } = renderPanel({ readOnly: true });
    expect(container.querySelector('[data-testid="live-guidance-panel"]')).toBeNull();
    rerender(<LiveGuidancePanel item={{ ...ITEM, guidanceEnabled: true }} readOnly isLive />);
    expect(screen.getByRole('checkbox').checked).toBe(true);
    expect(screen.getByRole('checkbox').disabled).toBe(true);
  });

  it('says the digest was published', async () => {
    const onRequestDigest = vi.fn(async () => ({ Success: true, Text: 'So far...' }));
    renderPanel({ onRequestDigest });
    fireEvent.click(screen.getByRole('button', { name: 'Digest so far' }));
    expect(await screen.findByText(DIGEST_PUBLISHED_MESSAGE)).toBeTruthy();
    expect(onRequestDigest).toHaveBeenCalledTimes(1);
  });

  it("shows the backend's reason when there is no digest", async () => {
    renderPanel({
      onRequestDigest: vi.fn(async () => ({ Success: false, Message: 'Nothing has been said in the meeting yet.' })),
    });
    fireEvent.click(screen.getByRole('button', { name: 'Digest so far' }));
    expect(await screen.findByText('Nothing has been said in the meeting yet.')).toBeTruthy();
  });

  it('says the digest may still arrive when the request failed or timed out', async () => {
    renderPanel({ onRequestDigest: vi.fn(async () => ({ Success: false, Failed: true })) });
    fireEvent.click(screen.getByRole('button', { name: 'Digest so far' }));
    expect(await screen.findByText(DIGEST_FAILED_MESSAGE)).toBeTruthy();
  });
});
