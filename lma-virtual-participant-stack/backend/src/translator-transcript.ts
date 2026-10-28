/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Keeping the Amazon Transcribe transcript continuous, and writing the
 * assistant's turns from its own text.
 *
 * - A participant row whose Transcribe session ends (a reconnect, or the
 *   session failing) before its final arrives would stay "in progress" for the
 *   rest of the meeting. {@link OpenPartialTracker} records the last partial
 *   written and hands it back as a final exactly once, so the session's
 *   teardown can close the row.
 *
 * - In translator mode the assistant speaks the other language, and its
 *   channel of the Transcribe stream is opened in the meeting's language, so
 *   transcribing it would give a garbled rendering. The voice assistant
 *   already has the exact text, so it publishes each finished turn on
 *   {@link publishAssistantTurn} and the transcription path writes it as the
 *   assistant's row instead. The participants are on a channel of their own
 *   (transcribe-channels.ts), so nothing on theirs is held back while the
 *   assistant speaks.
 */

/** A Transcribe result as forwarded to Kinesis, with what it was sent with. */
export interface OpenPartial {
    result: any;
    speaker: string;
    timeOffsetSeconds: number;
}

/**
 * The partial most recently written to the transcript whose final has not been.
 *
 * A Transcribe result is one row that a later delivery with the same
 * `ResultId` updates in place; the row stops being "in progress" only when a
 * delivery with `IsPartial: false` is written for it. When the session ends
 * first, {@link close} returns the last partial re-stamped as final so the
 * caller can write it.
 */
export class OpenPartialTracker {
    private open: OpenPartial | null = null;

    /** Record a result that was written to the transcript. */
    noteSent(result: any, speaker: string, timeOffsetSeconds: number): void {
        if (result?.IsPartial && result?.Alternatives?.[0]?.Items?.length) {
            this.open = { result, speaker, timeOffsetSeconds };
        } else {
            this.open = null;
        }
    }

    /**
     * The open partial as a final, or null when nothing is open. Returns each
     * partial at most once.
     */
    close(): OpenPartial | null {
        const open = this.open;
        this.open = null;
        if (!open) {
            return null;
        }
        return { ...open, result: { ...open.result, IsPartial: false } };
    }

    /** A new Transcribe session: nothing from the previous one can be continued. */
    reset(): void {
        this.open = null;
    }
}

// ---------------------------------------------------------------------------
// Assistant turns
// ---------------------------------------------------------------------------

type AssistantTurnListener = (text: string) => void;
const listeners = new Set<AssistantTurnListener>();

/** The text the assistant spoke in one finished turn, without provider control markers. */
export function assistantTurnText(raw: string): string {
    return (raw || '')
        .replace(/\{\s*"interrupted"\s*:\s*true\s*\}/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
}

/** Called by a voice-assistant provider when the assistant has finished a spoken turn. */
export function publishAssistantTurn(raw: string): void {
    const text = assistantTurnText(raw);
    if (!text) {
        return;
    }
    for (const listener of listeners) {
        try {
            listener(text);
        } catch (error: any) {
            console.error(`Assistant turn listener failed: ${error?.message || error}`);
        }
    }
}

/** Subscribe to finished assistant turns. Returns the unsubscribe function. */
export function onAssistantTurn(listener: AssistantTurnListener): () => void {
    listeners.add(listener);
    return () => {
        listeners.delete(listener);
    };
}

/**
 * Start and end of an assistant row on the transcript timeline.
 *
 * `speechStartedAt` is the timeline position at which the assistant's audio
 * was last detected starting. It belongs to this turn only if it is after the
 * previous assistant row ended; otherwise (or if it is missing) the row is
 * placed at `now`, which keeps rows in order even when it cannot be precise.
 */
export function assistantRowSpan(
    now: number,
    speechStartedAt: number | null,
    previousRowEnd: number,
): { start: number; end: number } {
    const end = Math.max(now, previousRowEnd);
    const start =
        speechStartedAt !== null && speechStartedAt >= previousRowEnd && speechStartedAt <= end
            ? speechStartedAt
            : end;
    return { start, end };
}
