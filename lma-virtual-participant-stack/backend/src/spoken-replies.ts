/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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

export interface SpokenUtterance {
    /** The participant the VP attributes the audio to, or null when it cannot name one. */
    speaker: string | null;
    /** The final text of the utterance. Never logged by anything that receives it. */
    text: string;
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
