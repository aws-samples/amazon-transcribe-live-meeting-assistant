/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * A Zoom personal meeting link given as the meeting ID is kept for the Zoom
 * handlers to resolve, and its pwd value is a secret from the start, so the
 * "Meeting ID:" line and the navigation lines never show it. `details` is
 * built once at import from the environment, so each case runs in a child
 * process of its own.
 */
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const TOKEN = 'Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr9St0.1';
const PERSONAL = `https://acme.zoom.us/my/someone?pwd=${TOKEN}`;

/** The invite `details` builds from these variables, and what the logger makes of the link. */
function inviteFor(env: Record<string, string>): {
    invite: Record<string, unknown>;
    logged: string;
} {
    const script = `
        const { details } = await import('./details.js');
        const { redactText } = await import('./logger.js');
        process.stdout.write('@@' + JSON.stringify({
            invite: details.invite,
            logged: redactText('Meeting ID: ' + process.env.MEETING_ID),
        }));
    `;
    const out = execFileSync(process.execPath, ['--input-type=module', '-e', script], {
        cwd: fileURLToPath(new URL('.', import.meta.url)),
        env: { PATH: process.env.PATH ?? '', LOG_LEVEL: 'error', ...env },
        encoding: 'utf8',
    });
    // Lines the modules write at import come first.
    return JSON.parse(out.slice(out.lastIndexOf('@@') + 2));
}

test('a personal meeting link is kept for resolution, which also picks its password', () => {
    const { invite, logged } = inviteFor({ MEETING_PLATFORM: 'ZOOM', MEETING_ID: PERSONAL });
    assert.equal(invite.meetingId, PERSONAL);
    // resolveZoomPersonalLink chooses the password, from the link or the redirect.
    assert.equal(invite.meetingPassword, undefined);
    assert.deepEqual(invite.zoomPersonalLink, { url: PERSONAL, host: 'acme.zoom.us', name: 'someone', pwd: TOKEN });
    assert.equal(logged.includes(TOKEN), false, logged);
});

test('a separately given passcode is kept, and the link pwd value is still a secret', () => {
    const { invite, logged } = inviteFor({ MEETING_PLATFORM: 'ZOOM', MEETING_ID: PERSONAL, MEETING_PASSWORD: '0206 1967' });
    assert.equal(invite.meetingPassword, '02061967');
    assert.equal(logged.includes(TOKEN), false, logged);
});

test('a numbered link and a meeting number have no personal link to resolve', () => {
    const numbered = inviteFor({ MEETING_PLATFORM: 'ZOOM', MEETING_ID: `https://acme.zoom.us/j/81234567890?pwd=${TOKEN}` });
    assert.equal(numbered.invite.meetingId, '81234567890');
    assert.equal('zoomPersonalLink' in numbered.invite, false);
    const plain = inviteFor({ MEETING_PLATFORM: 'ZOOM', MEETING_ID: '81234567890' });
    assert.equal('zoomPersonalLink' in plain.invite, false);
});

test('a personal link on another platform is not read as one', () => {
    const { invite } = inviteFor({ MEETING_PLATFORM: 'TEAMS', MEETING_ID: PERSONAL });
    assert.equal('zoomPersonalLink' in invite, false);
});
