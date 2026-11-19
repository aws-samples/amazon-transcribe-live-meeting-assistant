/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Chat attachments: the images and files shared in the meeting chat, stored
 * with the meeting beside the chat message that carried them.
 *
 * Each platform's chat reader finds the attachments in a message row and
 * offers them with the message (`IncomingChat.attachments`, meeting-chat.ts).
 * When the message is added to the transcript, and the Virtual Participant
 * setting "Capture chat attachments" is on, each attachment is handed to
 * `ChatAttachmentCapture`, which, one attachment at a time and in the
 * background, so the chat capture is never held up:
 *
 * 1. fetches it with the closure the reader gave it (an in-page fetch of the
 *    image's `blob:` address, or Zoom's file card clicked and the browser's
 *    download read back), with `MAX_ATTACHMENT_BYTES` as the cap;
 * 2. names its type from its leading bytes (`sniffContentType`), not from its
 *    name, and keeps it only when the type is in `ALLOWED_CONTENT_TYPES`;
 * 3. uploads it to the recordings bucket under
 *    `lma-chat-attachments/<posixified CallId>/<AttachmentId>/<safe file name>`,
 *    where the bucket's default encryption applies, as it does to the
 *    recordings;
 * 4. announces it with an ADD_CHAT_ATTACHMENT event naming the chat segment.
 *
 * An attachment that is not stored -- one the platform does not let the VP
 * download (a Teams file card for a guest), one over the cap, one of a type not
 * allowed, one whose fetch failed -- is still announced, with its name and
 * `Status: NOT_STORED` and the reason, so the meeting record shows that it was
 * shared.
 *
 * The file is never opened, rendered or run in the Virtual Participant: its
 * bytes are read, inspected for their type and uploaded, nothing else.
 *
 * Nothing here logs a file name, a sender or any content: only counts, kinds,
 * sizes and outcomes.
 *
 * Pure apart from the logger: uploading, emitting and the gates are passed in,
 * and chat-attachment-runtime.ts wires them to this VP.
 */
import { randomBytes } from 'node:crypto';
import { createLogger } from './logger.js';

const log = createLogger('chat-attachments');

/** The environment variable stack-settings.ts sets from `virtual_participant.captureChatAttachments`. */
export const CAPTURE_CHAT_ATTACHMENTS_VARIABLE = 'CAPTURE_CHAT_ATTACHMENTS';
/** The setting's shipped default, used when the configuration could not be read. */
export const CAPTURE_CHAT_ATTACHMENTS_DEFAULT = false;
/** The environment variable stack-settings.ts sets from `virtual_participant.chatAttachmentSummaries`. */
export const CHAT_ATTACHMENT_SUMMARIES_VARIABLE = 'CHAT_ATTACHMENT_SUMMARIES';
/** The setting's shipped default, used when the configuration could not be read. */
export const CHAT_ATTACHMENT_SUMMARIES_DEFAULT = true;

/** The folder the attachments are stored under in the recordings bucket. */
export const CHAT_ATTACHMENTS_PREFIX = 'lma-chat-attachments/';
/** The largest attachment stored. */
export const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;
/** Attachments taken from one message; more are left out. */
export const MAX_ATTACHMENTS_PER_MESSAGE = 10;
/** Attachments taken in one meeting; more are left out. */
export const MAX_ATTACHMENTS_PER_MEETING = 200;
/** Longest file name kept for display. */
export const MAX_FILE_NAME_LENGTH = 255;
/** Longest file name used in the object key. */
const MAX_KEY_NAME_LENGTH = 120;

export type AttachmentKind = 'image' | 'document';

/** Why an attachment was announced without its content. */
export type NotStoredReason = 'NOT_DOWNLOADABLE' | 'TOO_LARGE' | 'TYPE_NOT_ALLOWED' | 'UNAVAILABLE';

/** What fetching an attachment gave. */
export type AttachmentFetchResult =
    | { ok: true; bytes: Buffer }
    | { ok: false; reason: 'too-large' | 'unavailable'; size?: number };

/** One attachment of a chat message, as a platform's reader found it. */
export interface IncomingChatAttachment {
    kind: AttachmentKind;
    /** The file name the platform shows. */
    name: string;
    /** The size the platform shows, in bytes, when it shows one. */
    size?: number | null;
    /**
     * Fetches the content, refusing anything over `maxBytes`. Absent when the
     * platform does not let the VP download it.
     */
    fetch?: (maxBytes: number) => Promise<AttachmentFetchResult>;
}

/** The fields of an ADD_CHAT_ATTACHMENT event other than its envelope. */
export interface ChatAttachmentEvent {
    /** Zero-padded milliseconds on the transcript timeline plus a suffix, so ids sort in time. */
    AttachmentId: string;
    /** The MEETING_CHAT segment of the message that carried it. */
    SegmentId: string;
    /** Seconds on the transcript timeline: the chat segment's StartTime. */
    Timestamp: number;
    Kind: 'IMAGE' | 'DOCUMENT';
    FileName: string;
    Platform: string;
    Status: 'STORED' | 'NOT_STORED';
    NotStoredReason?: NotStoredReason;
    /** The type read from the content; only for a stored attachment. */
    ContentType?: string;
    /** Bytes stored, or the size the platform showed for one not stored. */
    Size?: number;
    /** The object key in the recordings bucket; only for a stored attachment. */
    S3Key?: string;
    /** Whether the stored attachment is to get an AI title and summary. */
    Summarize: boolean;
}

const OOXML: Record<string, { type: string; part: string }> = {
    docx: { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', part: 'word/' },
    xlsx: { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', part: 'xl/' },
    pptx: { type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation', part: 'ppt/' },
};
const LEGACY_OFFICE: Record<string, string> = {
    doc: 'application/msword',
    xls: 'application/vnd.ms-excel',
    ppt: 'application/vnd.ms-powerpoint',
};
const TEXT: Record<string, string> = {
    txt: 'text/plain',
    csv: 'text/csv',
    md: 'text/markdown',
};

/**
 * The types stored. Raster images, PDF, Office documents, and plain text, CSV
 * and Markdown. Not HTML, SVG or anything that can carry script, and no
 * archives or programs.
 */
export const ALLOWED_CONTENT_TYPES: ReadonlySet<string> = new Set([
    'image/png',
    'image/jpeg',
    'image/gif',
    'image/webp',
    'application/pdf',
    ...Object.values(OOXML).map((o) => o.type),
    ...Object.values(LEGACY_OFFICE),
    ...Object.values(TEXT),
]);

function extensionOf(name: string): string {
    const match = /\.([A-Za-z0-9]{1,8})$/.exec((name || '').trim());
    return match ? match[1].toLowerCase() : '';
}

function startsWith(bytes: Buffer, signature: readonly number[], offset = 0): boolean {
    if (bytes.length < offset + signature.length) return false;
    return signature.every((b, i) => bytes[offset + i] === b);
}

const ascii = (text: string): number[] => Array.from(text, (c) => c.charCodeAt(0));

/** Whether the bytes are UTF-8 text: no NUL, and they decode without error. */
function isText(bytes: Buffer): boolean {
    const head = bytes.subarray(0, 64 * 1024);
    if (head.includes(0)) return false;
    try {
        new TextDecoder('utf-8', { fatal: true }).decode(bytes);
        return true;
    } catch {
        return false;
    }
}

/**
 * The content type of an attachment, read from its leading bytes, or null when
 * it is not one of `ALLOWED_CONTENT_TYPES`. The name only tells apart formats
 * that share a container (an Office document is a ZIP or an OLE file; plain
 * text, CSV and Markdown are all text), and only after the bytes have shown
 * the container: a file named ".png" that is not a PNG is not an image here.
 */
export function sniffContentType(bytes: Buffer, name: string): string | null {
    if (!bytes || bytes.length === 0) return null;
    if (startsWith(bytes, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return 'image/png';
    if (startsWith(bytes, [0xff, 0xd8, 0xff])) return 'image/jpeg';
    if (startsWith(bytes, ascii('GIF87a')) || startsWith(bytes, ascii('GIF89a'))) return 'image/gif';
    if (startsWith(bytes, ascii('RIFF')) && startsWith(bytes, ascii('WEBP'), 8)) return 'image/webp';
    if (startsWith(bytes, ascii('%PDF-'))) return 'application/pdf';
    const extension = extensionOf(name);
    if (startsWith(bytes, [0x50, 0x4b, 0x03, 0x04])) {
        const ooxml = OOXML[extension];
        // An Office Open XML file is a ZIP whose parts are listed by name; the
        // central directory at the end names them even when the first entry is
        // something else.
        if (ooxml && bytes.includes('[Content_Types].xml') && bytes.includes(ooxml.part)) return ooxml.type;
        return null;
    }
    if (startsWith(bytes, [0xd0, 0xcf, 0x11, 0xe0, 0xa1, 0xb1, 0x1a, 0xe1])) {
        return LEGACY_OFFICE[extension] ?? null;
    }
    const text = TEXT[extension];
    if (text && isText(bytes)) return text;
    return null;
}

/** The kind an attachment is stored as: from its type when it has one, else as the platform showed it. */
export function attachmentKind(contentType: string | undefined, shown: AttachmentKind): 'IMAGE' | 'DOCUMENT' {
    if (contentType) return contentType.startsWith('image/') ? 'IMAGE' : 'DOCUMENT';
    return shown === 'image' ? 'IMAGE' : 'DOCUMENT';
}

/** The name shown: the platform's, with control characters removed, trimmed, never empty. */
export function displayFileName(name: string | null | undefined, kind: AttachmentKind): string {
    // eslint-disable-next-line no-control-regex
    const clean = `${name ?? ''}`.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim();
    const fallback = kind === 'image' ? 'image' : 'file';
    if (!clean) return fallback;
    return clean.length <= MAX_FILE_NAME_LENGTH ? clean : clean.slice(0, MAX_FILE_NAME_LENGTH);
}

/**
 * The file name used in the object key: letters, digits, ".", "_" and "-"
 * only, no leading dot, at most 120 characters with the extension kept.
 */
export function safeKeyName(name: string): string {
    let safe = (name || '').replace(/[^A-Za-z0-9._-]+/g, '_').replace(/_+/g, '_').replace(/^[._]+/, '');
    if (safe.length > MAX_KEY_NAME_LENGTH) {
        const extension = extensionOf(safe);
        const tail = extension ? `.${extension}` : '';
        safe = safe.slice(0, MAX_KEY_NAME_LENGTH - tail.length) + tail;
    }
    return safe || 'attachment';
}

/** Same rule as posixifyFilename in keyframe-sampler.ts, and the Python one meeting deletion uses. */
export function posixifyCallId(name: string): string {
    return name.replace(/[^a-zA-Z0-9_.]/g, '_').replace(/^_+/, '').replace(/_+$/, '');
}

/** The object key of a stored attachment. */
export function attachmentKey(callId: string, attachmentId: string, fileName: string): string {
    return `${CHAT_ATTACHMENTS_PREFIX}${posixifyCallId(callId)}/${attachmentId}/${safeKeyName(fileName)}`;
}

/** Zero-padded milliseconds, so ids sort in time order, plus a suffix that keeps them unique. */
export function attachmentIdFor(timelineSeconds: number, suffix: string = randomBytes(4).toString('hex')): string {
    const ms = Math.max(0, Math.round(timelineSeconds * 1000));
    return `${String(ms).padStart(10, '0')}-${suffix}`;
}

/** The note added to a chat message's transcript text for each of its attachments. */
export function attachmentNote(name: string): string {
    return `(attachment: ${name})`;
}

function booleanSetting(env: Record<string, string | undefined>, name: string, fallback: boolean): boolean {
    const raw = (env[name] ?? '').trim().toLowerCase();
    if (raw === 'true') return true;
    if (raw === 'false') return false;
    return fallback;
}

/** Whether "Capture chat attachments" is on, read from the environment stack-settings.ts sets. */
export function captureChatAttachmentsSetting(env: Record<string, string | undefined> = process.env): boolean {
    return booleanSetting(env, CAPTURE_CHAT_ATTACHMENTS_VARIABLE, CAPTURE_CHAT_ATTACHMENTS_DEFAULT);
}

/** Whether "AI titles and summaries for chat attachments" is on. */
export function chatAttachmentSummariesSetting(env: Record<string, string | undefined> = process.env): boolean {
    return booleanSetting(env, CHAT_ATTACHMENT_SUMMARIES_VARIABLE, CHAT_ATTACHMENT_SUMMARIES_DEFAULT);
}

/** An error's name, for a log line that must not quote what the error says. */
export function errorName(error: unknown): string {
    const name = (error as { name?: unknown } | null)?.name;
    return typeof name === 'string' && /^[A-Za-z0-9_.-]{1,64}$/.test(name) ? name : 'error';
}

export interface ChatAttachmentDeps {
    /** Whether "Capture chat attachments" is on. */
    settingOn: () => boolean;
    /** Whether stored attachments are to get an AI title and summary. */
    summariesOn: () => boolean;
    /**
     * Whether the meeting is being captured now (`details.start`: not paused,
     * consent not withheld). Read again after the fetch, before anything is
     * stored.
     */
    capturing: () => boolean;
    callId: () => string;
    upload: (key: string, body: Buffer, contentType: string) => Promise<void>;
    emit: (event: ChatAttachmentEvent) => Promise<void>;
    newId?: (timelineSeconds: number) => string;
    /**
     * The chat line's segment id and time as the meeting stores them. A
     * rejoined VP's session prefixes its segment ids and moves its times by the
     * session's offset (kinesis-stream.ts useSession), so an attachment names
     * its chat line the same way. Its id, and so its object key, comes from the
     * moved time: a session's offset follows everything earlier sessions
     * recorded, so no earlier attachment can have the same time prefix.
     * Omitted, the VP's own values are used.
     */
    onMeetingTimeline?: (segmentId: string, timelineSeconds: number) => { segmentId: string; timelineSeconds: number };
}

interface Pending {
    segmentId: string;
    platform: string;
    timestamp: number;
    attachment: IncomingChatAttachment;
}

/**
 * The attachment capture for one meeting. `capture` decides at once which
 * attachments are taken; they are fetched, stored and announced one at a time
 * in the background, in the order they were offered.
 */
export class ChatAttachmentCapture {
    private queue: Promise<void> = Promise.resolve();
    private taken = 0;
    private limitLogged = false;

    constructor(private readonly deps: ChatAttachmentDeps) {}

    /** Attachments taken in this meeting so far. */
    get takenCount(): number {
        return this.taken;
    }

    /**
     * Takes the attachments of a chat message that has been added to the
     * transcript as `segmentId`, when the setting is on. Returns how many were
     * taken.
     */
    capture(segmentId: string, platform: string, timestamp: number, attachments: readonly IncomingChatAttachment[]): number {
        if (!attachments.length || !this.deps.settingOn()) return 0;
        if (this.deps.onMeetingTimeline) {
            const moved = this.deps.onMeetingTimeline(segmentId, timestamp);
            segmentId = moved.segmentId;
            timestamp = moved.timelineSeconds;
        }
        let taken = 0;
        for (const attachment of attachments.slice(0, MAX_ATTACHMENTS_PER_MESSAGE)) {
            if (this.taken >= MAX_ATTACHMENTS_PER_MEETING) {
                if (!this.limitLogged) {
                    this.limitLogged = true;
                    log.info(`[chat-attachments] attachment limit reached (${MAX_ATTACHMENTS_PER_MEETING} in this meeting); leaving more out`);
                }
                break;
            }
            this.taken += 1;
            taken += 1;
            const pending: Pending = { segmentId, platform, timestamp, attachment };
            this.queue = this.queue.then(() => this.process(pending)).catch((error) => {
                // The error's name only: an S3 or Kinesis message can quote the object key.
                log.warn(`[chat-attachments] could not handle an attachment (${errorName(error)})`);
            });
        }
        return taken;
    }

    /** Resolves once every attachment taken so far has been handled. */
    idle(): Promise<void> {
        return this.queue;
    }

    private async process(pending: Pending): Promise<void> {
        const { attachment } = pending;
        const name = displayFileName(attachment.name, attachment.kind);
        const id = (this.deps.newId ?? attachmentIdFor)(pending.timestamp);
        const base = {
            AttachmentId: id,
            SegmentId: pending.segmentId,
            Timestamp: pending.timestamp,
            FileName: name,
            Platform: pending.platform,
        };
        const shownSize = typeof attachment.size === 'number' && Number.isFinite(attachment.size) && attachment.size >= 0 ? Math.round(attachment.size) : undefined;
        const notStored = async (reason: NotStoredReason, size?: number): Promise<void> => {
            log.info(`[chat-attachments] recorded an attachment without its content (kind=${attachment.kind} reason=${reason})`);
            await this.deps.emit({
                ...base,
                Kind: attachmentKind(undefined, attachment.kind),
                Status: 'NOT_STORED',
                NotStoredReason: reason,
                ...(size !== undefined ? { Size: size } : {}),
                Summarize: false,
            });
        };

        if (!attachment.fetch) return notStored('NOT_DOWNLOADABLE', shownSize);
        if (shownSize !== undefined && shownSize > MAX_ATTACHMENT_BYTES) return notStored('TOO_LARGE', shownSize);

        let fetched: AttachmentFetchResult;
        try {
            fetched = await attachment.fetch(MAX_ATTACHMENT_BYTES);
        } catch {
            fetched = { ok: false, reason: 'unavailable' };
        }
        if (!fetched.ok) {
            return notStored(fetched.reason === 'too-large' ? 'TOO_LARGE' : 'UNAVAILABLE', fetched.size ?? shownSize);
        }
        const { bytes } = fetched;
        if (bytes.length > MAX_ATTACHMENT_BYTES) return notStored('TOO_LARGE', bytes.length);
        const contentType = sniffContentType(bytes, name);
        if (!contentType || !ALLOWED_CONTENT_TYPES.has(contentType)) return notStored('TYPE_NOT_ALLOWED', bytes.length);

        // The fetch can take a while: the meeting may have been paused, or
        // consent withdrawn, or the setting turned off, meanwhile.
        if (!this.deps.capturing() || !this.deps.settingOn()) {
            log.info('[chat-attachments] capture stopped while an attachment was fetched; not storing it');
            return;
        }

        const key = attachmentKey(this.deps.callId(), id, name);
        try {
            await this.deps.upload(key, bytes, contentType);
        } catch (error) {
            log.warn(`[chat-attachments] could not upload an attachment (${errorName(error)})`);
            return notStored('UNAVAILABLE', bytes.length);
        }
        log.info(`[chat-attachments] stored an attachment (kind=${attachmentKind(contentType, attachment.kind)} type=${contentType} bytes=${bytes.length})`);
        await this.deps.emit({
            ...base,
            Kind: attachmentKind(contentType, attachment.kind),
            Status: 'STORED',
            ContentType: contentType,
            Size: bytes.length,
            S3Key: key,
            Summarize: this.deps.summariesOn(),
        });
    }
}
