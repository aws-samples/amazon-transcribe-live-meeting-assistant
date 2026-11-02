/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * How the meeting consent flow is wired into the Virtual Participant, read from
 * the sources: the parts the flow's own tests (meeting-consent.test.ts) cannot
 * reach, because they drive the flow against fakes.
 *
 * - index.ts decides the consent mode, and with it withholds capture, before
 *   the platform handler joins, and holds the screen recording back when told.
 * - Every platform handler starts the flow before its own "start transcription
 *   if details.start" block, so in an opt-in meeting that block finds capture
 *   withheld, and reads every chat message through the flow first.
 * - The release path restarts transcription, as the "LMA start" command does.
 * - The flow's own defaults are the ones the configuration seeder ships, so the
 *   notice it posts when the API cannot be reached is the deployment's default.
 *
 * Not covered: a live meeting on any platform.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { DEFAULT_CONSENT_SETTINGS, effectiveConsentSettings } from './meeting-consent.js';

const source = (name: string): string =>
    readFileSync(new URL(`./${name}`, import.meta.url).pathname.replace('/dist/', '/src/'), 'utf8');

const HANDLERS = ['chime.ts', 'zoom.ts', 'zoom-sdk.ts', 'teams.ts', 'teams-sdk.ts', 'webex.ts'];

/** The handler's own "start transcription if enabled" block: the last top-level `if (details.start) {`. */
function startBlockIndex(text: string): number {
    const matches = [...text.matchAll(/\n\s+if \(details\.start\) \{\n[^\n]*\n[^\n]*sendMessages\([^)]*details\.startMessages/g)];
    assert.ok(matches.length > 0, 'no start-transcription block found');
    return matches[matches.length - 1].index as number;
}

test('the consent mode is decided, and capture withheld, before the handler joins the meeting', () => {
    const index = source('index.ts');
    const prepared = index.indexOf('await prepareMeetingConsent()');
    const joined = index.indexOf('meeting.initialize(page');
    const video = index.indexOf('await videoRecorder.start(');
    assert.ok(prepared > 0 && joined > 0 && prepared < joined, 'prepareMeetingConsent runs before initialize()');
    assert.ok(prepared < video, 'and before the screen recording starts');
    assert.match(index.slice(prepared, video + 40), /if \(!consentPlan\.deferVideo\) \{/);
});

for (const handler of HANDLERS) {
    test(`${handler} starts the consent flow before its start-transcription block and reads chat through it`, () => {
        const text = source(handler);
        const attached = text.indexOf('attachMeetingConsent(page, {');
        assert.ok(attached > 0, 'attachMeetingConsent is not called');
        assert.ok(attached < startBlockIndex(text), 'attachMeetingConsent comes after the start block');
        assert.match(text, /await meetingConsent\.handleChat\(/, 'chat is not read through the flow');
        assert.match(text, /reason: 'consent-objection', trigger: 'consent-objection'/, 'no way to leave after an objection');
    });
}

test('releasing capture switches details.start on and restarts transcription, as LMA start does', () => {
    const runtime = source('consent-runtime.ts');
    const release = runtime.slice(runtime.indexOf('release: () => {'), runtime.indexOf('startVideo:'));
    assert.match(release, /details\.start = true;/);
    assert.match(release, /transcriptionService\.startTranscription\(\)/);
    const hold = runtime.slice(runtime.indexOf('hold: () => {'), runtime.indexOf('release: () => {'));
    assert.match(hold, /details\.start = false;/);
});

test("the flow's defaults are the ones the configuration seeder ships", () => {
    const shipped = JSON.parse(
        source('../../../lma-ai-stack/source/lambda_functions/configuration_seeder/defaults/MeetingConsent.json'),
    ) as Record<string, unknown>;
    const fromShipped = effectiveConsentSettings(shipped);
    assert.deepEqual(fromShipped, DEFAULT_CONSENT_SETTINGS);
    // And every value was taken from the file, not from the fallback.
    assert.equal(fromShipped.notifyNoticeText, shipped.notifyNoticeText);
    assert.equal(fromShipped.optInNoticeText, shipped.optInNoticeText);
    assert.equal(fromShipped.leaveOnObjection, shipped.leaveOnObjection);
});
