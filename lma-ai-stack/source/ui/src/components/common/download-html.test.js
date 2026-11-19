/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The "Web page (.html)" downloads: one self-contained file, with the key
 * frames as data: URIs, no script and nothing loaded from elsewhere, the
 * summary's text escaped and its links limited to http(s) and mailto, and a
 * table of contents whose every link reaches a section.
 */
import { afterEach, describe, it, expect, vi } from 'vitest';

import {
  downloadSummaryAsHtml,
  downloadTranscriptAsHtml,
  summaryDownloadHtml,
  transcriptDownloadHtml,
} from './download-func';
import { fetchKeyframeThumbnails, MAX_EMBEDDED_KEYFRAMES } from './keyframe-thumbnails';
import * as htmlExport from './html-export';

const { bytesToBase64, HTML_EXPORT_CSP, safeLinkHref } = htmlExport;

const CALL_ID = 'Weekly sync-2026-11-03-14-05-09-123';
const FRAME_A = '0000030000-ab12cd';
const FRAME_B = '0000090000-ef34gh';
const THUMB = { data: new Uint8Array([0xff, 0xd8, 0xff, 0xe0]), width: 480, height: 270 };
const loadRenderer = async () => htmlExport;

const meetingWith = (sections) => ({
  callId: CALL_ID,
  initiationTimeStamp: '2026-11-03T14:05:09.123Z',
  conversationDurationTimeStamp: '00:17:16',
  callSummaryText: JSON.stringify(sections),
});

const MEETING = meetingWith({
  SUMMARY: 'Sarah walked through the plan. See [the plan](https://docs.example.com/plan) and <b>bold</b>.',
  ACTIONS: '- **Sarah**: size the backfill\n  - by Friday',
  'SCREEN SHARE SUMMARY': [
    '**[00:30] Budget**',
    '',
    `![Slide at 00:30](keyframe:${FRAME_A})`,
    '',
    '**[01:30] Hiring**',
    '',
    `![Slide at 01:30](keyframe:${FRAME_B})`,
  ].join('\n'),
  'CHAT & SHARED LINKS': '- **Docs**: shared.\n\nLinks shared in the chat:\n\n- <https://chat.example.com/thread>',
});

const parse = (html) => new DOMParser().parseFromString(html, 'text/html');
const attributeValues = (page, name) => [...page.querySelectorAll(`[${name}]`)].map((el) => el.getAttribute(name));

const build = (meeting = MEETING, options = {}) =>
  summaryDownloadHtml(meeting, null, { loadThumbnails: async () => ({ [FRAME_A]: THUMB }), loadRenderer, ...options });

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the summary web page', () => {
  it('is one HTML document, with its styles inline and no script, font or stylesheet from anywhere', async () => {
    const html = await build();
    expect(html.startsWith('<!DOCTYPE html>\n<html lang="en">')).toBe(true);
    expect(html.match(/<html\b/g)).toHaveLength(1);
    expect(html).not.toMatch(/<script\b/i);
    expect(html).not.toMatch(/<link\b/i);
    expect(html).not.toMatch(/<iframe\b|<object\b|<embed\b/i);
    expect(html).not.toMatch(/@import|url\(|@font-face/i);
    const page = parse(html);
    expect(page.querySelectorAll('style')).toHaveLength(1);
    expect(page.querySelector('meta[http-equiv="Content-Security-Policy"]').getAttribute('content')).toBe(
      HTML_EXPORT_CSP,
    );
    expect(HTML_EXPORT_CSP).toContain("default-src 'none'");
    expect(page.title).toBe('Weekly sync');
  });

  it('embeds each key frame it could fetch as a data: URI, and writes the others as their caption', async () => {
    const page = parse(await build());
    const images = [...page.querySelectorAll('img')];
    expect(images).toHaveLength(1);
    expect(images[0].getAttribute('src')).toBe(`data:image/jpeg;base64,${bytesToBase64(THUMB.data)}`);
    expect(images[0].getAttribute('alt')).toBe('Slide at 00:30');
    expect(page.body.textContent).toContain('(Slide at 01:30)');
  });

  it('reads the frames through the same fetch as the Word and PDF downloads, at most 40 of them', async () => {
    const ids = Array.from({ length: MAX_EMBEDDED_KEYFRAMES + 5 }, (_v, i) => `00000${String(i).padStart(5, '0')}-f`);
    const meeting = meetingWith({
      'SCREEN SHARE SUMMARY': ids.map((id, i) => `![Slide ${i}](keyframe:${id})`).join('\n\n'),
    });
    const loadFrames = vi.fn(async (_callId, wanted) =>
      Object.fromEntries(wanted.map((id) => [id, { ImageSignedUrl: `https://bucket.example.com/${id}.jpg` }])),
    );
    const loadThumbnails = vi.fn((callId, frameIds) =>
      fetchKeyframeThumbnails(callId, frameIds, {
        loadFrames,
        fetchImage: async () => new Blob(['x']),
        resize: async () => THUMB,
      }),
    );
    const page = parse(await summaryDownloadHtml(meeting, null, { loadThumbnails, loadRenderer }));
    expect(loadThumbnails).toHaveBeenCalledWith(CALL_ID, ids);
    expect(loadFrames.mock.calls[0][1]).toHaveLength(MAX_EMBEDDED_KEYFRAMES);
    expect(page.querySelectorAll('img')).toHaveLength(MAX_EMBEDDED_KEYFRAMES);
    expect(page.body.textContent).toContain(`(Slide ${MAX_EMBEDDED_KEYFRAMES})`);
    // The signed links the frames were fetched through are not in the file.
    expect(page.documentElement.outerHTML).not.toContain('bucket.example.com');
  });

  it('holds no address but its own anchors, the data: pictures, and the links the meeting shared', async () => {
    const page = parse(await build());
    attributeValues(page, 'src').forEach((src) => expect(src).toMatch(/^data:image\/jpeg;base64,/));
    const external = attributeValues(page, 'href').filter((href) => !href.startsWith('#'));
    expect(external).toEqual(['https://docs.example.com/plan', 'https://chat.example.com/thread']);
    [...page.querySelectorAll('a[href^="http"]')].forEach((link) => {
      expect(link.getAttribute('rel')).toBe('noopener noreferrer');
      expect(link.getAttribute('target')).toBe('_blank');
    });
  });

  it('writes HTML and script in the summary as text, never as markup', async () => {
    const meeting = meetingWith({
      SUMMARY: [
        'Before <script>alert(1)</script> after.',
        '',
        '<img src="https://pixel.example.com/p.gif" onerror="alert(2)">',
        '',
        '<details open><summary>raw</summary>hidden</details>',
        '',
        '"quoted" & <style>body{display:none}</style>',
      ].join('\n'),
      '<i>Custom</i> & "quoted"': 'Custom text.',
    });
    const html = await build(meeting);
    expect(html).not.toMatch(/<script\b/i);
    // No element carries an event handler; `onerror=` is only in the text.
    expect([...parse(html).querySelectorAll('*')].flatMap((el) => el.getAttributeNames())).not.toContainEqual(
      expect.stringMatching(/^on/i),
    );
    expect(html).not.toContain('pixel.example.com/p.gif"');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(html).toContain('&lt;img src=');
    const page = parse(html);
    expect(page.querySelectorAll('style')).toHaveLength(1);
    expect(page.querySelectorAll('details')).toHaveLength(2);
    expect(page.querySelectorAll('i')).toHaveLength(0);
    expect(page.body.textContent).toContain('Before <script>alert(1)</script> after.');
    const headings = [...page.querySelectorAll('section h2')].map((h) => h.textContent);
    expect(headings).toContain('<i>Custom</i> & "quoted"');
  });

  it('writes a javascript:, data: or other non-web link as its text alone', async () => {
    const meeting = meetingWith({
      SUMMARY: [
        '[run me](javascript:alert(1))',
        '[JS upper](JaVaScRiPt:alert(1))',
        '[a page](data:text/html;base64,PHNjcmlwdD4=)',
        '[a file](file:///etc/passwd)',
        '[relative](../other)',
        '<javascript:alert(3)>',
        '[mail](mailto:team@example.com)',
        '![a picture](https://images.example.com/logo.png)',
        '![inline frame](data:image/png;base64,AAAA)',
      ].join('\n\n'),
    });
    const html = await build(meeting);
    const page = parse(html);
    const hrefs = attributeValues(page, 'href').filter((href) => !href.startsWith('#'));
    expect(hrefs).toEqual(['mailto:team@example.com']);
    expect(html).not.toMatch(/href="\s*(javascript|data|file|vbscript):/i);
    expect(page.querySelectorAll('img')).toHaveLength(0);
    ['run me', 'JS upper', 'a page', 'a file', 'relative', 'a picture', 'inline frame'].forEach((text) =>
      expect(page.body.textContent).toContain(text),
    );
  });

  it('lists its sections at the top, each entry a link to a section on the page', async () => {
    const consent = {
      Mode: 'opt_in',
      Rule: 'each_participant',
      Status: 'granted',
      CaptureAllowed: true,
      Participants: ['Ana Silva'],
      Events: [],
    };
    const page = parse(await summaryDownloadHtml(MEETING, consent, { loadThumbnails: async () => ({}), loadRenderer }));
    const entries = [...page.querySelectorAll('nav.toc a')];
    expect(entries.map((a) => a.textContent)).toEqual([
      'SUMMARY',
      'ACTIONS',
      'SCREEN SHARE SUMMARY',
      'CHAT & SHARED LINKS',
      'Consent',
    ]);
    const ids = [...page.querySelectorAll('[id]')].map((el) => el.id);
    expect(new Set(ids).size).toBe(ids.length);
    entries.forEach((a) => {
      const target = page.getElementById(a.getAttribute('href').slice(1));
      expect(target).not.toBeNull();
      expect(target.querySelector('summary h2').textContent).toBe(a.textContent);
    });
    // Each section opens and closes, and starts open so it prints.
    page.querySelectorAll('section > details').forEach((details) => expect(details.hasAttribute('open')).toBe(true));
    expect(page.body.textContent).toContain('Ana Silva');
  });

  it("keeps the Summary tab's Markdown: lists, emphasis, code and headings under the section's", async () => {
    const meeting = meetingWith({
      DETAILS: '# Top\n\n## Sub\n\n- one\n  - nested\n\n1. first\n\n*em* and `code`\n\n> quoted',
    });
    const page = parse(await build(meeting));
    const section = page.querySelector('section');
    expect(section.querySelector('h3').textContent).toBe('Top');
    expect(section.querySelector('h4').textContent).toBe('Sub');
    expect(section.querySelectorAll('h1')).toHaveLength(0);
    expect(section.querySelector('ul ul li').textContent).toBe('nested');
    expect(section.querySelector('ol li').textContent).toBe('first');
    expect(section.querySelector('em').textContent).toBe('em');
    expect(section.querySelector('code').textContent).toBe('code');
    expect(section.querySelector('blockquote').textContent.trim()).toBe('quoted');
  });

  it('says the summary is not available when there is none', async () => {
    const page = parse(await build({ callId: CALL_ID, callSummaryText: '' }));
    expect(page.querySelector('section h2').textContent).toBe('Summary');
    expect(page.querySelector('section').textContent).toContain('Not available');
    expect(page.querySelector('nav.toc')).toBeNull();
  });

  it('saves as Summary-<meeting>.html, as an HTML file', async () => {
    const created = [];
    vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => {
      created.push(blob);
      return 'blob:x';
    });
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function record() {
      created.push(this.download);
    });
    await downloadSummaryAsHtml(MEETING, null, { loadThumbnails: async () => ({}), loadRenderer });
    expect(click).toHaveBeenCalledTimes(1);
    expect(created[0].type).toBe('text/html;charset=utf-8');
    expect(created[1]).toBe(`Summary-${CALL_ID}.html`);
  });
});

describe('safeLinkHref', () => {
  it('keeps http, https and mailto addresses only', () => {
    expect(safeLinkHref('https://a.example.com/x')).toBe('https://a.example.com/x');
    expect(safeLinkHref(' HTTP://a.example.com ')).toBe('HTTP://a.example.com');
    expect(safeLinkHref('mailto:a@example.com')).toBe('mailto:a@example.com');
    // eslint-disable-next-line no-script-url -- the address under test
    ['javascript:alert(1)', 'data:text/html,x', 'vbscript:x', '//other.example.com', '#top', '', null].forEach((href) =>
      expect(safeLinkHref(href)).toBeNull(),
    );
  });
});

describe('the transcript web page', () => {
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
      CALLER: {
        segments: [
          segment('s1', 'CALLER', 'Sarah', 'Hello <script>alert(1)</script> & welcome', 1),
          segment('s2', 'CALLER', 'Sarah', 'Second line', 5),
        ],
      },
    },
  };

  it('writes every row, escaped, under the meeting facts, as one self-contained document', async () => {
    const html = await transcriptDownloadHtml(TRANSCRIPT, MEETING, null, { loadRenderer });
    expect(html).not.toMatch(/<script\b/i);
    expect(html).toContain('&lt;script&gt;');
    const page = parse(html);
    expect(page.title).toBe('Meeting Transcript: Weekly sync');
    const rows = [...page.querySelectorAll('.segment')].map((row) => row.textContent);
    expect(rows).toEqual([
      'Sarah [00:01.0]: Hello <script>alert(1)</script> & welcome',
      'Sarah [00:05.0]: Second line',
    ]);
    expect(page.querySelector('meta[http-equiv="Content-Security-Policy"]')).not.toBeNull();
  });

  it('saves as Transcript-<meeting>.html', async () => {
    const names = [];
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:x');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function record() {
      names.push(this.download);
    });
    await downloadTranscriptAsHtml(TRANSCRIPT, MEETING, null, { loadRenderer });
    expect(names).toEqual([`Transcript-${CALL_ID}.html`]);
  });
});
