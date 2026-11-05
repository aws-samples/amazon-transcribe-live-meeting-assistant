/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Markdown downloads: the summary and the transcript as Markdown documents
 * (also what "Copy to clipboard" copies), and the Markdown read back as the
 * blocks of the summary's Word document.
 */
import { describe, it, expect } from 'vitest';

import {
  consentAsMarkdown,
  markdownToDocBlocks,
  summaryDownloadMarkdown,
  transcriptDownloadMarkdown,
} from './download-func';

const CALL_ID = 'Weekly sync-2026-11-03-14-05-09-123';
const MEETING = {
  callId: CALL_ID,
  initiationTimeStamp: '2026-11-03T14:05:09.123Z',
  conversationDurationTimeStamp: '00:17:16',
  callSummaryText: JSON.stringify({
    SUMMARY: 'Sarah walked through the plan.',
    ACTIONS: '- **Bob**: send the budget\n- Sarah: book the room',
    'SCREEN SHARE SUMMARY': '**[00:30] Budget**\n\n![Slide at 00:30](keyframe:0000030000-ab12cd)\n\nQ3 numbers.',
  }),
};

const CONSENT = {
  Mode: 'opt_in',
  Rule: 'each_participant',
  Status: 'granted',
  CaptureAllowed: true,
  Participants: ['Ana Silva'],
  Events: [{ ParticipantName: 'Ana Silva', Decision: 'consent', Source: 'chat', OccurredAt: '2026-11-03T14:06:00Z' }],
};

describe('summaryDownloadMarkdown', () => {
  it('titles the document by the meeting and lists its facts', () => {
    const md = summaryDownloadMarkdown(MEETING);
    expect(md.startsWith('# Weekly sync\n\n')).toBe(true);
    expect(md).toContain(`- **Meeting ID:** ${CALL_ID}`);
    expect(md).toContain('- **Date:** 2026-11-03T14:05:09.123Z');
    expect(md).toContain('- **Duration:** 00:17:16');
  });

  it('writes each section under a heading of its own, in order, with its Markdown kept', () => {
    const md = summaryDownloadMarkdown(MEETING);
    const headings = md.split('\n').filter((line) => line.startsWith('## '));
    expect(headings).toEqual(['## SUMMARY', '## ACTIONS', '## SCREEN SHARE SUMMARY']);
    expect(md).toContain('## ACTIONS\n\n- **Bob**: send the budget\n- Sarah: book the room');
  });

  it('writes a key frame as its alt text, since the picture cannot travel with the file', () => {
    const md = summaryDownloadMarkdown(MEETING);
    expect(md).not.toContain('keyframe:');
    expect(md).toContain('(Slide at 00:30)');
  });

  it('keeps a hand-written summary, split at its headings', () => {
    const md = summaryDownloadMarkdown({
      ...MEETING,
      callSummaryText: '**SUMMARY**\n\nWritten by hand.\n\n**NOTES**\n\nMore.',
    });
    expect(md).toContain('## SUMMARY\n\nWritten by hand.');
    expect(md).toContain('## NOTES\n\nMore.');
  });

  it('says so for a meeting with no summary', () => {
    expect(summaryDownloadMarkdown({ callId: 'm' })).toContain('## Summary\n\nNot available');
  });

  it('ends with the consent record when the meeting has one', () => {
    const md = summaryDownloadMarkdown(MEETING, CONSENT);
    expect(md).toMatch(/## Consent\n\n- \*\*Mode:\*\* Opt-in/);
    expect(md.indexOf('## Consent')).toBeGreaterThan(md.indexOf('## SCREEN SHARE SUMMARY'));
    expect(summaryDownloadMarkdown(MEETING)).not.toContain('## Consent');
  });

  it('escapes Markdown in the meeting name', () => {
    expect(summaryDownloadMarkdown({ callId: 'Q*4* [plan]' })).toMatch(/^# Q\\\*4\\\* \\\[plan\\\]\n/);
  });
});

describe('consentAsMarkdown', () => {
  it('writes the facts as a list and the participants under their own label', () => {
    const md = consentAsMarkdown(CONSENT);
    expect(md).toContain('- **Status:** Granted');
    expect(md).toContain('**Participants**\n\n- Ana Silva: consent');
    expect(md).toMatch(/\n\nThis record is supporting evidence/);
    expect(consentAsMarkdown(null)).toBe('');
  });
});

describe('transcriptDownloadMarkdown', () => {
  const segment = (id, channel, speaker, transcript, startTime) => ({
    segmentId: id,
    channel,
    speaker,
    transcript,
    startTime,
    endTime: startTime + 2,
    isPartial: false,
  });
  const TRANSCRIPT = {
    [CALL_ID]: {
      REMOTE: { segments: [segment('s1', 'REMOTE', 'Sarah Lee', 'Welcome.', 1)] },
      LOCAL: { segments: [segment('s2', 'LOCAL', 'Bob *the* Builder', 'Thanks.', 20)] },
      MEETING_CHAT: { segments: [segment('c1', 'MEETING_CHAT', 'Alice', 'See\nthe doc', 10)] },
    },
  };

  it('writes a paragraph per line, in time order, with the speaker in bold and the start time', () => {
    const md = transcriptDownloadMarkdown(TRANSCRIPT, MEETING);
    expect(md.startsWith('# Transcript: Weekly sync\n\n')).toBe(true);
    const lines = md.split('\n\n').slice(2);
    expect(lines).toEqual([
      '**Sarah Lee** [00:01.0]: Welcome.',
      '**Alice (chat)** [00:10.0]: See / the doc',
      '**Bob \\*the\\* Builder** [00:20.0]: Thanks.\n',
    ]);
  });

  it('uses the names given to the voices, as the screen does', () => {
    const named = {
      ...MEETING,
      speakerIdentities: JSON.stringify({ identities: { 'REMOTE#Sarah Lee': { name: 'Sarah L.' } } }),
    };
    expect(transcriptDownloadMarkdown(TRANSCRIPT, named)).toContain('**Sarah L.** [00:01.0]: Welcome.');
  });
});

describe('markdownToDocBlocks', () => {
  it('reads headings, bullets with their level, and paragraphs', () => {
    const blocks = markdownToDocBlocks('# Title\n\n## SUMMARY\n\nText here.\n\n- one\n  - two\n#### Deep');
    expect(blocks.map((b) => [b.kind, b.level])).toEqual([
      ['heading', 1],
      ['heading', 2],
      ['paragraph', undefined],
      ['bullet', 0],
      ['bullet', 1],
      ['heading', 3],
    ]);
  });

  it('makes **bold** bold and drops the other markers', () => {
    const [block] = markdownToDocBlocks('- **Bob**: send the _budget_ and `notes`, see [the doc](https://x.example/d)');
    expect(block.runs).toEqual([
      { text: 'Bob', bold: true },
      { text: ': send the budget and notes, see the doc (https://x.example/d)', bold: false },
    ]);
  });

  it('reads escaped characters as themselves', () => {
    const [block] = markdownToDocBlocks('# Q\\*4\\* \\[plan\\]');
    expect(block.runs).toEqual([{ text: 'Q*4* [plan]', bold: false }]);
  });
});
