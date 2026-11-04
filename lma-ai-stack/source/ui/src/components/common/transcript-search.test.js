/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import ReactMarkdown from 'react-markdown';

import markdownRehypePlugins from './markdown-plugins';
import { isSpeechChannel } from './channels';
import {
  matchRanges,
  passesSpeakerFilter,
  passesTranscriptFilter,
  rehypeMarkMatches,
  speakerFilterOptions,
  splitByRanges,
  stepMatch,
  textMatches,
} from './transcript-search';

const isSpeech = (s) => s.channel !== 'MEETING_ASSISTANT';

describe('textMatches', () => {
  it('needs every word, in any order, ignoring case and accents', () => {
    expect(textMatches('The Résumé is due Friday', 'resume friday')).toBe(true);
    expect(textMatches('The Résumé is due Friday', 'FRIDAY résumé')).toBe(true);
    expect(textMatches('The Résumé is due Friday', 'resume monday')).toBe(false);
  });

  it('matches everything for an empty search', () => {
    expect(textMatches('anything', '   ')).toBe(true);
  });
});

describe('matchRanges', () => {
  it('finds each word in the original text, accents and case included', () => {
    const text = 'Budget: the RÉSUMÉ and the budget';
    expect(matchRanges(text, 'budget resume').map(([a, b]) => text.slice(a, b))).toEqual([
      'Budget',
      'RÉSUMÉ',
      'budget',
    ]);
  });

  it('merges overlapping matches', () => {
    expect(matchRanges('abcd', 'abc bcd')).toEqual([[0, 4]]);
  });

  it('finds nothing for an empty search or text', () => {
    expect(matchRanges('abc', '')).toEqual([]);
    expect(matchRanges('', 'a')).toEqual([]);
  });
});

describe('rehypeMarkMatches', () => {
  it('marks the words in rendered text, inside formatting too', () => {
    const { container } = render(
      <ReactMarkdown rehypePlugins={[...markdownRehypePlugins, rehypeMarkMatches('budget plan')]}>
        The **budget** and the plan.
      </ReactMarkdown>,
    );
    expect([...container.querySelectorAll('mark')].map((m) => m.textContent)).toEqual(['budget', 'plan']);
    expect(container.textContent).toBe('The budget and the plan.');
  });

  it('adds nothing for an empty search', () => {
    const { container } = render(
      <ReactMarkdown rehypePlugins={[...markdownRehypePlugins, rehypeMarkMatches('')]}>budget</ReactMarkdown>,
    );
    expect(container.querySelector('mark')).toBeNull();
  });
});

describe('the speaker filter', () => {
  const segments = [
    { channel: 'REMOTE', speaker: 'Sarah Lee', startTime: 10 },
    { channel: 'MEETING_ASSISTANT', speaker: 'MEETING_ASSISTANT', startTime: 12 },
    { channel: 'LOCAL', speaker: 'Bob Chen ', startTime: 3 },
    { channel: 'REMOTE', speaker: 'Sarah Lee', startTime: 20 },
  ];

  it('offers each speaker once, in the order they first spoke, without the assistant', () => {
    expect(speakerFilterOptions(segments, isSpeech)).toEqual(['Bob Chen', 'Sarah Lee']);
  });

  it('shows every line with no speaker chosen', () => {
    expect(segments.every((s) => passesSpeakerFilter(s, [], isSpeech))).toBe(true);
  });

  it("shows only the chosen speakers' lines", () => {
    expect(segments.filter((s) => passesSpeakerFilter(s, ['Bob Chen'], isSpeech))).toEqual([segments[2]]);
  });
});

describe('the chat messages under the filter', () => {
  const speech = (s) => isSpeechChannel(s.channel);
  const segments = [
    { channel: 'REMOTE', speaker: 'Sarah Lee', startTime: 1 },
    { channel: 'MEETING_CHAT', speaker: 'Alice', startTime: 2 },
    { channel: 'LOCAL', speaker: 'Bob Chen', startTime: 3 },
    { channel: 'MEETING_CHAT', speaker: 'Sarah Lee', startTime: 4 },
    { channel: 'MEETING_ASSISTANT', speaker: 'MEETING_ASSISTANT', startTime: 5 },
  ];
  const shown = (filter) => segments.filter((s) => passesTranscriptFilter(s, filter, speech));

  it('offers no chat sender as a speaker, even one who also spoke', () => {
    expect(speakerFilterOptions(segments, speech)).toEqual(['Sarah Lee', 'Bob Chen']);
  });

  it('shows everything by default', () => {
    expect(shown({})).toEqual(segments);
    expect(shown(undefined)).toEqual(segments);
  });

  it('hides the chat messages and nothing else when chat is off', () => {
    expect(shown({ showChat: false })).toEqual([segments[0], segments[2], segments[4]]);
  });

  it("keeps the chat beside the chosen speakers' lines, and only it when chat is on", () => {
    expect(shown({ speakers: ['Sarah Lee'], showChat: true })).toEqual([segments[0], segments[1], segments[3]]);
    expect(shown({ speakers: ['Sarah Lee'], showChat: false })).toEqual([segments[0]]);
  });

  it('finds the search in a chat message like in a spoken line', () => {
    expect(textMatches('Deck: https://example.com/Résumé', 'resume deck')).toBe(true);
  });
});

describe('splitByRanges', () => {
  it('cuts plain text at the matches', () => {
    const text = 'the Budget and the budget';
    expect(splitByRanges(text, matchRanges(text, 'budget'))).toEqual([
      { text: 'the ', match: false },
      { text: 'Budget', match: true },
      { text: ' and the ', match: false },
      { text: 'budget', match: true },
    ]);
  });

  it('cuts a piece of a longer text by where the piece starts in it', () => {
    const whole = 'see https://example.com/budget now';
    const ranges = matchRanges(whole, 'budget see');
    expect(splitByRanges('https://example.com/budget', ranges, 4)).toEqual([
      { text: 'https://example.com/', match: false },
      { text: 'budget', match: true },
    ]);
    expect(splitByRanges(' now', ranges, 30)).toEqual([{ text: ' now', match: false }]);
  });

  it('marks the part of a match that falls in the piece', () => {
    expect(splitByRanges('abcdef', [[4, 8]], 6)).toEqual([
      { text: 'ab', match: true },
      { text: 'cdef', match: false },
    ]);
  });

  it('is the text unmarked with no matches, and nothing for no text', () => {
    expect(splitByRanges('+1', [])).toEqual([{ text: '+1', match: false }]);
    expect(splitByRanges('', [[0, 2]])).toEqual([]);
  });
});

describe('stepMatch', () => {
  it('steps forward and back, wrapping', () => {
    expect(stepMatch(3, 0, 1)).toBe(1);
    expect(stepMatch(3, 2, 1)).toBe(0);
    expect(stepMatch(3, 0, -1)).toBe(2);
  });

  it('starts at the first or the last match from none', () => {
    expect(stepMatch(3, -1, 1)).toBe(0);
    expect(stepMatch(3, -1, -1)).toBe(2);
    expect(stepMatch(0, -1, 1)).toBe(-1);
  });
});
