/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which of the two Teams joins the Virtual Participant used, and when the
 * Azure Communication Services (ACS) join hands over to the browser join.
 *
 * With an ACS connection string configured and `MEETING_TEAMS_METHOD` left at
 * `auto`, a Teams meeting is joined through ACS (teams-sdk.ts). ACS users are
 * external application users to Teams, which a meeting's tenant controls
 * separately from browser guests, so a meeting the browser join is admitted to
 * can still turn the ACS join away. For the refusals that are the meeting's
 * tenant or meeting type deciding, the VP then joins the same meeting with the
 * browser (teams.ts) instead of ending FAILED. Everything else stays a
 * failure: it is either something the browser join would hit as well (a wrong
 * meeting ID, a declined admission, a meeting that has ended) or a
 * configuration problem an administrator has to see.
 *
 * See docs/teams-acs-sdk.md.
 */
import { details } from './details.js';
import { createLogger } from './logger.js';

const log = createLogger('teams');

/** ACS subCode: anonymous (external) join turned off for the meeting's tenant by policy. */
export const ACS_SUBCODE_ANONYMOUS_JOIN_DISABLED = 5723;
/** ACS subCode: a personal (teams.live.com) meeting, which ACS never joins. */
export const ACS_SUBCODE_PERSONAL_MEETING = 5222;

/**
 * The ACS subCodes after which the browser join is tried in the same meeting,
 * with the plain-words reason used in the log and the JOINING sub-step.
 */
export const ACS_FALLBACK_SUBCODES: Readonly<Record<number, string>> = {
    [ACS_SUBCODE_ANONYMOUS_JOIN_DISABLED]: "the meeting's organisation does not let Azure Communication Services users join",
    [ACS_SUBCODE_PERSONAL_MEETING]: 'Azure Communication Services cannot join personal (teams.live.com) meetings',
};

/** What the ACS join saw when Teams turned it away. */
export interface AcsRefusal {
    /** The callEnded / join-rejection subCode, when Teams gave one. */
    subCode?: number | null;
    /** The call composite page (`__lmaCallState().page`), when that is the signal. */
    page?: string | null;
}

/**
 * Whether the browser join is tried after this ACS refusal.
 *
 * Only by subCode, and only for the ones in ACS_FALLBACK_SUBCODES. The call
 * composite's `accessDeniedTeamsMeeting` page does not fall back: the
 * composite shows it for subCode 5854 alone, which is the organiser or a lobby
 * admitter declining the participant. That is a person's decision about this
 * meeting, not a setting that treats ACS users differently, so asking again
 * from the browser would put the same request in front of the same person.
 * 5751 (invalid meeting identifier) is a wrong meeting ID, which the browser
 * join would fail on too.
 */
export function acsRefusalFallsBack(refusal: AcsRefusal | null | undefined): boolean {
    const subCode = refusal?.subCode;
    if (typeof subCode !== 'number' || !Number.isFinite(subCode)) return false;
    return Object.prototype.hasOwnProperty.call(ACS_FALLBACK_SUBCODES, subCode);
}

/**
 * Whether the ACS join may hand over to the browser join: only when the join
 * method was chosen automatically. `MEETING_TEAMS_METHOD=sdk` asks for ACS and
 * nothing else, so a refusal stays a failure there. Read the same way as
 * resolveJoinMethod in details.ts, which treats every value other than `sdk`
 * and `dom` as `auto`.
 */
export function teamsSdkFallbackAllowed(override: string | undefined): boolean {
    const mode = (override || 'auto').toLowerCase();
    return mode !== 'sdk' && mode !== 'dom';
}

/**
 * Thrown by the ACS join when Teams refused it for a reason in
 * ACS_FALLBACK_SUBCODES. The message keeps the form the join failure has
 * always had ("Teams SDK join failed: …"), so where no fallback runs the
 * failure reads as before.
 */
export class AcsJoinRefusedError extends Error {
    constructor(
        readonly subCode: number,
        reason: string,
    ) {
        super(`Teams SDK join failed: ${reason}`);
        this.name = 'AcsJoinRefusedError';
    }
}

/** The log line written when the browser join takes over. */
export function acsFallbackLogLine(subCode: number): string {
    const why = ACS_FALLBACK_SUBCODES[subCode] ?? 'Teams refused it';
    return `[teams-sdk] Teams refused the ACS join (subCode ${subCode}: ${why}); joining with the browser instead`;
}

/** The JOINING sub-step shown while the browser join takes over. */
export function acsFallbackSubstep(subCode: number): string {
    const why = ACS_FALLBACK_SUBCODES[subCode] ?? 'Teams refused it';
    return `Teams did not accept the Azure Communication Services join (${why}); joining with the browser instead…`;
}

export type TeamsJoinMethod = 'acs' | 'browser';

/**
 * The value stored in the VP row's `joinMethod`: `acs`, `browser`, or
 * `browser:acs-refused:<subCode>` when the browser join took over from a
 * refused ACS join. The UI turns it into words (vp-details.js).
 */
export function teamsJoinMethodValue(method: TeamsJoinMethod, refusedSubCode: number | null): string {
    if (method === 'acs') return 'acs';
    return refusedSubCode === null ? 'browser' : `browser:acs-refused:${refusedSubCode}`;
}

/** The always-level log line written once Teams has admitted the VP. */
export function teamsJoinedLogLine(method: TeamsJoinMethod, refusedSubCode: number | null): string {
    if (method === 'acs') return '[teams] joined with Azure Communication Services';
    return refusedSubCode === null
        ? '[teams] joined with the browser'
        : `[teams] joined with the browser after Teams refused the ACS join (subCode ${refusedSubCode})`;
}

// The ACS refusal the browser join took over from, if one did. Module state:
// one container joins one meeting, and the two handlers that report the join
// are separate objects.
let refusedSubCode: number | null = null;

/** Record that the browser join is taking over from a refused ACS join. */
export function noteAcsRefusal(subCode: number): void {
    refusedSubCode = subCode;
}

/**
 * Report which method joined: one always-level log line, and the VP row's
 * `joinMethod`, which the Virtual Participant page shows for as long as the
 * row exists. Called by teams-sdk.ts and teams.ts once Teams has admitted the
 * VP. Never throws: the VP is in the meeting either way.
 */
export async function reportTeamsJoined(method: TeamsJoinMethod): Promise<void> {
    log.always(teamsJoinedLogLine(method, refusedSubCode));
    const vpId = details.invite.virtualParticipantId;
    if (!vpId || process.env.LOCAL_TEST === 'true') return;
    try {
        const { createStatusManager } = await import('./status-manager.js');
        await createStatusManager(vpId).setJoinMethod(teamsJoinMethodValue(method, refusedSubCode));
    } catch (err) {
        log.warn('[teams] could not record the join method on the VP row (non-fatal):', err);
    }
}
