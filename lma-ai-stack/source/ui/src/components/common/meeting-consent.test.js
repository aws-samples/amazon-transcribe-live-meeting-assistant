/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it } from 'vitest';

import {
  buildTimeline,
  consentAsText,
  isPlaceholderSeat,
  latestDecisions,
  participantRows,
  statusIndicatorType,
  unidentifiedLabel,
  unidentifiedPresentCount,
} from './meeting-consent';
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

  it('shows placeholder seats as one count while present, and not at all once they are not', () => {
    const consent = {
      Mode: 'opt_in',
      Participants: ['Ana Silva', 'Unidentified participant 1', 'Unidentified participant 2'],
      ParticipantsSeen: [
        'Ana Silva',
        'Unidentified participant 1',
        'Unidentified participant 2',
        'Unidentified participant 3',
      ],
      Events: [
        { ParticipantName: 'Ana Silva', Decision: 'consent', Source: 'chat' },
        // A reply with no sender is recorded under the name without a number.
        { ParticipantName: 'Unidentified participant', Decision: 'objection', Source: 'chat' },
      ],
    };
    expect(isPlaceholderSeat('Unidentified participant 3')).toBe(true);
    expect(isPlaceholderSeat('unidentified  PARTICIPANT 12')).toBe(true);
    expect(isPlaceholderSeat('Unidentified participant')).toBe(false);
    expect(isPlaceholderSeat('Unidentified participant Bob')).toBe(false);
    expect(participantRows(consent)).toEqual([
      { name: 'Ana Silva', present: true, decision: 'consent' },
      { name: 'Unidentified participant', present: false, decision: 'objection' },
    ]);
    expect(unidentifiedPresentCount(consent)).toBe(2);
    expect(unidentifiedLabel(2)).toBe('2 participants not yet identified');
    expect(unidentifiedLabel(1)).toBe('1 participant not yet identified');
    const text = consentAsText(consent);
    expect(text).toContain('- 2 participants not yet identified');
    expect(text).not.toContain('Unidentified participant 1');
    expect(text).not.toContain('Unidentified participant 3');
    // Nobody unidentified present: no line.
    expect(unidentifiedPresentCount({ Participants: ['Ana Silva'], Events: [] })).toBe(0);
    expect(consentAsText({ Mode: 'opt_in', Participants: ['Ana Silva'] })).not.toContain('not yet identified');
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
    expect(text).toContain('- Cy Diaz: no reply (not present at the time of this download)');
    expect(text).toContain(
      '- screenshot captured 2026-11-02T10:02:30.000Z. ' +
        'Image check: Objection found (model amazon.nova-lite-v1:0, confidence 0.9)',
    );
    expect(text).toContain('not a legal determination');
    expect(consentAsText(null)).toBe('');
  });

  it('describes each evidence image by its note in the download', () => {
    const text = consentAsText({
      ...CONSENT,
      Evidence: [
        { ...CONSENT.Evidence[0], Note: 'Meeting chat when Bo Chen objected' },
        {
          EvidenceId: 'e2',
          Kind: 'screenshot',
          CapturedAt: '2026-11-02T10:02:31.000Z',
          Note: 'Participant list\nwhen Bo Chen objected',
        },
      ],
    });
    expect(text).toContain(
      '- Meeting chat when Bo Chen objected (screenshot captured 2026-11-02T10:02:30.000Z). ' +
        'Image check: Objection found (model amazon.nova-lite-v1:0, confidence 0.9)',
    );
    expect(text).toContain(
      '- Participant list when Bo Chen objected (screenshot captured 2026-11-02T10:02:31.000Z). ' +
        'Not checked: participant lists are kept as a record',
    );
  });

  it('says a chat image with no check was not run, and names consent given outside the chat', () => {
    const text = consentAsText({
      ...CONSENT,
      Evidence: [
        { EvidenceId: 'e3', Kind: 'screenshot', CapturedAt: 'x', Note: 'Meeting chat when Bo Chen consented' },
        {
          EvidenceId: 'e4',
          Kind: 'screenshot',
          CapturedAt: 'y',
          S3Key: 'lma-consent-evidence/m1/2026-11-02T10-02-31-000Z-participants-abc123.jpg',
        },
        {
          EvidenceId: 'e5',
          Kind: 'screenshot',
          CapturedAt: 'z',
          Note: 'Meeting chat when Jeremy Feldman consented',
          Verification: {
            Verdict: 'supports_consent',
            ModelId: 'm',
            Confidence: 0.8,
            ConsentedOutsideChat: ['Bo Chen'],
          },
        },
      ],
    });
    expect(text).toContain('- Meeting chat when Bo Chen consented (screenshot captured x). Image check: not run');
    expect(text).toContain('- screenshot captured y. Not checked: participant lists are kept as a record');
    expect(text).toContain(
      'Image check: Supports consent (model m, confidence 0.8). ' +
        'Consented aloud or in LMA, so not expected in this image: Bo Chen',
    );
  });

  it('adds the record to the summary download only when there is one', () => {
    const meeting = { callId: 'm1', callSummaryText: 'All good.' };
    expect(getTextFileFormattedMeetingDetails(meeting)).not.toContain('CONSENT');
    expect(getTextFileFormattedMeetingDetails(meeting, CONSENT)).toContain('Mode: Opt-in (consent needed first)');
  });
});
