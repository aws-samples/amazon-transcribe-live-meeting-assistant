/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The transcript downloads (text, Excel and Word): the lines they hold, a
 * message typed in the meeting chat among them, and an Excel file that holds
 * every value as typed.
 */
import { describe, it, expect } from 'vitest';
import * as XLSX from 'xlsx';
import { buildWorkbook, parseTranscriptLine, transcriptDownloadRows, transcriptDownloadText } from './download-func';

const CALL_ID = 'Weekly sync-2026-11-03-14-05-09-123';
const MEETING = { callId: CALL_ID };
const segment = (id, channel, speaker, transcript, startTime, endTime = startTime) => ({
  segmentId: id,
  channel,
  speaker,
  transcript,
  startTime,
  endTime,
  isPartial: false,
});

const HYPERLINK = '=HYPERLINK("https://example.com/x","click")';

const TRANSCRIPT = {
  [CALL_ID]: {
    CHAT_ASSISTANT: { segments: [] },
    AGENT_ASSISTANT: { segments: [] },
    MEETING_ASSISTANT: { segments: [] },
    REMOTE: { segments: [segment('s1', 'REMOTE', 'Sarah Lee', 'Welcome.', 1, 8)] },
    LOCAL: { segments: [segment('s2', 'LOCAL', 'Bob Chen', 'Thanks.', 20, 24)] },
    MEETING_CHAT: {
      segments: [
        segment('c1', 'MEETING_CHAT', 'Alice', 'Agenda:\n- budget\r\n- hiring\n\n', 10),
        segment('c2', 'MEETING_CHAT', 'Mallory', HYPERLINK, 12),
        segment('c3', 'MEETING_CHAT', '', '+1', 13),
        segment('c4', 'MEETING_CHAT', 'Eve', '@SUM(A1:A2)', 14),
        segment('c5', 'MEETING_CHAT', '=cmd', '-2+3', 15),
      ],
    },
  },
};

describe('transcriptDownloadRows', () => {
  it('holds every channel the meeting has, in time order, chat lines under "<sender> (chat)"', () => {
    expect(transcriptDownloadRows(TRANSCRIPT, MEETING).map((r) => [r.speaker, r.transcript])).toEqual([
      ['Sarah Lee', 'Welcome.'],
      ['Alice (chat)', 'Agenda: / - budget / - hiring'],
      ['Mallory (chat)', HYPERLINK],
      ['Unknown sender (chat)', '+1'],
      ['Eve (chat)', '@SUM(A1:A2)'],
      ['=cmd (chat)', '-2+3'],
      ['Bob Chen', 'Thanks.'],
    ]);
  });

  it('gives a chat line its time', () => {
    const alice = transcriptDownloadRows(TRANSCRIPT, MEETING).find((r) => r.speaker === 'Alice (chat)');
    expect(alice.startTimestamp).toBe('00:10.0');
  });
});

describe('the text and Word downloads', () => {
  it('write a chat message on one line, under "<sender> (chat)"', () => {
    const lines = transcriptDownloadText(TRANSCRIPT, MEETING).split('\n');
    expect(lines).toHaveLength(7);
    expect(lines[1]).toBe('Alice (chat) [00:10.0]: Agenda: / - budget / - hiring');
    expect(lines[3]).toBe('Unknown sender (chat) [00:13.0]: +1');
  });

  it('read each chat line back as speaker, time and text for the Word paragraph', () => {
    const lines = transcriptDownloadText(TRANSCRIPT, MEETING).split('\n');
    expect(parseTranscriptLine(lines[1])).toEqual({
      speaker: 'Alice (chat)',
      timestamp: '00:10.0',
      content: 'Agenda: / - budget / - hiring',
    });
    expect(parseTranscriptLine(lines[2]).content).toBe(HYPERLINK);
  });
});

describe('the Excel download', () => {
  // Written to the bytes of an .xlsx file and read back, as a spreadsheet would open it.
  const roundTrip = (data) => {
    const bytes = XLSX.write(buildWorkbook(data), { type: 'array', bookType: 'xlsx' });
    const book = XLSX.read(bytes, { type: 'array', cellFormula: true, bookFiles: true });
    return { sheet: book.Sheets[book.SheetNames[0]], files: book.files };
  };
  const sheetXml = (files) =>
    Object.keys(files)
      .filter((name) => /^xl\/worksheets\/.*\.xml$/.test(name))
      .map((name) => new TextDecoder().decode(Uint8Array.from(files[name].content)))
      .join('');
  const cells = (sheet) =>
    Object.keys(sheet)
      .filter((ref) => !ref.startsWith('!'))
      .map((ref) => sheet[ref]);

  it('holds a chat message starting "=", "+", "-" or "@" as text, never as a formula', () => {
    const { sheet, files } = roundTrip(transcriptDownloadRows(TRANSCRIPT, MEETING));
    const all = cells(sheet);
    expect(all.filter((cell) => cell.f !== undefined)).toEqual([]);
    const values = all.map((cell) => cell.v);
    [HYPERLINK, '+1', '@SUM(A1:A2)', '-2+3', '=cmd (chat)'].forEach((text) => {
      const cell = all.find((c) => c.v === text);
      expect(cell, text).toBeTruthy();
      expect(cell.t).toBe('s');
    });
    expect(values).toContain('Alice (chat)');
    // No formula element anywhere in the sheet's XML.
    const xml = sheetXml(files);
    expect(xml).toContain('<sheetData>');
    expect(xml).not.toMatch(/<f[\s>]/);
  });

  it('would show a formula, were one written (the check above can fail)', () => {
    const wb = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet([[{ t: 'n', f: 'SUM(1,2)', v: 3 }]]));
    const book = XLSX.read(XLSX.write(wb, { type: 'array', bookType: 'xlsx' }), {
      type: 'array',
      cellFormula: true,
      bookFiles: true,
    });
    expect(sheetXml(book.files)).toMatch(/<f[\s>]/);
    expect(book.Sheets[book.SheetNames[0]].A1.f).toBe('SUM(1,2)');
  });

  it('keeps numbers as numbers', () => {
    const { sheet } = roundTrip([{ name: 'a', count: 3 }]);
    expect(cells(sheet).find((c) => c.v === 3).t).toBe('n');
  });
});
