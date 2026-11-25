/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The LMA roles, as lma-ai-stack/source/appsync/role-matrix.json defines them.
 *
 * A caller's role comes from their Cognito groups: Admin when they include
 * Admin, otherwise Participant when they include Participant, otherwise User.
 * The user pool's pre token generation trigger writes exactly that role into
 * every token's cognito:groups claim; the rule is applied here as well so a
 * token issued before the trigger existed (which carries no group at all for a
 * User) is still read correctly.
 */
export type Role = 'Admin' | 'User' | 'Participant';

/** The cognito:groups claim of a verified token as a list; absent is no groups. */
export const claimedGroups = (payload: Record<string, unknown>): string[] => {
    const groups = payload['cognito:groups'];
    if (Array.isArray(groups)) {
        return groups.map(String);
    }
    return typeof groups === 'string' && groups.length > 0 ? [groups] : [];
};

/** The roles that may stream to the transcriber: the matrix's "capture" capability. */
export const TRANSCRIBER_ROLES: readonly Role[] = ['Admin', 'User'];

export const effectiveRole = (groups: readonly string[]): Role => {
    if (groups.includes('Admin')) {
        return 'Admin';
    }
    if (groups.includes('Participant')) {
        return 'Participant';
    }
    return 'User';
};

/** Whether a caller with these groups may use the transcriber. */
export const mayUseTranscriber = (groups: readonly string[]): boolean =>
    TRANSCRIBER_ROLES.includes(effectiveRole(groups));
