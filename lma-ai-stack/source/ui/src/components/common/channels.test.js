/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';
import {
  canonicalChannel,
  canonicalKeyed,
  canonicalSentiment,
  canonicalSpeakerKey,
  channelLabel,
  isChatChannel,
  isSpeechChannel,
  LOCAL_CHANNEL,
  MEETING_CHAT_CHANNEL,
  REMOTE_CHANNEL,
  SPEECH_CHANNELS,
} from './channels';

describe('canonicalChannel', () => {
  it('reads the older channel names as REMOTE and LOCAL', () => {
    expect(canonicalChannel('CALLER')).toBe(REMOTE_CHANNEL);
    expect(canonicalChannel('CUSTOMER')).toBe(REMOTE_CHANNEL);
    expect(canonicalChannel('AGENT')).toBe(LOCAL_CHANNEL);
  });

  it('leaves the current names and every other channel unchanged', () => {
    ['REMOTE', 'LOCAL', 'AGENT_ASSISTANT', 'CHAT_ASSISTANT', 'MEETING_ASSISTANT', '', undefined, null].forEach((c) =>
      expect(canonicalChannel(c)).toBe(c),
    );
    // Not an own property of the alias map, so not an alias.
    expect(canonicalChannel('toString')).toBe('toString');
  });
});

describe('isSpeechChannel', () => {
  it('is true for the two speech channels under either name', () => {
    expect(SPEECH_CHANNELS).toEqual(['REMOTE', 'LOCAL']);
    ['REMOTE', 'LOCAL', 'CALLER', 'AGENT'].forEach((c) => expect(isSpeechChannel(c)).toBe(true));
  });

  it('is false for the assistants, the meeting chat and anything else', () => {
    ['AGENT_ASSISTANT', 'CHAT_ASSISTANT', 'CATEGORY_MATCH', 'MEETING_CHAT', '', undefined].forEach((c) =>
      expect(isSpeechChannel(c)).toBe(false),
    );
  });
});

describe('isChatChannel', () => {
  it('is true for the meeting chat only', () => {
    expect(MEETING_CHAT_CHANNEL).toBe('MEETING_CHAT');
    expect(isChatChannel('MEETING_CHAT')).toBe(true);
    ['REMOTE', 'LOCAL', 'CALLER', 'AGENT', 'CHAT_ASSISTANT', '', undefined].forEach((c) =>
      expect(isChatChannel(c)).toBe(false),
    );
  });
});

describe('channelLabel', () => {
  it('names the speech channels in meeting terms, under either name', () => {
    expect(channelLabel('REMOTE')).toBe('Remote');
    expect(channelLabel('CALLER')).toBe('Remote');
    expect(channelLabel('LOCAL')).toBe('Local');
    expect(channelLabel('AGENT')).toBe('Local');
  });

  it('shows any other channel by name', () => {
    expect(channelLabel('CHAT_ASSISTANT')).toBe('CHAT_ASSISTANT');
  });
});

describe('canonicalSpeakerKey and canonicalKeyed', () => {
  it('re-keys only the channel part of a speaker key', () => {
    expect(canonicalSpeakerKey('CALLER#Other Participant (spk_0)')).toBe('REMOTE#Other Participant (spk_0)');
    expect(canonicalSpeakerKey('AGENT#Me#1')).toBe('LOCAL#Me#1');
    expect(canonicalSpeakerKey('REMOTE#CALLER')).toBe('REMOTE#CALLER');
    expect(canonicalSpeakerKey('no channel')).toBe('no channel');
    expect(canonicalSpeakerKey(undefined)).toBe(undefined);
  });

  it('keeps the entry under the current name when both spellings are stored', () => {
    expect(canonicalKeyed({ 'CALLER#a': { name: 'old' }, 'REMOTE#a': { name: 'new' }, 'AGENT#b': 1 })).toEqual({
      'REMOTE#a': { name: 'new' },
      'LOCAL#b': 1,
    });
  });

  it('re-keys both sides of a merge', () => {
    expect(canonicalKeyed({ 'CALLER#x': 'CALLER#y' }, true)).toEqual({ 'REMOTE#x': 'REMOTE#y' });
  });
});

describe('canonicalSentiment', () => {
  const quarter = (score) => [{ BeginOffsetMillis: 0, EndOffsetMillis: 10, Score: score }];

  it('keys a meeting stored with CALLER and AGENT sentiment by REMOTE and LOCAL', () => {
    const stored = {
      OverallSentiment: { REMOTE: null, LOCAL: null, CALLER: 1.5, AGENT: -2 },
      SentimentByPeriod: { QUARTER: { REMOTE: null, LOCAL: null, CALLER: quarter(1), AGENT: quarter(2) } },
    };
    expect(canonicalSentiment(stored)).toEqual({
      OverallSentiment: { REMOTE: 1.5, LOCAL: -2 },
      SentimentByPeriod: { QUARTER: { REMOTE: quarter(1), LOCAL: quarter(2) } },
    });
  });

  it('prefers the current keys, which is what a meeting recorded today has', () => {
    const stored = {
      OverallSentiment: { REMOTE: 3, LOCAL: 4, CALLER: null, AGENT: null },
      SentimentByPeriod: { QUARTER: { REMOTE: quarter(3), CALLER: quarter(9) } },
    };
    expect(canonicalSentiment(stored)).toEqual({
      OverallSentiment: { REMOTE: 3, LOCAL: 4 },
      SentimentByPeriod: { QUARTER: { REMOTE: quarter(3) } },
    });
  });

  it('tolerates a meeting with no sentiment', () => {
    expect(canonicalSentiment(null)).toBe(null);
    expect(canonicalSentiment({})).toEqual({});
  });
});
