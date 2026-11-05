/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, expect, it } from 'vitest';

import {
  CONSENT_PAUSE_GRACE_MS,
  SOURCE_API,
  SOURCE_BROWSER_EXTENSION,
  SOURCE_DESKTOP_APP,
  SOURCE_STREAM_AUDIO,
  SOURCE_UNKNOWN,
  SOURCE_UPLOAD,
  SOURCE_VIRTUAL_PARTICIPANT,
  captureSource,
  consentPauseDueAt,
  consentSummary,
  hasRecordedCaptureContext,
  recordedSpeakerSeparation,
  speakerSeparation,
  transcriptionEngine,
  transcriptionLanguage,
  transcriptRetention,
} from './meeting-context';

describe('the capture context a meeting recorded', () => {
  it.each([
    ['VIRTUAL_PARTICIPANT', SOURCE_VIRTUAL_PARTICIPANT, 'Virtual Participant'],
    ['STREAM_AUDIO', SOURCE_STREAM_AUDIO, 'Stream Audio'],
    ['BROWSER_EXTENSION', SOURCE_BROWSER_EXTENSION, 'Browser extension'],
    ['DESKTOP_APP', SOURCE_DESKTOP_APP, 'Desktop Capture App'],
    ['UPLOAD', SOURCE_UPLOAD, 'Uploaded recording'],
    ['API', SOURCE_API, 'Another client of the streaming API (such as an MCP client)'],
  ])('reads CaptureSource %s as recorded, whatever the Meeting ID looks like', (stored, kind, label) => {
    // A Meeting ID in the Stream Audio form, which the recorded source wins over.
    const item = { callId: 'Weekly sync - 2026-11-03-14:05:09.123', captureSource: stored };
    expect(captureSource(item)).toEqual({ kind, label, inferred: false });
  });

  it('shows an unknown CaptureSource as it is, and infers only when there is none', () => {
    expect(captureSource({ callId: 'x', captureSource: 'KIOSK' })).toMatchObject({ label: 'KIOSK', inferred: false });
    expect(captureSource({ callId: 'Weekly sync - 2026-11-03-14:05:09.123', captureSource: '' }).inferred).toBe(true);
  });

  it('names the engine, the language setting and the speaker separation asked for', () => {
    expect(transcriptionEngine({ transcriptionEngine: 'TRANSCRIBE' })).toBe('Amazon Transcribe');
    expect(transcriptionEngine({ transcriptionEngine: 'MICROVM_ASR' })).toBe('ASR MicroVM');
    expect(transcriptionLanguage({ transcriptionLanguage: 'en-US' })).toBe('en-US');
    expect(transcriptionLanguage({ transcriptionLanguage: 'identify-multiple-languages' })).toBe(
      'Identified by Amazon Transcribe (several languages)',
    );
    expect(transcriptionLanguage({ transcriptionEngine: 'MICROVM_ASR' })).toBe('Set by the ASR model');
    expect(recordedSpeakerSeparation({ speakerSeparation: 'REMOTE' })).toBe('On for the meeting audio');
    expect(recordedSpeakerSeparation({ speakerSeparation: 'NONE' })).toBe('Off');
  });

  it('knows nothing of these for a meeting started before they were recorded', () => {
    const older = { callId: 'Weekly sync-2026-11-03-14-05-09-123' };
    expect(hasRecordedCaptureContext(older)).toBe(false);
    expect(transcriptionEngine(older)).toBeNull();
    expect(transcriptionLanguage(older)).toBeNull();
    expect(recordedSpeakerSeparation(older)).toBeNull();
    expect(hasRecordedCaptureContext({ transcriptionEngine: 'TRANSCRIBE' })).toBe(true);
  });
});

describe('captureSource', () => {
  it('names a Virtual Participant meeting from its own labels or its linked record, as recorded', () => {
    const labelled = { callId: 'Standup', remoteParticipantName: 'Virtual Participant', systemLabel: 'LMA System' };
    expect(captureSource(labelled)).toMatchObject({ kind: SOURCE_VIRTUAL_PARTICIPANT, inferred: false });
    expect(captureSource({ callId: 'Standup' }, { id: 'vp-1' })).toMatchObject({
      kind: SOURCE_VIRTUAL_PARTICIPANT,
      inferred: false,
    });
  });

  it.each([
    ['Weekly sync-2026-11-03-14-05-09-123', SOURCE_VIRTUAL_PARTICIPANT],
    ['Weekly sync - 2026-11-03-14:05:09.123', SOURCE_STREAM_AUDIO],
    ['TOH5 - 2026-08-14 12:12', SOURCE_DESKTOP_APP],
    ['LMA native prototype - 2026-11-03T14:05:09Z', SOURCE_DESKTOP_APP],
    ['weekly-sync-20261103T140509-1a2b3c4d', SOURCE_UPLOAD],
  ])('reads %s as %s, from the form of its Meeting ID', (callId, kind) => {
    expect(captureSource({ callId, remoteParticipantName: 'Others', systemLabel: 'System' })).toMatchObject({
      kind,
      inferred: true,
    });
  });

  it('says it does not know for a Meeting ID of no known form', () => {
    expect(captureSource({ callId: 'my meeting' })).toMatchObject({ kind: SOURCE_UNKNOWN, inferred: false });
    expect(captureSource({})).toMatchObject({ kind: SOURCE_UNKNOWN });
  });
});

describe('speakerSeparation', () => {
  it('counts the voices the engine labelled, merged ones included', () => {
    const roster = [
      { channel: 'REMOTE', voiceId: '(spk_0)', mergedKeys: ['REMOTE#x (spk_2)'] },
      { channel: 'REMOTE', voiceId: '(spk_1)', mergedKeys: [] },
      { channel: 'LOCAL', voiceId: '', mergedKeys: [] },
    ];
    expect(speakerSeparation(roster)).toEqual({ separated: true, voices: 3, splitChannel: true });
  });

  it('says no voices were separated when none carries a voice id, and nothing without speech', () => {
    expect(speakerSeparation([{ channel: 'REMOTE', voiceId: '' }])).toEqual({
      separated: false,
      voices: 0,
      splitChannel: false,
    });
    expect(speakerSeparation([])).toBeNull();
  });
});

describe('consentSummary', () => {
  it('knows nothing until the record or the settings have been read', () => {
    expect(consentSummary({ consent: null, settings: null }).known).toBe(false);
    expect(consentSummary(null).known).toBe(false);
  });

  it('reads a meeting with no record, or one in mode off, as consent enforcement off', () => {
    expect(consentSummary({ consent: null, settings: { Mode: 'opt_in' } })).toMatchObject({
      known: true,
      enforced: false,
      text: 'Consent enforcement off',
    });
    expect(consentSummary({ consent: { Mode: 'off', Status: 'not_required' } }).enforced).toBe(false);
  });

  it('gives the mode and the status of an enforced record', () => {
    const state = { consent: { Mode: 'opt_in', Status: 'granted', CaptureAllowed: true }, captureAllowed: true };
    expect(consentSummary(state)).toMatchObject({ enforced: true, text: 'Opt-in · granted', captureAllowed: true });
    const notify = { consent: { Mode: 'notify', Status: 'not_required', CaptureAllowed: true } };
    expect(consentSummary(notify).text).toBe('Notify · not required');
  });

  it('takes capture as withheld when either the record or the answer says so', () => {
    const pending = { consent: { Mode: 'opt_in', Status: 'pending', CaptureAllowed: false }, captureAllowed: true };
    expect(consentSummary(pending).captureAllowed).toBe(false);
    const answer = { consent: { Mode: 'opt_in', Status: 'pending' }, captureAllowed: false };
    expect(consentSummary(answer).captureAllowed).toBe(false);
  });
});

describe('consentPauseDueAt', () => {
  const pending = consentSummary({ consent: { Mode: 'opt_in', Status: 'pending', CaptureAllowed: false } });

  it('is due a grace period after the meeting, or its consent, started', () => {
    const startedAt = '2026-11-05T10:00:00.000Z';
    const due = Date.parse(startedAt) + CONSENT_PAUSE_GRACE_MS;
    expect(consentPauseDueAt({ isLive: true, summary: pending, startedAt })).toBe(due);
    expect(
      consentPauseDueAt({ isLive: true, summary: pending, startedAt, consentStartedAt: '2026-11-05T10:01:00.000Z' }),
    ).toBe(Date.parse('2026-11-05T10:01:00.000Z') + CONSENT_PAUSE_GRACE_MS);
  });

  it('is never due for a meeting that has ended, allows capture, or has consent off', () => {
    const startedAt = '2026-11-05T10:00:00.000Z';
    expect(consentPauseDueAt({ isLive: false, summary: pending, startedAt })).toBeNull();
    const granted = consentSummary({ consent: { Mode: 'opt_in', Status: 'granted', CaptureAllowed: true } });
    expect(consentPauseDueAt({ isLive: true, summary: granted, startedAt })).toBeNull();
    expect(consentPauseDueAt({ isLive: true, summary: consentSummary({ consent: null }), startedAt })).toBeNull();
  });
});

describe('transcriptRetention', () => {
  it('says when a transcript was deleted, and how much went', () => {
    const item = {
      transcriptDeletedAt: '2026-11-05T11:00:00Z',
      transcriptSegmentsDeleted: 412,
      transcriptFilesDeleted: 2,
      expiresAfter: 1793000000,
    };
    expect(transcriptRetention(item)).toEqual({
      state: 'deleted',
      at: '2026-11-05T11:00:00Z',
      segments: 412,
      files: 2,
      recordExpiresAt: new Date(1793000000 * 1000).toISOString(),
    });
  });

  it('says when a transcript is to be deleted, and that one is kept otherwise', () => {
    expect(transcriptRetention({ transcriptDeletionScheduledFor: '2026-11-05T11:30:00Z' })).toMatchObject({
      state: 'scheduled',
      at: '2026-11-05T11:30:00Z',
    });
    expect(transcriptRetention({})).toEqual({ state: 'kept', at: null, recordExpiresAt: null });
  });
});

describe('transcriptionLanguage on the ASR MicroVM', () => {
  it('reports the model language even when a language from START is still stored', () => {
    expect(transcriptionLanguage({ transcriptionEngine: 'MICROVM_ASR', transcriptionLanguage: 'en-US' })).toBe(
      'Set by the ASR model',
    );
  });
});
