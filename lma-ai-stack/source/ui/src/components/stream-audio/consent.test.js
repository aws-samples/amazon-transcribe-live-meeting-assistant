/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Stream Audio consent step's rules: the notice is filled as the server
 * fills it, opt-in may not start unconfirmed while notify may, the START
 * payload carries what the user confirmed, and a CONSENT_STATE with
 * captureAllowed false means the transcriber stopped the meeting.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

import {
  captureStopped,
  consentPayload,
  consentRequired,
  describeConsentState,
  parseServerFrame,
  renderNotice,
  startBlockedReason,
} from './consent';

const DEFAULTS = JSON.parse(
  readFileSync(
    join(import.meta.dirname, '../../../../lambda_functions/configuration_seeder/defaults/MeetingConsent.json'),
    'utf8',
  ),
);

describe('renderNotice', () => {
  it('fills the shipped notice as the server does', () => {
    expect(
      renderNotice(DEFAULTS.notifyNoticeText, {
        lmaUser: 'Ana',
        meeting: 'Weekly sync',
        objectionPhrase: 'I object',
      }),
    ).toBe(
      'Ana is using Live Meeting Assistant to record and transcribe this meeting (Weekly sync). ' +
        'If you do not agree, reply "I object" in the chat or say so, and recording and transcription will stop.',
    );
    expect(renderNotice('{LMA_USER}, {meeting}, {other}', {})).toBe('The meeting organizer, this meeting, {other}');
  });
});

describe('startBlockedReason', () => {
  const optIn = { available: true, mode: 'opt_in', supported: true };
  it('holds an opt-in meeting until it is confirmed with a method', () => {
    expect(startBlockedReason(optIn, { confirmed: false, method: 'verbal' })).toMatch(/Confirm/);
    expect(startBlockedReason(optIn, { confirmed: true, method: '' })).toMatch(/how everyone agreed/);
    expect(startBlockedReason(optIn, { confirmed: true, method: 'chat' })).toBe('');
  });
  it('lets a notify meeting start unconfirmed, and any meeting when consent is off or unknown', () => {
    expect(startBlockedReason({ available: true, mode: 'notify' }, { confirmed: false })).toBe('');
    expect(startBlockedReason({ available: true, mode: 'off' }, { confirmed: false })).toBe('');
    expect(startBlockedReason(null, { confirmed: false })).toBe('');
    expect(consentRequired({ available: false, mode: 'opt_in' })).toBe(false);
  });
  it('refuses a deployment whose rule this page cannot meet', () => {
    expect(startBlockedReason({ ...optIn, supported: false, reason: 'Use a Virtual Participant.' }, {})).toBe(
      'Use a Virtual Participant.',
    );
  });
});

describe('consentPayload', () => {
  it('carries the confirmation, its method and the notice as shown', () => {
    expect(
      consentPayload(
        { mode: 'opt_in' },
        { confirmed: true, method: 'chat', statement: '  everyone typed I agree ' },
        { lmaUser: 'Ana', meetingName: 'Sync', noticeText: 'Ana would like…' },
      ),
    ).toEqual({
      mode: 'opt_in',
      confirmed: true,
      method: 'chat',
      noticeText: 'Ana would like…',
      lmaUser: 'Ana',
      meetingName: 'Sync',
      statement: 'everyone typed I agree',
      client: 'Stream Audio',
    });
  });
});

describe('server frames', () => {
  it('reads only JSON frames with an event', () => {
    expect(parseServerFrame('{"event":"CONSENT_SETTINGS","mode":"notify"}')).toEqual({
      event: 'CONSENT_SETTINGS',
      mode: 'notify',
    });
    expect(parseServerFrame('not json')).toBeNull();
    expect(parseServerFrame('{"callEvent":"START"}')).toBeNull();
    expect(parseServerFrame(new ArrayBuffer(2))).toBeNull();
  });
  it('treats captureAllowed false as the meeting having been stopped', () => {
    expect(captureStopped({ event: 'CONSENT_STATE', captureAllowed: false })).toBe(true);
    expect(captureStopped({ event: 'CONSENT_STATE', code: 'EVIDENCE_REFUSED' })).toBe(false);
    expect(captureStopped({ event: 'CONSENT_STATE', captureAllowed: true })).toBe(false);
  });
  it('describes the record, an objection and an evidence check', () => {
    expect(describeConsentState({ captureAllowed: true, status: 'granted' }).type).toBe('success');
    expect(describeConsentState({ captureAllowed: false, status: 'objected' }).type).toBe('error');
    expect(describeConsentState({ captureAllowed: false, message: 'Refused.' })).toEqual({
      type: 'error',
      text: 'Refused.',
    });
    expect(describeConsentState({ evidence: { stored: true, verdict: 'inconclusive' } }).text).toMatch(/inconclusive/);
    expect(describeConsentState(null)).toBeNull();
  });
});
