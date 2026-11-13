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
 * It gives the replies in the one form the notice's reply line gives ("LMA,
 * <phrase>", with the deployment's first consent and objection phrases), by
 * the routes this VP reads: typed or said where spoken replies are read, typed
 * otherwise, and said where the platform shows no chat sender.
 */

export interface ConsentHelpPhrases {
    consent: string;
    objection: string;
}

const DEFAULT_PHRASES: ConsentHelpPhrases = { consent: 'I consent', objection: 'I object' };

/** How replies are read in the meeting, which decides the line's wording. */
export type ConsentHelpRoute = 'chat' | 'chat_and_voice' | 'voice';

/** The line for one route and pair of phrases. */
export function consentHelpLineFor(route: ConsentHelpRoute, phrases: ConsentHelpPhrases = DEFAULT_PHRASES): string {
    const consent = `"LMA, ${phrases.consent}"`;
    const objection = `"LMA, ${phrases.objection}"`;
    const how = route === 'chat' ? 'type in this chat' : route === 'voice' ? 'say' : 'type or say';
    return `Meeting consent: ${how} ${consent} to agree to recording and transcription, or ${objection} to object.`;
}

let consentFlowActive = false;

let route: ConsentHelpRoute = 'chat';

let currentPhrases: ConsentHelpPhrases = DEFAULT_PHRASES;

/**
 * Set by the consent flow when it starts running in the meeting, and cleared
 * when it stops; `spoken` when it also reads replies said aloud,
 * `sendersHidden` where the platform shows no chat sender, and the
 * deployment's first phrases.
 */
export function setConsentHelpActive(
    active: boolean,
    spoken = false,
    phrases: ConsentHelpPhrases = DEFAULT_PHRASES,
    sendersHidden = false,
): void {
    consentFlowActive = active;
    route = !active || !spoken ? 'chat' : sendersHidden ? 'voice' : 'chat_and_voice';
    currentPhrases = phrases;
}

export function consentHelpActive(): boolean {
    return consentFlowActive;
}

/** The line to post now. */
export function consentHelpLine(): string {
    return consentHelpLineFor(route, currentPhrases);
}
