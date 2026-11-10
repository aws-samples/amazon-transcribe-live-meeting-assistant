/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The alert that transcription is paused for consent: shown only for a live
 * meeting with enforced consent that withholds capture, and only once the
 * meeting has run for the grace period.
 */
import React from 'react';
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest';
import { act, render, screen, fireEvent } from '@testing-library/react';

import ConsentPauseAlert from './ConsentPauseAlert';
import { CONSENT_PAUSE_GRACE_MS } from '../common/meeting-context';

const STARTED = '2026-11-05T10:00:00.000Z';
const PENDING = {
  consent: { Mode: 'opt_in', Status: 'pending', CaptureAllowed: false },
  captureAllowed: false,
  settings: { Mode: 'opt_in' },
};
const HEADER = 'Transcription is paused until consent is recorded';

const renderAlert = ({ isLive = true, consentState = PENDING, onShowRecord = vi.fn() } = {}) =>
  render(
    <ConsentPauseAlert isLive={isLive} consentState={consentState} startedAt={STARTED} onShowRecord={onShowRecord} />,
  );

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(STARTED));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('ConsentPauseAlert', () => {
  it('waits out the grace period after the meeting started, then says transcription is paused', () => {
    renderAlert();
    expect(screen.queryByText(HEADER)).toBeNull();
    act(() => vi.advanceTimersByTime(CONSENT_PAUSE_GRACE_MS - 1000));
    expect(screen.queryByText(HEADER)).toBeNull();
    act(() => vi.advanceTimersByTime(1000));
    expect(screen.getByText(HEADER)).toBeTruthy();
    expect(screen.getByText(/consent setting is Opt-in \(consent needed first\)/)).toBeTruthy();
  });

  it('is shown at once for a meeting that started longer ago', () => {
    vi.setSystemTime(new Date(Date.parse(STARTED) + 5 * 60000));
    renderAlert();
    expect(screen.getByText(HEADER)).toBeTruthy();
  });

  it('goes once consent is recorded', () => {
    vi.setSystemTime(new Date(Date.parse(STARTED) + 60000));
    const { rerender } = renderAlert();
    expect(screen.getByText(HEADER)).toBeTruthy();
    const granted = { ...PENDING, consent: { ...PENDING.consent, Status: 'granted', CaptureAllowed: true } };
    rerender(<ConsentPauseAlert isLive consentState={{ ...granted, captureAllowed: true }} startedAt={STARTED} />);
    expect(screen.queryByText(HEADER)).toBeNull();
  });

  it('is not shown for an ended meeting, nor with consent enforcement off', () => {
    vi.setSystemTime(new Date(Date.parse(STARTED) + 60000));
    const { unmount } = renderAlert({ isLive: false });
    expect(screen.queryByText(HEADER)).toBeNull();
    unmount();
    renderAlert({ consentState: { consent: null, settings: { Mode: 'opt_in' } } });
    expect(screen.queryByText(HEADER)).toBeNull();
  });

  it('says so when a participant objected', () => {
    vi.setSystemTime(new Date(Date.parse(STARTED) + 60000));
    renderAlert({ consentState: { ...PENDING, consent: { ...PENDING.consent, Status: 'objected' } } });
    expect(screen.getByText('Transcription is paused: a participant objected')).toBeTruthy();
  });

  it('opens the consent record, and can be dismissed', () => {
    vi.setSystemTime(new Date(Date.parse(STARTED) + 60000));
    const onShowRecord = vi.fn();
    renderAlert({ onShowRecord });
    fireEvent.click(screen.getByRole('button', { name: 'Show the consent record' }));
    expect(onShowRecord).toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText(HEADER)).toBeNull();
  });
});
