/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Turning a Strands agent answer into plain text for the voice assistant to say.
 *
 * The Strands agent writes for the meeting chat, which renders markdown, and
 * the voice assistant hands that same answer to Nova Sonic as the lookup's
 * tool result. Nova Sonic speaks the result rather than rendering it, so the
 * markup has nowhere to go: emphasis markers, headings, bullet points, link
 * targets and table pipes either reach the speech model as stray symbols or
 * survive into the text it reports as having said, where every blank line
 * shows up as an empty-looking block in the logs and the meeting transcript.
 *
 * `toSpokenText` keeps the words and drops the layout. Each heading, list item
 * and table row becomes a sentence of its own, so the structure the chat reader
 * gets from line breaks is kept as sentence boundaries for the listener, and
 * the result is one paragraph. A link is reduced to its text, a bare URL to its
 * host name, and a table row is read with its column headings as labels
 * ("Day: Monday, High: 60°F.").
 *
 * The Strands Lambda has no notion of who is asking, so it cannot be asked for
 * a spoken answer instead; the chat keeps its markdown because nothing upstream
 * of the voice assistant changes.
 */

/** A line that is only a table's header/body separator, e.g. `|---|:--:|`. */
const TABLE_SEPARATOR = /^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$/;

/** A line that is only a thematic break: `---`, `***`, `___`. */
const THEMATIC_BREAK = /^([-*_])(\s*\1){2,}$/;

const TERMINAL_PUNCTUATION = /[.!?:;…]["')\]]*$/;

/**
 * End a sentence that has no terminal punctuation of its own, so that it does
 * not run into the one after it.
 */
function asSentence(text: string): string {
    const trimmed = text.trim();
    if (!trimmed) return '';
    return TERMINAL_PUNCTUATION.test(trimmed) ? trimmed : `${trimmed}.`;
}

function tableCells(line: string): string[] {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
}

function hostOf(url: string): string {
    try {
        return new URL(url).hostname.replace(/^www\./, '');
    } catch {
        return url;
    }
}

/** Inline markup within one line: links, code, emphasis, HTML tags, bare URLs. */
function stripInline(text: string): string {
    return text
        // Images and links keep their text; a link whose text is empty keeps nothing.
        .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
        .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
        // Reference-style links, [text][ref].
        .replace(/\[([^\]]+)\]\[[^\]]*\]/g, '$1')
        .replace(/<br\s*\/?>/gi, ' ')
        .replace(/<\/?[a-z][^>]*>/gi, '')
        .replace(/`([^`]*)`/g, '$1')
        // Bold and italic, asterisk and underscore forms. The underscore forms
        // only at word boundaries, so snake_case identifiers keep their underscores.
        .replace(/\*\*\*([^*]+)\*\*\*/g, '$1')
        .replace(/\*\*([^*]+)\*\*/g, '$1')
        .replace(/(^|[^\w*])\*([^*\s][^*]*?)\*(?=[^\w*]|$)/g, '$1$2')
        .replace(/(^|\W)___([^_]+)___(?=\W|$)/g, '$1$2')
        .replace(/(^|\W)__([^_]+)__(?=\W|$)/g, '$1$2')
        .replace(/(^|\W)_([^_\s][^_]*?)_(?=\W|$)/g, '$1$2')
        .replace(/~~([^~]+)~~/g, '$1')
        // Any emphasis marker left unpaired.
        .replace(/\*{2,}/g, '')
        .replace(/https?:\/\/[^\s)]+/g, (url) => hostOf(url.replace(/[.,;:!?]+$/, '')))
        // A pipe used as a separator in prose ("High: 63°F | Low: 47°F").
        .replace(/\s+\|\s+/g, ', ')
        .replace(/[ \t]+/g, ' ')
        .trim();
}

/**
 * Plain spoken text for a markdown answer: one paragraph of sentences, with no
 * markup. Text that has no markdown in it comes back with only its whitespace
 * normalised: line breaks within a paragraph are spaces, and a sentence
 * boundary is added only between paragraphs that lack one.
 */
export function toSpokenText(markdown: string): string {
    const sentences: string[] = [];
    // A run of prose lines with no blank line between them is one paragraph in
    // markdown, so they are joined before being ended as a sentence.
    let paragraph: string[] = [];
    let tableHeader: string[] | null = null;
    let inFence = false;

    const flushParagraph = () => {
        if (paragraph.length) {
            sentences.push(paragraph.join(' '));
            paragraph = [];
        }
    };

    const lines = markdown.replace(/\r\n?/g, '\n').split('\n');
    for (let i = 0; i < lines.length; i++) {
        const raw = lines[i];
        const line = raw.trim();

        if (/^(```|~~~)/.test(line)) {
            flushParagraph();
            inFence = !inFence;
            continue;
        }
        if (inFence) {
            if (line) sentences.push(line);
            continue;
        }
        if (!line) {
            flushParagraph();
            tableHeader = null;
            continue;
        }
        if (THEMATIC_BREAK.test(line)) {
            flushParagraph();
            continue;
        }

        if (line.startsWith('|') || (line.includes('|') && TABLE_SEPARATOR.test((lines[i + 1] || '').trim()))) {
            flushParagraph();
            if (TABLE_SEPARATOR.test(line)) continue;
            const cells = tableCells(line).map(stripInline);
            if (!tableHeader && TABLE_SEPARATOR.test((lines[i + 1] || '').trim())) {
                tableHeader = cells;
                continue;
            }
            const header = tableHeader;
            const parts = cells
                .map((cell, c) => {
                    if (!cell) return '';
                    const label = header?.[c];
                    // The first column is usually the row's name; it reads
                    // better on its own than as "Day: Monday".
                    return label && c > 0 ? `${label}: ${cell}` : cell;
                })
                .filter(Boolean);
            if (parts.length) sentences.push(parts.join(', '));
            continue;
        }
        tableHeader = null;

        const heading = /^#{1,6}\s+(.*?)\s*#*$/.exec(line);
        if (heading) {
            flushParagraph();
            sentences.push(stripInline(heading[1]));
            continue;
        }

        const item = /^(?:>\s*)*(?:[-*+•]|\d{1,3}[.)])\s+(.*)$/.exec(line);
        if (item) {
            flushParagraph();
            sentences.push(stripInline(item[1]));
            continue;
        }

        const text = stripInline(line.replace(/^(>\s*)+/, ''));
        if (text) paragraph.push(text);
    }
    flushParagraph();

    // Every block but the last is ended as a sentence; the last keeps whatever
    // ending it had, so text with no markdown in it is returned as it was.
    const blocks = sentences.map((b) => b.trim()).filter(Boolean);
    return blocks
        .map((block, i) => (i < blocks.length - 1 ? asSentence(block) : block))
        .join(' ')
        .replace(/\s+/g, ' ')
        .trim();
}
