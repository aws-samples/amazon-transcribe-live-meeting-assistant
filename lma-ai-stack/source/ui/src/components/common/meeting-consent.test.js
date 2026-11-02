/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it } from 'vitest';

import { buildTimeline, consentAsText, latestDecisions, participantRows, statusIndicatorType } from './meeting-consent';
import { getTextFileFormattedMeetingDetails } from './summary';

const CONSENT = {
  Mode: 'opt_in',
  Rule: 'each_participant',
  Status: 'objected',
  CaptureAllowed: false,
  NoticeText: 'Ana would like to record this meeting. Reply "I consent".',
  NoticePostedAt: '2026-11-02T10:00:05.000Z',
  StartedAt: '2026-11-02T10:00:00.000Z',
  StartedBy: 'LMA',
  Notices: [{ Channel: 'chat', PostedAt: '2026-11-02T10:00:05.000Z', PostedBy: 'LMA' }],
  Participants: ['Ana Silva', 'Bo Chen'],
  ParticipantsSeen: ['Ana Silva', 'Bo Chen', 'Cy Diaz'],
  Events: [
    { ParticipantName: 'ana silva', Decision: 'consent', Source: 'chat', OccurredAt: '2026-11-02T10:01:00.000Z' },
    { ParticipantName: 'Bo Chen', Decision: 'objection', Source: 'voice', OccurredAt: '2026-11-02T10:02:00.000Z' },
    { ParticipantName: 'Bo Chen', Decision: 'consent', Source: 'attestation', OccurredAt: '2026-11-02T10:03:00.000Z' },
  ],
  StatusHistory: [
    { Status: 'pending', CaptureAllowed: false, At: '2026-11-02T10:00:00.000Z', Reason: 'no participants' },
    { Status: 'objected', CaptureAllowed: false, At: '2026-11-02T10:02:00.000Z', Reason: '1 participant objected' },
  ],
  Evidence: [
    {
      EvidenceId: 'e1',
      Kind: 'screenshot',
      CapturedAt: '2026-11-02T10:02:30.000Z',
      Verification: { Verdict: 'objection_found', ModelId: 'amazon.nova-lite-v1:0', Confidence: 0.9 },
    },
  ],
};

describe('meeting-consent', () => {
  it('takes each participant’s latest reply and leaves attestations out', () => {
    const decisions = latestDecisions(CONSENT.Events);
    expect(decisions.get('ana silva')).toBe('consent');
    expect(decisions.get('bo chen')).toBe('objection');
  });

  it('lists the participants present first, then the others seen', () => {
    expect(participantRows(CONSENT)).toEqual([
      { name: 'Ana Silva', present: true, decision: 'consent' },
      { name: 'Bo Chen', present: true, decision: 'objection' },
      { name: 'Cy Diaz', present: false, decision: null },
    ]);
  });

  it('orders the timeline by time, starting with the start of consent', () => {
    const kinds = buildTimeline(CONSENT).map((entry) => entry.kind);
    expect(kinds).toEqual(['started', 'status', 'notice', 'consent', 'objection', 'status', 'attestation']);
    expect(buildTimeline(null)).toEqual([]);
  });

  it('matches names as the backend case-folds them', () => {
    const consent = {
      Participants: ['Strauß'],
      Events: [{ ParticipantName: 'STRAUSS', Decision: 'consent', Source: 'chat' }],
    };
    expect(participantRows(consent)).toEqual([{ name: 'Strauß', present: true, decision: 'consent' }]);
  });

  it('orders the timeline by instant, and replies by when they were recorded', () => {
    const consent = {
      StatusHistory: [{ Status: 'objected', CaptureAllowed: false, At: '2026-11-02T10:00:00.900Z' }],
      Events: [
        {
          ParticipantName: 'Bo',
          Decision: 'objection',
          Source: 'chat',
          OccurredAt: '2026-11-02T11:00:00+01:00',
          RecordedAt: '2026-11-02T10:00:00.500Z',
        },
      ],
    };
    expect(buildTimeline(consent).map((entry) => entry.kind)).toEqual(['objection', 'status']);
  });

  it('maps statuses to indicator types', () => {
    expect(statusIndicatorType('granted')).toBe('success');
    expect(statusIndicatorType('objected')).toBe('error');
    expect(statusIndicatorType('unknown')).toBe('info');
  });

  it('writes the record as text with its caveat', () => {
    const text = consentAsText(CONSENT);
    expect(text).toContain('CONSENT');
    expect(text).toContain('Status: Objected');
    expect(text).toContain('- Bo Chen: objection');
    expect(text).toContain('- Cy Diaz: no reply (not present at the end)');
    expect(text).toContain('Objection found (model amazon.nova-lite-v1:0, confidence 0.9)');
    expect(text).toContain('not a legal determination');
    expect(consentAsText(null)).toBe('');
  });

  it('adds the record to the summary download only when there is one', () => {
    const meeting = { callId: 'm1', callSummaryText: 'All good.' };
    expect(getTextFileFormattedMeetingDetails(meeting)).not.toContain('CONSENT');
    expect(getTextFileFormattedMeetingDetails(meeting, CONSENT)).toContain('Mode: Opt-in (consent needed first)');
  });
});
