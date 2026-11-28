/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The email draft's HTML body (renderSummaryEmailHtml): the meeting's name and
 * facts, a note, each section rendered as the Summary tab renders it, the
 * attached files, HTML in the summary shown as text, and no picture.
 */
import { describe, it, expect } from 'vitest';

import { renderSummaryEmailHtml } from './html-export';

const render = (overrides = {}) =>
  renderSummaryEmailHtml({
    title: 'Weekly sync',
    facts: [
      { label: 'Meeting ID', value: 'Weekly sync-2026-11-03' },
      { label: 'Participants', value: 'Ana, Bo' },
    ],
    sections: [
      { title: 'SUMMARY', markdown: 'We **agreed** the plan.' },
      { title: 'ACTIONS', markdown: '- Ana: ship it\n- Bo: test it' },
    ],
    note: 'This meeting is still going on.',
    attachments: ['Summary-x.pdf', 'Slides-x.pdf'],
    ...overrides,
  });

const parse = (html) => new DOMParser().parseFromString(html, 'text/html');

describe('renderSummaryEmailHtml', () => {
  it('holds the title, the facts, the note, each section and the attached files', () => {
    const doc = parse(render());
    expect(doc.querySelector('h1').textContent).toBe('Weekly sync');
    expect([...doc.querySelectorAll('table.facts tr')].map((row) => row.textContent)).toEqual([
      'Meeting IDWeekly sync-2026-11-03',
      'ParticipantsAna, Bo',
    ]);
    expect(doc.querySelector('p.note').textContent).toBe('This meeting is still going on.');
    expect([...doc.querySelectorAll('h2')].map((h) => h.textContent)).toEqual(['SUMMARY', 'ACTIONS']);
    expect(doc.querySelector('strong').textContent).toBe('agreed');
    expect([...doc.querySelectorAll('li')].map((li) => li.textContent)).toEqual(['Ana: ship it', 'Bo: test it']);
    expect(doc.body.textContent).toContain('Attached: Summary-x.pdf, Slides-x.pdf');
    // Styled inline as well as in <head>, for mail apps that drop <style>.
    expect(doc.querySelector('h1').getAttribute('style')).toContain('font-size');
  });

  it('shows HTML in a section as text, and writes a picture as its caption', () => {
    const html = render({
      note: '',
      attachments: [],
      sections: [
        { title: 'SUMMARY', markdown: '<script>alert(1)</script> <b>bold?</b>' },
        {
          title: 'SCREEN SHARE SUMMARY',
          markdown: '![Slide at 0:30](keyframe:abc123)\n\n![x](https://example.com/p.png)',
        },
      ],
    });
    const doc = parse(html);
    expect(doc.querySelector('script')).toBeNull();
    expect(doc.querySelector('b')).toBeNull();
    expect(doc.querySelector('img')).toBeNull();
    expect(doc.body.textContent).toContain('<script>alert(1)</script>');
    expect(doc.body.textContent).toContain('(Slide at 0:30)');
    expect(html).not.toContain('example.com/p.png');
    expect(doc.querySelector('p.note')).toBeNull();
    expect(doc.body.textContent).not.toContain('Attached:');
  });
});
