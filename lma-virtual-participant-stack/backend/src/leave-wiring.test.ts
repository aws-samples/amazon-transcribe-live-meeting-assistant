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

test('the Teams watchdog decides the People pane count through peopleCountStep and peopleCountAfterRead', () => {
    const body = source('teams.ts');
    assert.match(body, /this\.peopleCountForWatchdog\(page, !participantsActive\)/);
    assert.match(body, /const step = peopleCountStep\(this\.lastPeopleCount, before, mayOpen\);/);
    assert.match(body, /await this\.readPeopleRoster\(page\);\s*return peopleCountAfterRead\(this\.lastPeopleCount, before, Date\.now\(\)\);/);
});

test('participant activity comes from the participants, never from the voice assistant channel', () => {
    const body = source('scribe.ts');
    assert.match(body, /if \(!isPartial && result\.ChannelId !== 'ch_1'\) noteParticipantActivity\('participant speech'\);/);
    assert.match(body, /!isOwnIdentity\(speaker, this\.ownIdentities\(\)\)\) \{\s*noteParticipantActivity\('active speaker'\);/);
});

test('Teams ends on the hidden hang-up button only after confirming the call UI is gone', () => {
    const body = source('teams.ts');
    assert.match(body, /const hangupHidden: Promise<ExitInfo> = this\.watchHangupHidden\(page, Date\.now\(\) \+ details\.meetingTimeout\);/);
    assert.doesNotMatch(body, /waitForSelector\("#hangup-button", \{ state: 'hidden', timeout: details\.meetingTimeout \}\)\s*\.then/);
    const watch = body.slice(body.indexOf('private async watchHangupHidden('), body.indexOf('public async initialize('));
    assert.match(watch, /await this\.inMeetingSignal\(page\)/);
    assert.match(watch, /decideHangupHidden\(\{ hangupVisible, inMeetingSignal \}, state\)/);
    assert.match(watch, /if \(action === 'end'\) \{[\s\S]{0,300}trigger: 'HANGUP_BUTTON_HIDDEN'/);
});

test("Zoom's participants watchdog decides through decideZoomCounterAction with participant activity", () => {
    const body = source('zoom.ts');
    assert.match(body, /decideZoomCounterAction\(result, counterState, participantsActiveWithin\(\)\)/);
    assert.match(body, /action === 'end-removed'[\s\S]{0,800}trigger: `participants-ui:\$\{result\.state\.toLowerCase\(\)\}`/);
});

test("Zoom's end-dialog check matches only dialogs, by their text", () => {
    const body = source('zoom.ts');
    const watch = body.slice(body.indexOf('const zoomDialogPromise'), body.indexOf("trigger: 'ZOOM_END_DIALOG'"));
    assert.match(watch, /if \(!modal\) continue;/);
    assert.doesNotMatch(watch, /return 'meeting ended'/);
    assert.match(watch, /ZOOM_END_DIALOG_PHRASES,/);
    assert.match(watch, /zoomEndDialogReason\(dialogText\)/);
});

test('Webex reads its end text outside chat and captions, and decides through decideWebexEnd', () => {
    const body = source('webex.ts');
    assert.doesNotMatch(body, /document\.body\?\.textContent\?\.toLowerCase\(\)/);
    assert.match(body, /participantText: \[\.\.\.WEBEX_PARTICIPANT_TEXT_SELECTORS\]/);
    assert.match(body, /controls: \[\.\.\.WEBEX_IN_MEETING_CONTROLS\]/);
    assert.match(body, /decideWebexEnd\(result, endState, participantsActiveWithin\(\)\)/);
});
