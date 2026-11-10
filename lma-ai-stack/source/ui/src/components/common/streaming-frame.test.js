/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';
import { toStreamingFrame } from './streaming-frame';

describe('toStreamingFrame', () => {
  it('sends the speaker names under the names the streaming API documents', () => {
    const frame = toStreamingFrame({
      callId: 'Weekly sync - 2026-11-04',
      agentId: 'ana@example.com',
      fromNumber: 'Other Participant',
      toNumber: 'System',
      samplingRate: 48000,
      callEvent: 'START',
    });
    expect(frame).toEqual({
      callId: 'Weekly sync - 2026-11-04',
      clientType: 'STREAM_AUDIO',
      localSpeakerName: 'ana@example.com',
      remoteSpeakerName: 'Other Participant',
      samplingRate: 48000,
      callEvent: 'START',
    });
  });

  it('keeps every other field, and adds no name the form did not hold', () => {
    const frame = toStreamingFrame({ callId: 'c1', callEvent: 'END', diarizeMicChannel: true });
    expect(frame).toEqual({ callId: 'c1', callEvent: 'END', diarizeMicChannel: true });
  });

  it('names the client on a START frame only', () => {
    expect(toStreamingFrame({ callId: 'c1', callEvent: 'START' }).clientType).toBe('STREAM_AUDIO');
    expect(toStreamingFrame({ callId: 'c1', callEvent: 'END' })).not.toHaveProperty('clientType');
  });
});
