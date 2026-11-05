/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Details tab's "Capture and processing": where the meeting came from, the
 * consent setting that applied, and what happened to its transcript, so a
 * transcript that is missing can be explained from the page.
 */
import React from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import CaptureContextPanel from './CaptureContextPanel';
import { DONE_STATUS, IN_PROGRESS_STATUS } from '../common/get-recording-status';

const ITEM = {
  callId: 'Weekly sync-2026-11-03-14-05-09-123',
  remoteParticipantName: 'Virtual Participant',
  systemLabel: 'LMA System',
  recordingStatusLabel: DONE_STATUS,
  recordingUrl: 's3://bucket/a.wav',
};
const OPT_IN = {
  consent: { Mode: 'opt_in', Rule: 'each_participant', Status: 'granted', CaptureAllowed: true },
  captureAllowed: true,
  settings: { Mode: 'opt_in' },
};

const renderPanel = ({
  item = ITEM,
  vp = null,
  platform = '',
  roster = [],
  consentState = null,
  onShowConsentRecord,
} = {}) =>
  render(
    <CaptureContextPanel
      item={item}
      vp={vp}
      platform={platform}
      roster={roster}
      consentState={consentState}
      onShowConsentRecord={onShowConsentRecord}
    />,
  );

describe('CaptureContextPanel', () => {
  it('names a Virtual Participant meeting with its platform, and links to the Virtual Participant', () => {
    renderPanel({ vp: { id: 'vp-7', enableVideoRecording: false }, platform: 'Microsoft Teams' });
    expect(screen.getByText('Virtual Participant · Microsoft Teams')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Virtual Participant vp-7' }).getAttribute('href')).toBe(
      '#/virtual-participant/vp-7',
    );
    expect(screen.getByText('Recorded')).toBeTruthy();
    expect(screen.getByText('Off for this Virtual Participant')).toBeTruthy();
  });

  it('says when the source is read from the form of the Meeting ID', () => {
    const item = { ...ITEM, callId: 'TOH5 - 2026-08-14 12:12', remoteParticipantName: 'Others', systemLabel: 'System' };
    renderPanel({ item });
    expect(screen.getByText('Desktop Capture App')).toBeTruthy();
    expect(screen.getByText(/Read from the form of the Meeting ID/)).toBeTruthy();
  });

  it('counts the voices told apart', () => {
    const roster = [
      { key: 'a', channel: 'REMOTE', voiceId: '(spk_0)', mergedKeys: [] },
      { key: 'b', channel: 'REMOTE', voiceId: '(spk_1)', mergedKeys: [] },
    ];
    renderPanel({ roster });
    expect(screen.getByText('On: 2 voices told apart')).toBeTruthy();
  });

  it('highlights an enforced consent setting and opens its record', () => {
    const onShowConsentRecord = vi.fn();
    renderPanel({ consentState: OPT_IN, onShowConsentRecord });
    expect(screen.getByText('Opt-in (consent needed first)')).toBeTruthy();
    expect(screen.getByText('granted')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'View the consent record' }));
    expect(onShowConsentRecord).toHaveBeenCalled();
  });

  it('says consent was off for a meeting with no record', () => {
    renderPanel({ consentState: { consent: null, settings: { Mode: 'off' } } });
    expect(screen.getByText(/No consent notice was required/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'View the consent record' })).toBeNull();
  });

  it('explains a deleted transcript: when, why, and what is kept', () => {
    renderPanel({
      item: {
        ...ITEM,
        transcriptDeletedAt: '2026-11-03T15:00:00Z',
        transcriptSegmentsDeleted: 120,
        transcriptFilesDeleted: 1,
      },
    });
    expect(screen.getByText(/^Deleted \d/)).toBeTruthy();
    expect(
      screen.getByText(/Deleted on schedule by the Retention setting "Delete the transcript after the summary"/),
    ).toBeTruthy();
    expect(screen.getByText(/120 segments and 1 file removed/)).toBeTruthy();
    expect(screen.getByText('Not known: the transcript was deleted')).toBeTruthy();
  });

  it('says when a transcript is to be deleted, and that one is kept otherwise', () => {
    const { unmount } = renderPanel({ item: { ...ITEM, transcriptDeletionScheduledFor: '2026-11-03T15:30:00Z' } });
    expect(screen.getByText(/^To be deleted /)).toBeTruthy();
    unmount();
    renderPanel({ item: { ...ITEM, expiresAfter: 1793000000 } });
    expect(screen.getByText(/Kept for the deployment's days to keep transcript segments/)).toBeTruthy();
    expect(screen.getByText(/The meeting record is kept until/)).toBeTruthy();
  });

  it('says a live meeting has no recordings yet', () => {
    renderPanel({ item: { ...ITEM, recordingStatusLabel: IN_PROGRESS_STATUS, recordingUrl: undefined } });
    expect(screen.getByText('Stored when the meeting ends, if audio is recorded')).toBeTruthy();
  });
});
