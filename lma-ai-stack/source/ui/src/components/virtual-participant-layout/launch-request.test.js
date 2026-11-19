/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { LAUNCH_FIELDS, launchStateMachineInput } from './launch-request';

const FORM_LAUNCH = {
  meetingPlatform: 'ZOOM',
  meetingID: '123456789',
  meetingPassword: 'pw-from-the-form',
  meetingName: 'Weekly sync',
  meetingTime: '',
  userName: 'owner@example.com',
  virtualParticipantId: 'vp-1',
  userSub: 'sub-1',
  userZoomSub: '',
  enableVideoRecording: 'true',
  accessToken: 'a',
  idToken: 'i',
  rereshToken: '',
};

describe('launchStateMachineInput', () => {
  it('carries no meeting password, even when one is given', () => {
    const input = launchStateMachineInput(FORM_LAUNCH);
    expect(input.data).not.toHaveProperty('meetingPassword');
    expect(JSON.stringify(input)).not.toContain('pw-from-the-form');
    expect(LAUNCH_FIELDS).not.toContain('meetingPassword');
  });

  it('carries every launch field as given', () => {
    const { meetingPassword, ...launch } = FORM_LAUNCH;
    expect(launchStateMachineInput(FORM_LAUNCH)).toEqual({ apiInfo: { httpMethod: 'POST' }, data: launch });
  });

  it('leaves out a field that is not given, so the state machine default applies', () => {
    const { data } = launchStateMachineInput({ virtualParticipantId: 'vp-1', meetingID: '1' });
    expect(data).toEqual({ virtualParticipantId: 'vp-1', meetingID: '1' });
  });
});

// Both callers of StartSyncExecution build its input with launchStateMachineInput.
describe.each([
  ['VirtualParticipantList.jsx', resolve(__dirname, 'VirtualParticipantList.jsx')],
  ['EmbedVpLoader.jsx', resolve(__dirname, '../embed/EmbedVpLoader.jsx')],
])('%s', (_name, path) => {
  const source = readFileSync(path, 'utf8');
  const start = source.indexOf('const sfnParams = {');
  const block = source.slice(start, source.indexOf('};', start));

  it('builds the StartSyncExecution input without the meeting password', () => {
    expect(start).toBeGreaterThan(-1);
    expect(block).toContain('launchStateMachineInput(');
    expect(block).not.toContain('meetingPassword');
  });
});
