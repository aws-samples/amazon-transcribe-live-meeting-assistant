/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/* eslint-disable max-classes-per-file -- stand-ins for the docx and jsPDF classes */

/**
 * The Word and PDF downloads that hold pictures and pages: the summary's blocks
 * with its key frames embedded (or their text when a frame cannot be fetched),
 * the transcript's blocks, and the PDF built from them, as text.
 */
import { afterEach, describe, it, expect, vi } from 'vitest';
import { jsPDF } from 'jspdf';

import {
  docxChildren,
  downloadSummaryAsPdf,
  downloadTranscriptAsPdf,
  markdownToDocBlocks,
  summaryDocBlocks,
  transcriptDocBlocks,
} from './download-func';
import { buildPdf, pdfSafeText } from './pdf-export';

const CALL_ID = 'Weekly sync-2026-11-03-14-05-09-123';
const FRAME_A = '0000030000-ab12cd';
const FRAME_B = '0000090000-ef34gh';
const MEETING = {
  callId: CALL_ID,
  initiationTimeStamp: '2026-11-03T14:05:09.123Z',
  conversationDurationTimeStamp: '00:17:16',
  callSummaryText: JSON.stringify({
    SUMMARY: 'Sarah walked through the plan.',
    'SCREEN SHARE SUMMARY': [
      '**[00:30] Budget**',
      '',
      `![Slide at 00:30](keyframe:${FRAME_A})`,
      '',
      'Q3 numbers.',
      '',
      '**[01:30] Hiring**',
      '',
      `![Slide at 01:30](keyframe:${FRAME_B})`,
    ].join('\n'),
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
const THUMB = { data: new Uint8Array([0xff, 0xd8, 0xff]), width: 480, height: 270 };

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
    LOCAL: { segments: [segment('s2', 'LOCAL', 'Bob Chen', 'Thanks (all).', 20)] },
  },
};

// The text a PDF draws, from its uncompressed page content.
const pdfStrings = (doc) => [...doc.output().matchAll(/\(((?:\\.|[^\\)])*)\) Tj/g)].map((m) => m[1]);

// Catches the file saveBlob hands the browser.
const captureSavedFile = () => {
  const saved = {};
  const createObjectURL = vi.fn((blob) => {
    saved.blob = blob;
    return 'blob:test';
  });
  vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() });
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click() {
    saved.name = this.download;
  });
  return saved;
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('markdownToDocBlocks with key frames', () => {
  it('reads a line that is only a key frame as a key frame block, and one within text as its alt text', () => {
    const blocks = markdownToDocBlocks(
      `![Slide at 00:30](keyframe:${FRAME_A})\nSee ![Slide](keyframe:${FRAME_B}) here`,
    );
    expect(blocks[0]).toEqual({ kind: 'keyframe', id: FRAME_A, alt: 'Slide at 00:30' });
    expect(blocks[1]).toEqual({ kind: 'paragraph', runs: [{ text: 'See (Slide) here', bold: false }] });
  });
});

describe('summaryDocBlocks', () => {
  it('embeds each key frame the summary names, in place, from one read of all of them', async () => {
    const loadThumbnails = vi.fn(async () => ({ [FRAME_A]: THUMB, [FRAME_B]: THUMB }));
    const blocks = await summaryDocBlocks(MEETING, null, { loadThumbnails });
    expect(loadThumbnails).toHaveBeenCalledTimes(1);
    expect(loadThumbnails).toHaveBeenCalledWith(CALL_ID, [FRAME_A, FRAME_B]);
    const images = blocks.filter((block) => block.kind === 'image');
    expect(images.map((block) => block.id)).toEqual([FRAME_A, FRAME_B]);
    // The picture follows its title and time, as on the Summary tab.
    const budget = blocks.findIndex((block) => block.runs?.[0]?.text === '[00:30] Budget');
    expect(blocks[budget + 1]).toMatchObject({ kind: 'image', id: FRAME_A, alt: 'Slide at 00:30', image: THUMB });
  });

  it("writes a frame that could not be fetched as today's text description", async () => {
    const blocks = await summaryDocBlocks(MEETING, CONSENT, { loadThumbnails: async () => ({ [FRAME_A]: THUMB }) });
    expect(blocks.filter((block) => block.kind === 'image').map((block) => block.id)).toEqual([FRAME_A]);
    expect(blocks).toContainEqual({ kind: 'paragraph', runs: [{ text: '(Slide at 01:30)', bold: false }] });
    expect(blocks.some((block) => block.kind === 'keyframe')).toBe(false);
    // The consent record is still at the end.
    expect(blocks.some((block) => block.kind === 'heading' && block.runs[0].text === 'Consent')).toBe(true);
  });

  it('reads no frame for a summary without one', async () => {
    const loadThumbnails = vi.fn();
    const blocks = await summaryDocBlocks({ ...MEETING, callSummaryText: 'Just text.' }, null, { loadThumbnails });
    expect(loadThumbnails).not.toHaveBeenCalled();
    expect(blocks.at(-1)).toEqual({ kind: 'paragraph', runs: [{ text: 'Just text.', bold: false }] });
  });
});

describe('docxChildren', () => {
  // A stand-in for the docx library, recording what each element was built with.
  const fakeDocx = () => {
    const made = (kind) =>
      class {
        constructor(options) {
          this.kind = kind;
          this.options = options;
        }
      };
    return {
      HeadingLevel: { HEADING_1: 'h1', HEADING_2: 'h2', HEADING_3: 'h3' },
      Paragraph: made('paragraph'),
      TextRun: made('text'),
      ImageRun: made('image'),
    };
  };

  it('writes an image block as a JPEG picture of its size, with its alt text', () => {
    const [paragraph] = docxChildren(fakeDocx(), [{ kind: 'image', id: FRAME_A, alt: 'Slide at 00:30', image: THUMB }]);
    const [image] = paragraph.options.children;
    expect(image.kind).toBe('image');
    expect(image.options).toMatchObject({
      type: 'jpg',
      data: THUMB.data,
      transformation: { width: 480, height: 270 },
      altText: { description: 'Slide at 00:30' },
    });
  });

  it('writes a run in its colour, and a key frame block left unresolved as its text', () => {
    const [segmentLine, frame] = docxChildren(fakeDocx(), [
      { kind: 'paragraph', runs: [{ text: ' [00:01.0]: ', color: '666666' }] },
      { kind: 'keyframe', id: FRAME_A, alt: 'Slide at 00:30' },
    ]);
    expect(segmentLine.options.children[0].options.color).toBe('666666');
    expect(frame.options.children[0].options.text).toBe('(Slide at 00:30)');
  });
});

describe('transcriptDocBlocks', () => {
  it("has the Word transcript's title and segments, the meeting's facts, and the consent record at the end", () => {
    const blocks = transcriptDocBlocks(TRANSCRIPT, MEETING, CONSENT);
    expect(blocks[0]).toEqual({
      kind: 'heading',
      level: 1,
      runs: [{ text: 'Meeting Transcript: Weekly sync', bold: false }],
    });
    expect(blocks).toContainEqual({
      kind: 'bullet',
      level: 0,
      runs: [
        { text: 'Duration:', bold: true },
        { text: ' 00:17:16', bold: false },
      ],
    });
    const segments = blocks.filter((block) => block.runs?.[1]?.color === '666666');
    expect(segments.map((block) => block.runs.map((run) => run.text).join(''))).toEqual([
      'Sarah Lee [00:01.0]: Welcome.',
      'Bob Chen [00:20.0]: Thanks (all).',
    ]);
    expect(segments[0].runs[0].bold).toBe(true);
    const consent = blocks.findIndex((block) => block.kind === 'heading' && block.runs[0].text === 'Consent');
    expect(consent).toBeGreaterThan(blocks.indexOf(segments[1]));
  });
});

describe('pdfSafeText', () => {
  it('keeps Western European text and typographic punctuation as written', () => {
    expect(pdfSafeText('Café “déjà vu” — 5 €')).toBe('Café “déjà vu” — 5 €');
  });

  it('writes other characters as their unaccented form, or "?" when they have none', () => {
    expect(pdfSafeText('Łódź Dvořák')).toBe('?ódz Dvorák');
    expect(pdfSafeText('会議')).toBe('??');
    expect(pdfSafeText(`a${String.fromCharCode(0x200b)}b\tc`)).toBe('ab c');
  });
});

describe('buildPdf', () => {
  it('writes the blocks as text that can be selected, wrapping long lines onto more lines and pages', async () => {
    const long = 'word '.repeat(400).trim();
    const blocks = [
      { kind: 'heading', level: 1, runs: [{ text: 'Weekly sync', bold: false }] },
      {
        kind: 'bullet',
        level: 0,
        runs: [
          { text: 'Duration:', bold: true },
          { text: ' 00:17:16', bold: false },
        ],
      },
      ...Array.from({ length: 80 }, () => ({ kind: 'paragraph', runs: [{ text: long }] })),
      { kind: 'paragraph', runs: [{ text: 'Last line (end).' }] },
    ];
    const doc = await buildPdf(blocks, { jsPDF, compress: false, title: 'Weekly sync', yieldEvery: 10 });
    const strings = pdfStrings(doc);
    expect(strings).toContain('Weekly sync');
    expect(strings).toContain('Duration:');
    expect(strings).toContain('Last line \\(end\\).');
    expect(doc.getNumberOfPages()).toBeGreaterThan(10);
    expect(strings).toContain(`Page 1 of ${doc.getNumberOfPages()}`);
    // No line is wider than the page's text column.
    const column = doc.internal.pageSize.getWidth() - 2 * 54;
    doc.setFont('helvetica', 'normal');
    doc.setFontSize(10.5);
    strings.filter((s) => s.startsWith('word')).forEach((s) => expect(doc.getTextWidth(s)).toBeLessThanOrEqual(column));
  });

  it('pauses for the browser while it builds a long document', async () => {
    const timeout = vi.spyOn(globalThis, 'setTimeout');
    const blocks = Array.from({ length: 250 }, (_, i) => ({ kind: 'paragraph', runs: [{ text: `Line ${i}` }] }));
    await buildPdf(blocks, { jsPDF, compress: false, yieldEvery: 100 });
    expect(timeout.mock.calls.filter(([, delay]) => delay === 0)).toHaveLength(2);
  });

  it('places a picture at its size in points, no wider than the column', async () => {
    const addImage = vi.spyOn(jsPDF.API, 'addImage').mockImplementation(function addImage() {
      return this;
    });
    await buildPdf([{ kind: 'image', id: FRAME_A, alt: 'Slide', image: { ...THUMB, width: 2000, height: 1000 } }], {
      jsPDF,
      compress: false,
    });
    expect(addImage).toHaveBeenCalledTimes(1);
    const [data, format, x, , w, h, alias] = addImage.mock.calls[0];
    expect(data).toBe(THUMB.data);
    expect(format).toBe('JPEG');
    expect(x).toBe(54);
    expect(w).toBe(612 - 108);
    expect(h).toBeCloseTo(w / 2);
    expect(alias).toBe(`keyframe-${FRAME_A}`);
  });
});

describe('the PDF downloads', () => {
  it('save the transcript as Transcript-<meeting>.pdf, a PDF holding its segments', async () => {
    const saved = captureSavedFile();
    let built;
    const loadPdfLibrary = async () => ({
      jsPDF: class extends jsPDF {
        constructor(options) {
          super({ ...options, compress: false });
          built = this;
        }
      },
    });
    await downloadTranscriptAsPdf(TRANSCRIPT, MEETING, CONSENT, { loadPdfLibrary });
    expect(saved.name).toBe(`Transcript-${CALL_ID}.pdf`);
    expect(saved.blob).toBeInstanceOf(Blob);
    expect(saved.blob.type).toBe('application/pdf');
    const strings = pdfStrings(built);
    expect(strings).toContain('Sarah Lee');
    expect(strings).toContain('Welcome.');
    expect(strings).toContain('Consent');
  });

  it('save the summary as Summary-<meeting>.pdf, with its key frames', async () => {
    const saved = captureSavedFile();
    const addImage = vi.spyOn(jsPDF.API, 'addImage').mockImplementation(function addImage() {
      return this;
    });
    await downloadSummaryAsPdf(MEETING, null, {
      loadThumbnails: async () => ({ [FRAME_A]: THUMB }),
      loadPdfLibrary: async () => ({ jsPDF }),
    });
    expect(saved.name).toBe(`Summary-${CALL_ID}.pdf`);
    expect(saved.blob).toBeInstanceOf(Blob);
    expect(addImage).toHaveBeenCalledTimes(1);
  });

  it('say so, rather than fail silently, when the PDF library cannot be loaded', async () => {
    const alert = vi.fn();
    vi.stubGlobal('alert', alert);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    await downloadTranscriptAsPdf(TRANSCRIPT, MEETING, null, {
      loadPdfLibrary: async () => {
        throw new Error('chunk failed');
      },
    });
    expect(alert).toHaveBeenCalledWith(expect.stringContaining('Could not generate PDF file'));
  });
});
