/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How each platform handler is wired to the leave rules of #1148, read from
 * its source: the handlers drive a real browser page, so these lines cannot be
 * reached from a unit test.
 */

import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const source = (file: string): string => readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8');

test('Zoom and Chime leave as alone only through the debounce, never on one reading', () => {
    for (const file of ['zoom.ts', 'chime.ts']) {
        const body = source(file);
        const observer = body.slice(body.indexOf("exposeFunction('attendeeChange'"), body.indexOf("exposeFunction('attendeeChange'") + 500);
        assert.match(observer, /aloneDebounce\.note\(number\)/, `${file}: the attendee observer feeds the debounce`);
        assert.doesNotMatch(observer, /requestEnd\(/, `${file}: the attendee observer does not end the meeting itself`);
        assert.match(body, /new AloneDebounce\(\(\) => \{[\s\S]{0,300}reason: 'alone-in-meeting', trigger: 'attendees-left'/);
    }
});

test("Chime's end check needs a run of failures, not one", () => {
    const body = source('chime.ts');
    assert.match(body, /export const CHIME_POLL_ERRORS_BEFORE_END = 15;/);
    assert.match(body, /pollErrors \+= 1;\s*if \(pollErrors < CHIME_POLL_ERRORS_BEFORE_END && !page\.isClosed\(\)\) return;/);
});

test('Teams and Meet pass participant activity to the shared watchdog', () => {
    assert.match(source('teams.ts'), /decideAttendeeAction\(reading, watchdogState, watchdogConfig, participantsActive\)/);
    assert.match(source('google-meet.ts'), /decideAttendeeAction\(reading, watchdogState, watchdogConfig, participantsActiveWithin\(\)\)/);
});

test('the Teams watchdog opens the People pane only in a silent meeting with no fresh count', () => {
    const body = source('teams.ts');
    assert.match(body, /this\.peopleCountForWatchdog\(page, !participantsActiveWithin\(\)\)/);
    assert.match(body, /if \(fresh\(\) !== null \|\| !mayOpen\) return fresh\(\);\s*await this\.readPeopleRoster\(page\);/);
});

test('participant activity comes from the participants, never from the voice assistant channel', () => {
    const body = source('scribe.ts');
    assert.match(body, /if \(!isPartial && result\.ChannelId !== 'ch_1'\) noteParticipantActivity\('participant speech'\);/);
    assert.match(body, /!isOwnIdentity\(speaker, this\.ownIdentities\(\)\)\) \{\s*noteParticipantActivity\('active speaker'\);/);
});
