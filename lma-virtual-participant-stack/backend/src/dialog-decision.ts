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

/**
 * Whether a dialog is about camera/microphone access. An action that joins
 * without audio always counts. Otherwise the model's `mediaPermission` answer
 * decides; the keyword check on its message is used only when that answer is
 * missing, so an in-meeting notice that merely mentions the microphone is not
 * treated as a permission dialog.
 */
export function isMediaPermissionDialog(
    analysis: Pick<DialogAnalysis, 'message' | 'mediaPermission'>,
    actionText?: string | null,
): boolean {
    if (isNoAudioAction(actionText)) return true;
    if (typeof analysis.mediaPermission === 'boolean') return analysis.mediaPermission;
    return MEDIA_PERMISSION_TEXT.test(analysis.message || '');
}

/*
 * The text matched is the model's description of the dialog (DialogAnalysis
 * message), not the dialog's own words, so these are stricter than Zoom's
 * fixed end-dialog phrases (end-signals.ts ZOOM_END_DIALOG_PHRASES), which
 * are read from Zoom's own dialogs: an in-meeting notice that another
 * participant was removed, or that screen sharing, a poll or the breakout
 * rooms ended, must not end the VP's meeting.
 */
/** Text of a dialog saying the VP itself was removed from the meeting. */
const REMOVED_TEXT =
    /\b(?:you|user)\s+(?:have|has|were|was)\s+(?:been\s+)?removed\b|\bremoved\s+you\b/i;
/** Text of a dialog saying the meeting itself has ended. */
const ENDED_TEXT =
    /\b(?:meeting|call)\s+(?:has|was)\s+(?:been\s+)?ended\b|\bended\s+(?:the|this)\s+(?:meeting|call)\b/i;

/** Dialog types a removal or end notice can be classified as. */
const MEETING_OVER_TYPES: ReadonlySet<string> = new Set(['BLOCKED', 'OTHER']);

/**
 * How a dialog says the meeting is over for the VP: it was removed, or the
 * meeting ended; null when it says neither. Read from the model's message,
 * for the types such a notice is classified as.
 */
export function meetingOverReason(
    analysis: Pick<DialogAnalysis, 'type' | 'message'>,
): 'removed-from-meeting' | 'host-ended' | null {
    if (!MEETING_OVER_TYPES.has(analysis.type)) return null;
    const message = analysis.message || '';
    if (REMOVED_TEXT.test(message)) return 'removed-from-meeting';
    if (ENDED_TEXT.test(message)) return 'host-ended';
    return null;
}

export type DialogDecision =
    | { kind: 'click'; selector: string }
    | { kind: 'escalate'; message: string }
    /** The dialog says the meeting is over for the VP: an exit, never a manual action. */
    | { kind: 'meeting-over'; reason: 'removed-from-meeting' | 'host-ended' }
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
 * - An auto-click type with a readable button is clicked whatever the model
 *   returned for `needsHuman`, except that a button that joins without audio
 *   is never clicked: that dialog goes to a human.
 * - A camera/microphone dialog the watchdog cannot safely answer (no button, a
 *   button it cannot read, or an unlabeled one such as an icon-only close,
 *   which can dismiss it without audio) goes to a human. It blocks the join.
 * - Any other dialog the watchdog cannot click goes to a human only when the
 *   model says one is needed, and is otherwise left alone. That keeps an
 *   in-meeting notice without a usable button from changing a joined VP's
 *   status.
 * - Once the VP is in the meeting (`inMeeting`), a dialog the model could not
 *   classify (OTHER) is left alone even when the model says a human is needed:
 *   the VP is capturing, and an unclassified panel (a whiteboard loading, a
 *   feature tip) does not stop it. Escalating it marked a working VP as
 *   needing manual action. Before the join such a dialog may be what blocks
 *   it, so it still goes to a human there; the classified blocking types
 *   (CAPTCHA, sign-in, SSO, blocked) do at any time.
 * - A dialog saying the VP was removed, or that the meeting ended, is the
 *   meeting's end, not something a human can act on: `meeting-over`, never
 *   escalated, whatever its type.
 * - Once the meeting is over for the VP (`meetingOver`: it has ended, or the
 *   VP's teardown has begun), every dialog is left alone. Nothing a human
 *   could do would keep the VP in the meeting, and the VP's final status is
 *   decided by its exit.
 */
export function decideDialogAction(
    analysis: DialogAnalysis,
    platform: string,
    actionText: string | null,
    inMeeting: boolean = false,
    meetingOver: boolean = false,
): DialogDecision {
    const escalate = (): DialogDecision => ({
        kind: 'escalate',
        message: manualActionMessage(analysis, platform, actionText),
    });

    if (meetingOver) return { kind: 'ignore' };
    const over = meetingOverReason(analysis);
    if (over) return { kind: 'meeting-over', reason: over };

    if (AUTOCLICK_TYPES.has(analysis.type)) {
        if (isNoAudioAction(actionText)) return escalate();
        const media = isMediaPermissionDialog(analysis, actionText);
        const selector = analysis.primaryActionSelector;
        const readable = !!selector && actionText !== null;
        if (readable && !(media && !actionText)) return { kind: 'click', selector: selector as string };
        if (media) return escalate();
    }
    if (inMeeting && analysis.type === 'OTHER') return { kind: 'ignore' };
    return analysis.needsHuman ? escalate() : { kind: 'ignore' };
}

/**
 * What to do when the watchdog's click on a dialog failed. A camera/microphone
 * dialog goes to a human, because it blocks the join; any other dialog is left
 * alone, as before the click was attempted.
 */
export function decideAfterClickFailure(
    analysis: DialogAnalysis,
    platform: string,
    actionText: string | null,
): DialogDecision {
    return isMediaPermissionDialog(analysis, actionText)
        ? { kind: 'escalate', message: manualActionMessage(analysis, platform, actionText) }
        : { kind: 'ignore' };
}
