/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * What the dialog watchdog does with a classified dialog: click it, hand it to
 * a human, or leave it alone.
 *
 * Kept free of Playwright and of the VP's runtime configuration so the rules
 * can be unit tested. The watchdog supplies the classification from the AI
 * resolver and the visible text of the button the resolver picked.
 */
import type { DialogAnalysis } from './ai-dom-resolver.js';

/** Dialog types the watchdog dismisses on its own when given a button. */
export const AUTOCLICK_TYPES: ReadonlySet<string> = new Set(['CONSENT', 'RECORDING_NOTICE']);

/**
 * Button labels that join or continue without meeting audio. Clicking one of
 * these leaves the VP in the meeting with nothing to transcribe or record, so
 * the watchdog never clicks them.
 */
const NO_AUDIO_ACTION =
    /\bwithout\s+(?:computer\s+)?(?:audio|sound|media)\b|\b(?:don'?t|do\s+not)\s+use\s+(?:computer\s+)?audio\b|\bno\s+audio\b/i;

/** Text that identifies a camera/microphone permission dialog. */
const MEDIA_PERMISSION_TEXT =
    /\b(?:camera|microphone|mic|media\s+devices?|audio\s+devices?|getusermedia)\b/i;

export const NO_AUDIO_WARNING =
    "Do not choose 'Continue without audio or video': the meeting will not be transcribed or recorded.";

export function isNoAudioAction(actionText: string | null | undefined): boolean {
    return !!actionText && NO_AUDIO_ACTION.test(actionText);
}

export function isMediaPermissionDialog(
    analysis: Pick<DialogAnalysis, 'message' | 'mediaPermission'>,
    actionText?: string | null,
): boolean {
    return (
        !!analysis.mediaPermission ||
        MEDIA_PERMISSION_TEXT.test(analysis.message || '') ||
        isNoAudioAction(actionText)
    );
}

export type DialogDecision =
    | { kind: 'click'; selector: string }
    | { kind: 'escalate'; message: string }
    | { kind: 'ignore' };

/** The manual-action message for a dialog, with the no-audio warning added to
 * camera/microphone permission dialogs. */
export function manualActionMessage(
    analysis: Pick<DialogAnalysis, 'message' | 'mediaPermission'>,
    platform: string,
    actionText?: string | null,
): string {
    const base = analysis.message || `Manual action required in the ${platform} web client.`;
    return isMediaPermissionDialog(analysis, actionText) ? `${base} ${NO_AUDIO_WARNING}` : base;
}

/**
 * Decide what to do with a classified dialog.
 *
 * `actionText` is the visible text of the element at
 * `analysis.primaryActionSelector` (label, aria-label, title), or null when
 * that element could not be found or read.
 *
 * - An auto-click type with a button is clicked whatever the model returned
 *   for `needsHuman`, unless the button joins without audio or cannot be read.
 *   Both of those go to a human instead.
 * - An auto-click type with no button goes to a human: the dialog is blocking
 *   and the watchdog has nothing it can safely click.
 * - Any other type goes to a human when the model says one is needed, and is
 *   otherwise left alone.
 */
export function decideDialogAction(
    analysis: DialogAnalysis,
    platform: string,
    actionText: string | null,
): DialogDecision {
    const escalate = (): DialogDecision => ({
        kind: 'escalate',
        message: manualActionMessage(analysis, platform, actionText),
    });

    if (AUTOCLICK_TYPES.has(analysis.type)) {
        const selector = analysis.primaryActionSelector;
        if (!selector || actionText === null || isNoAudioAction(actionText)) return escalate();
        return { kind: 'click', selector };
    }
    return analysis.needsHuman ? escalate() : { kind: 'ignore' };
}
