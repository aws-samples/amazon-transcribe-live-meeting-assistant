/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The line `LMA help` adds while the meeting runs a consent flow
 * (meeting-consent.ts), and whether it does. Dependency-free, so chat-help.ts
 * can read it without importing the flow.
 *
 * It names the built-in replies only, which the flow accepts whatever phrases
 * the deployment configured (consent-matcher.ts). Where the deployment reads
 * spoken replies, which count only when addressed to LMA, it says so.
 */
export const CONSENT_HELP_LINE =
    'Meeting consent: reply "LMA consent" to agree to recording and transcription, or "LMA no consent" to object.';

/** The line where spoken replies are read too. */
export const CONSENT_HELP_SPOKEN_LINE =
    'Meeting consent: reply "LMA consent" to agree to recording and transcription, or "LMA no consent" to object, typed here or said aloud.';

let consentFlowActive = false;

let spokenRepliesRead = false;

/**
 * Set by the consent flow when it starts running in the meeting, and cleared
 * when it stops; `spoken` when it also reads replies said aloud.
 */
export function setConsentHelpActive(active: boolean, spoken = false): void {
    consentFlowActive = active;
    spokenRepliesRead = active && spoken;
}

export function consentHelpActive(): boolean {
    return consentFlowActive;
}

/** The line to post now. */
export function consentHelpLine(): string {
    return spokenRepliesRead ? CONSENT_HELP_SPOKEN_LINE : CONSENT_HELP_LINE;
}
