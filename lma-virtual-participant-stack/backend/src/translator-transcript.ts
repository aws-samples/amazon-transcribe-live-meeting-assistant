/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Keeping a translator meeting's transcript continuous.
 *
 * In translator mode the assistant speaks a translation into the meeting, and
 * on the Amazon Transcribe path that audio reaches the same stream as the
 * participants (agent_output is looped into combined_audio). The stream is
 * opened in the meeting's configured language, so the translation comes back
 * as a garbled rendering of the other language; scribe.ts therefore keeps
 * results that arrive while the assistant is speaking out of the transcript.
 *
 * Two things follow from that, and this module holds the logic for both:
 *
 * - A participant's utterance that the assistant starts answering before
 *   Transcribe has finalised it is delivered as one result whose final also
 *   carries the assistant's speech. That final is held back with the rest, so
 *   the participant's row has to be closed from the last partial that was
 *   sent, or it stays in progress for the rest of the meeting.
 *   {@link OpenPartialTracker} records that partial and hands it back as a
 *   final exactly once.
 *
 * - The translation itself is then absent from the transcript. The voice
 *   assistant already has its exact text, so it publishes each finished turn
 *   on {@link publishAssistantTurn} and the transcription path writes it as the
 *   assistant's row instead of transcribing the audio.
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
 * delivery with `IsPartial: false` is written for it. When that final is
 * withheld, {@link close} returns the last partial re-stamped as final so the
 * caller can write it, and remembers the id so that the withheld final — or
 * any later delivery for the same result — does not reopen or duplicate the
 * row.
 */
export class OpenPartialTracker {
    private open: OpenPartial | null = null;
    // Closed result id -> session time at which its written row ends.
    private readonly closedIds = new Map<string, number>();

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
        if (open.result.ResultId) {
            const items: any[] = open.result.Alternatives?.[0]?.Items ?? [];
            const ends = items.map((item) => item?.EndTime).filter((t) => typeof t === 'number');
            this.closedIds.set(open.result.ResultId, ends.length ? Math.max(...ends) : 0);
        }
        return { ...open, result: { ...open.result, IsPartial: false } };
    }

    /**
     * Session time at which the row {@link close} wrote for `result` ends, or
     * null if `result` does not belong to a closed row. Its own final ends the
     * result, so the id is forgotten once that has been seen.
     */
    closedRowEnd(result: any): number | null {
        const id = result?.ResultId;
        const end = id ? this.closedIds.get(id) : undefined;
        if (end === undefined) {
            return null;
        }
        if (!result.IsPartial) {
            this.closedIds.delete(id);
        }
        return end;
    }

    /** Whether `result` belongs to a closed row; forgets it on its final, like {@link closedRowEnd}. */
    isClosed(result: any): boolean {
        return this.closedRowEnd(result) !== null;
    }

    /** A new Transcribe session: nothing from the previous one can be continued. */
    reset(): void {
        this.open = null;
        this.closedIds.clear();
    }
}

/**
 * The words of `result` that start at or after `sessionSeconds` (on the
 * result's own session timeline), as a separate result, or null if there are
 * none. Its id is derived from the original, so its partials and final update
 * one row of their own.
 */
export function resultAfter(result: any, sessionSeconds: number): any | null {
    const items: any[] = result?.Alternatives?.[0]?.Items ?? [];
    const first = items.findIndex(
        (item) => item?.Type === 'pronunciation' && (item.StartTime ?? -Infinity) >= sessionSeconds,
    );
    if (first === -1) {
        return null;
    }
    const kept = items.slice(first);
    let transcript = '';
    for (const item of kept) {
        if (item.Type === 'pronunciation' && transcript) {
            transcript += ' ';
        }
        transcript += item.Content ?? '';
    }
    return {
        ...result,
        ResultId: result.ResultId ? `${result.ResultId}-after` : result.ResultId,
        StartTime: kept[0].StartTime,
        Alternatives: [{ ...result.Alternatives[0], Transcript: transcript, Items: kept }],
    };
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
