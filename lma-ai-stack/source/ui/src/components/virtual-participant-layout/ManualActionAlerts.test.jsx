/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect, vi } from 'vitest';

import { isUndescribedPrompt, shouldClearManualAlert } from './ManualActionAlerts';
import { VP_STATUS_CONFIG } from './VirtualParticipantDetails';

// Both modules call generateClient() / ConsoleLogger at module load; stub
// aws-amplify so importing them stays side-effect free.
vi.mock('aws-amplify/api', () => ({ generateClient: () => ({ graphql: vi.fn() }) }));
vi.mock('aws-amplify/utils', () => ({ ConsoleLogger: class {} }));

describe('shouldClearManualAlert', () => {
  it('keeps the alert up only while the prompt is open', () => {
    expect(shouldClearManualAlert('MANUAL_ACTION_REQUIRED')).toBe(false);
  });

  it('clears the alert on every other status the VP can report', () => {
    // Including the pre-admission ones. A prompt raised during sign-in or
    // pre-join resumes CONNECTING or JOINING when it is cleared, so an alert
    // that only cleared on JOINED / ACTIVE / COMPLETED would stay on screen
    // until the VP joined — or for the whole meeting, if the prompt was the
    // reason it never did.
    const statuses = Object.keys(VP_STATUS_CONFIG).filter((s) => s !== 'MANUAL_ACTION_REQUIRED');
    expect(statuses).toContain('JOINING');
    expect(statuses).toContain('CONNECTING');
    statuses.forEach((status) => {
      expect(shouldClearManualAlert(status), status).toBe(true);
    });
  });

  it('treats an update carrying no status as saying nothing about the prompt', () => {
    expect(shouldClearManualAlert(undefined)).toBe(false);
    expect(shouldClearManualAlert(null)).toBe(false);
    expect(shouldClearManualAlert('')).toBe(false);
  });
});

describe('isUndescribedPrompt', () => {
  // A subscription payload carries the fields the mutation behind it selected.
  // An update written by a caller that did not select the prompt fields arrives
  // with the prompt status and nulls, which is not something to act on: the
  // real message would be replaced by generic text and the notification would
  // fire a second time.
  it('recognises a prompt status with no prompt to show', () => {
    expect(isUndescribedPrompt({ status: 'MANUAL_ACTION_REQUIRED' })).toBe(true);
    expect(isUndescribedPrompt({ status: 'MANUAL_ACTION_REQUIRED', manualActionType: null })).toBe(true);
  });

  it('accepts a prompt that describes itself', () => {
    expect(
      isUndescribedPrompt({
        status: 'MANUAL_ACTION_REQUIRED',
        manualActionType: 'LOGIN',
        manualActionMessage: 'Sign in via the live view',
      }),
    ).toBe(false);
  });

  it('says nothing about an update that is not reporting a prompt', () => {
    expect(isUndescribedPrompt({ status: 'ACTIVE' })).toBe(false);
    expect(isUndescribedPrompt({ status: 'FAILED', errorMessage: 'Wrong meeting password' })).toBe(false);
    expect(isUndescribedPrompt({})).toBe(false);
    expect(isUndescribedPrompt(null)).toBe(false);
  });
});
