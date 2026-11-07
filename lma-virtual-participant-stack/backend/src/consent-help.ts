/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The line `LMA help` adds while the meeting runs a consent flow
 * (meeting-consent.ts), and whether it does. Dependency-free, so chat-help.ts
 * can read it without importing the flow.
 *
 * It names the built-in replies only, which the flow accepts whatever phrases
 * the deployment configured (consent-matcher.ts).
 */
export const CONSENT_HELP_LINE =
    'Meeting consent: reply "LMA consent" to agree to recording and transcription, or "LMA no consent" to object, typed here or said aloud.';

let consentFlowActive = false;

/** Set by the consent flow when it starts running in the meeting, and cleared when it stops. */
export function setConsentHelpActive(active: boolean): void {
    consentFlowActive = active;
}

export function consentHelpActive(): boolean {
    return consentFlowActive;
}
