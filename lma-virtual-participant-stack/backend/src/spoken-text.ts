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
 * host name, and a table row is read with its column headings as labels on
 * every column but the first, which names the row ("Monday, High: 60°F.").
 *
 * The Strands Lambda has no notion of who is asking, so it cannot be asked for
 * a spoken answer instead; the chat keeps its markdown because nothing upstream
 * of the voice assistant changes.
 *
 * `LMA ask` answers in the meeting chat use the same conversion to reach plain
 * single-line text, with `keepUrls`: a reader can follow a link, so a link
 * keeps its address after its text and a bare URL is left whole.
 */

export interface SpokenTextOptions {
    /** Keep link addresses, for text that is read rather than spoken: a bare
     *  URL is left whole, and a link becomes "text (address)". */
    keepUrls?: boolean;
}

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

/**
 * The HTML formatting tags a markdown answer may carry. Only these are removed,
 * so prose that compares values ("x<5 and y>3") is left alone.
 */
const FORMATTING_TAG = /<\/?(?:a|b|i|u|s|em|strong|small|mark|sub|sup|span|code|p|div|br|hr|ul|ol|li|h[1-6]|table|thead|tbody|tr|td|th)\b[^<>]*>/gi;

/** Remove formatting tags, repeating until none is left. */
function stripFormattingTags(text: string): string {
    let previous: string;
    let current = text;
    do {
        previous = current;
        current = current.replace(FORMATTING_TAG, ' ');
    } while (current !== previous);
    return current;
}

/** A link written with its text: the text alone when spoken, and the text with
 *  its address when the result is to be read. */
function linkText(text: string, url: string, keepUrls: boolean): string {
    const address = url.trim().split(/\s+/)[0] ?? '';
    if (!keepUrls || !address) return text;
    if (!text.trim() || text.trim() === address) return address;
    return `${text} (${address})`;
}

/** Inline markup within one line: links, code, emphasis, HTML tags, bare URLs. */
function stripInline(text: string, options: SpokenTextOptions = {}): string {
    const keepUrls = options.keepUrls === true;
    return stripFormattingTags(text)
        // Autolinks, <https://...> and <someone@example.com>: the address
        // itself, which the bare-URL rule below then treats like any other.
        .replace(/<((?:https?|ftp):\/\/[^\s<>]+)>/gi, '$1')
        .replace(/<mailto:([^\s<>]+)>/gi, '$1')
        .replace(/<([^\s<>@]+@[^\s<>@]+\.[^\s<>@]+)>/g, '$1')
        // Images and links keep their text; a spoken link whose text is empty keeps nothing.
        .replace(/!\[([^\]]*)\]\(([^)]*)\)/g, (_m, alt: string, url: string) => linkText(alt, url, keepUrls))
        .replace(/\[([^\]]*)\]\(([^)]*)\)/g, (_m, label: string, url: string) => linkText(label, url, keepUrls))
        // Reference-style links, [text][ref].
        .replace(/\[([^\]]+)\]\[[^\]]*\]/g, '$1')
        // Code spans, delimited by a run of backticks of any length, so that
        // ```code``` on one line is unwrapped as well as `code`.
        .replace(/(`+)(?!`)([\s\S]*?[^`])\1(?!`)/g, (_m, _ticks: string, code: string) => code.trim())
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
        // Punctuation after a URL ends the sentence, not the URL, so it is kept.
        .replace(/https?:\/\/[^\s)]+/g, (url) => {
            if (keepUrls) return url;
            const trailing = /[.,;:!?]+$/.exec(url)?.[0] ?? '';
            return hostOf(url.slice(0, url.length - trailing.length)) + trailing;
        })
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
export function toSpokenText(markdown: string, options: SpokenTextOptions = {}): string {
    const inline = (text: string) => stripInline(text, options);
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

        // A fence line. Not one that opens with backticks and has a backtick
        // later in it: an info string cannot contain one, so ```code``` on a
        // single line is a code span, handled with the rest of the line's
        // inline markup below.
        if (/^(```|~~~)/.test(line) && (inFence || !/^`{3,}[^`]*`/.test(line))) {
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

        // A table's separator row, with or without outer pipes.
        if (TABLE_SEPARATOR.test(line)) {
            flushParagraph();
            continue;
        }

        const nextIsSeparator = TABLE_SEPARATOR.test((lines[i + 1] || '').trim());
        // A table row: one with outer pipes, a header (followed by the separator),
        // or, inside a table, any line with a pipe — GFM allows rows without
        // outer pipes.
        if (line.startsWith('|') || (line.includes('|') && (nextIsSeparator || tableHeader !== null))) {
            flushParagraph();
            const cells = tableCells(line).map(inline);
            if (!tableHeader && nextIsSeparator) {
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

        // A closing run of #s is markup only when a space separates it from
        // the text, so "## Learning C#" keeps its #.
        const heading = /^#{1,6}\s+(.*?)(?:\s+#+)?\s*$/.exec(line);
        if (heading) {
            flushParagraph();
            sentences.push(inline(heading[1]));
            continue;
        }

        const item = /^(?:>\s*)*(?:[-*+•]|\d{1,3}[.)])\s+(.*)$/.exec(line);
        if (item) {
            flushParagraph();
            sentences.push(inline(item[1]));
            continue;
        }

        const text = inline(line.replace(/^(>\s*)+/, ''));
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

/**
 * What a voice assistant hands its speech model as the result of a Strands
 * lookup: the answer the agent wrote, reduced to spoken text, or the whole
 * response as JSON when there is no written answer in it.
 *
 * Shared by the Nova Sonic and ElevenLabs agents, which both speak the result
 * rather than render it.
 */
export function spokenStrandsAnswer(responsePayload: unknown, log: (line: string) => void = console.log): string {
    const message = (responsePayload as { message?: unknown } | null)?.message;
    if (!message) return JSON.stringify(responsePayload);
    if (typeof message !== 'string') return JSON.stringify(message);
    const spoken = toSpokenText(message);
    if (spoken !== message) {
        log(`Strands answer reduced to spoken text (${message.length} -> ${spoken.length} chars)`);
    }
    return spoken;
}
