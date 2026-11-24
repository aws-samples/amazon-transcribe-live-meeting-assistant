/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Final utterances heard in the meeting, for the meeting consent flow to read
 * as spoken consent replies and objections (meeting-consent.ts).
 *
 * Two sources report here: the transcription service, for every final
 * participant result while the meeting is captured, and the consent listener
 * (consent-listener.ts), for what it hears while consent is awaited. Neither
 * reports the voice assistant's own audio.
 *
 * Dependency-free, like consent-roster.ts's sightings, so the transcription
 * service can report without importing the consent flow.
 */

/** A transcript line as the meeting stores it. */
export interface UtteranceLine {
    segmentId: string;
    channel: string;
    /** The speaker as stored: the attendee's name, with the engine's voice label when it has one. */
    speaker: string;
}

export interface SpokenUtterance {
    /** The participant the VP attributes the audio to, or null when it cannot name one. */
    speaker: string | null;
    /** The final text of the utterance. Never logged by anything that receives it. */
    text: string;
    /**
     * The transcript line the utterance is written as, when it is one: only
     * the transcription service's results while the meeting is captured, and
     * only when the whole utterance is one line's speaker (see
     * singleSpeakerLine). A reply read from it is linked to that line's speaker.
     * Absent for what the consent listener hears, which is never written.
     */
    line?: UtteranceLine;
}

/**
 * The line an utterance written as `lines` is linked by: the first of them,
 * when every one is on the same channel under the same stored speaker. None
 * when there are no lines, or when the utterance was split between speakers,
 * since nothing then says which of them gave a reply read from the whole.
 */
export function singleSpeakerLine(lines: readonly UtteranceLine[]): UtteranceLine | undefined {
    const [first] = lines;
    if (!first || !first.segmentId || !first.speaker.trim()) return undefined;
    const same = lines.every((line) => line.channel === first.channel && line.speaker.trim() === first.speaker.trim());
    return same ? first : undefined;
}

type SpokenUtteranceListener = (utterance: SpokenUtterance) => void;

const listeners = new Set<SpokenUtteranceListener>();

export function noteSpokenUtterance(utterance: SpokenUtterance): void {
    if (!utterance.text || !utterance.text.trim()) return;
    for (const listener of listeners) {
        try {
            listener(utterance);
        } catch {
            /* a listener must never break transcription */
        }
    }
}

export function onSpokenUtterance(listener: SpokenUtteranceListener): () => void {
    listeners.add(listener);
    return () => listeners.delete(listener);
}
