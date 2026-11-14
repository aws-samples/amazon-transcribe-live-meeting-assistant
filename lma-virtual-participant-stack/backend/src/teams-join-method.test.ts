/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Which Teams join the Virtual Participant uses, and when the Azure
 * Communication Services (ACS) join hands over to the browser join.
 *
 * The classification and the wording are pure functions, tested directly. How
 * index.ts, teams-sdk.ts and teams.ts use them is read from the sources: the
 * fallback needs a live meeting whose tenant refuses ACS to run for real.
 *
 * Not covered: a live meeting. The fallback has not been run end to end.
 */
import { strict as assert } from 'node:assert';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import {
    AcsJoinRefusedError,
    acsFallbackLogLine,
    acsFallbackSubstep,
    acsRefusalFallsBack,
    teamsJoinMethodValue,
    teamsJoinedLogLine,
    teamsSdkFallbackAllowed,
} from './teams-join-method.js';
import { resolveJoinMethod } from './details.js';

const source = (name: string): string =>
    readFileSync(new URL(`./${name}`, import.meta.url).pathname.replace('/dist/', '/src/'), 'utf8');

// ---------------------------------------------------------------------------
// Which refusals fall back
// ---------------------------------------------------------------------------

test('a tenant that turns ACS users away (5723) falls back to the browser join', () => {
    assert.equal(acsRefusalFallsBack({ subCode: 5723 }), true);
});

test('a personal teams.live.com meeting (5222) falls back to the browser join', () => {
    assert.equal(acsRefusalFallsBack({ subCode: 5222 }), true);
});

test('refusals the browser join would hit too, or that are configuration, do not fall back', () => {
    // 5751 invalid meeting identifier; 5854 declined from the lobby (the
    // composite's accessDeniedTeamsMeeting); 0 a normal end; 4502 media setup;
    // 5000/5300 removed from the call; 10004 call timeout.
    for (const subCode of [5751, 5854, 0, 4502, 5000, 5300, 10004]) {
        assert.equal(acsRefusalFallsBack({ subCode }), false, String(subCode));
    }
});

test('the access-denied page alone does not fall back', () => {
    assert.equal(acsRefusalFallsBack({ page: 'accessDeniedTeamsMeeting' }), false);
    assert.equal(acsRefusalFallsBack({ page: 'accessDeniedTeamsMeeting', subCode: 5854 }), false);
});

test('no subCode, or one that is not a number, does not fall back', () => {
    assert.equal(acsRefusalFallsBack(null), false);
    assert.equal(acsRefusalFallsBack(undefined), false);
    assert.equal(acsRefusalFallsBack({}), false);
    assert.equal(acsRefusalFallsBack({ subCode: null }), false);
    assert.equal(acsRefusalFallsBack({ subCode: '5723' as unknown as number }), false);
    assert.equal(acsRefusalFallsBack({ subCode: Number.NaN }), false);
});

// ---------------------------------------------------------------------------
// Only when the method was chosen automatically
// ---------------------------------------------------------------------------

test('the fallback is allowed when the join method is auto, and not when ACS is forced', () => {
    assert.equal(teamsSdkFallbackAllowed(undefined), true);
    assert.equal(teamsSdkFallbackAllowed(''), true);
    assert.equal(teamsSdkFallbackAllowed('auto'), true);
    assert.equal(teamsSdkFallbackAllowed('AUTO'), true);
    assert.equal(teamsSdkFallbackAllowed('sdk'), false);
    assert.equal(teamsSdkFallbackAllowed('SDK'), false);
    assert.equal(teamsSdkFallbackAllowed('dom'), false);
});

test('the fallback is allowed for exactly the values resolveJoinMethod treats as auto', () => {
    for (const value of [undefined, '', 'auto', 'Auto', 'something-else', 'sdk', 'dom', 'DOM']) {
        const isAuto = resolveJoinMethod(value, true) === 'sdk' && resolveJoinMethod(value, false) === 'dom';
        assert.equal(teamsSdkFallbackAllowed(value), isAuto, String(value));
    }
});

// ---------------------------------------------------------------------------
// The error and the words
// ---------------------------------------------------------------------------

test('the refusal error keeps the join failure message form and carries the subCode', () => {
    const error = new AcsJoinRefusedError(5723, 'Teams refused it');
    assert.ok(error instanceof Error);
    assert.equal(error.subCode, 5723);
    assert.equal(error.message, 'Teams SDK join failed: Teams refused it');
    // index.ts classifies failure messages by substring; this one must not read
    // as a wrong password or a missing meeting when no fallback runs.
    assert.doesNotMatch(error.message.toLowerCase(), /password|passcode|meeting not found|invalid meeting/);
});

test('the fallback log line names the subCode and says the browser join follows', () => {
    const line = acsFallbackLogLine(5723);
    assert.match(line, /^\[teams-sdk\] Teams refused the ACS join \(subCode 5723/);
    assert.match(line, /; joining with the browser instead$/);
    assert.match(acsFallbackSubstep(5222), /joining with the browser instead…$/);
});

test('the join is reported by method, and says when the browser join took over', () => {
    assert.equal(teamsJoinedLogLine('acs', null), '[teams] joined with Azure Communication Services');
    assert.equal(teamsJoinedLogLine('browser', null), '[teams] joined with the browser');
    assert.equal(
        teamsJoinedLogLine('browser', 5723),
        '[teams] joined with the browser after Teams refused the ACS join (subCode 5723)',
    );
    assert.equal(teamsJoinMethodValue('acs', null), 'acs');
    assert.equal(teamsJoinMethodValue('browser', null), 'browser');
    assert.equal(teamsJoinMethodValue('browser', 5723), 'browser:acs-refused:5723');
});

// ---------------------------------------------------------------------------
// Wiring, read from the sources
// ---------------------------------------------------------------------------

test('index.ts falls back to the browser join on a refused ACS join, with auto only', () => {
    const index = source('index.ts');
    const start = index.indexOf('const joinMeeting = async');
    assert.ok(start > 0, 'joinMeeting was not found');
    const body = index.slice(start, index.indexOf('exitInfo = await Promise.race([joinMeeting(), pageCrashLatch]);', start));
    assert.ok(body.length > 0, 'joinMeeting is not what the crash latch races');
    // The three conditions, all of which must hold, before anything else.
    const guard = body.indexOf('throw error;');
    const guardText = body.slice(0, guard);
    assert.match(guardText, /!\(meeting instanceof TeamsSdk\)/);
    assert.match(guardText, /!\(error instanceof AcsJoinRefusedError\)/);
    assert.match(guardText, /!teamsSdkFallbackAllowed\(process\.env\.MEETING_TEAMS_METHOD\)/);
    // Then the switch to the browser join, in this order.
    const after = body.slice(guard);
    const order = [
        'log.always(acsFallbackLogLine(error.subCode))',
        'noteAcsRefusal(error.subCode)',
        'setJoiningSubstep(acsFallbackSubstep(error.subCode))',
        "details.teamsMethod = 'dom'",
        'meeting = new Teams()',
        'return meeting.initialize(page, { prepareAvatar })',
    ].map((needle) => {
        const at = after.indexOf(needle);
        assert.ok(at > 0, `${needle} is missing from the fallback`);
        return at;
    });
    assert.deepEqual([...order].sort((a, b) => a - b), order, 'the fallback steps are out of order');
});

test('the screen-share probe is chosen at each sample, so it follows a fallback', () => {
    const index = source('index.ts');
    const start = index.indexOf('keyframeSampler.start({');
    const block = index.slice(start, index.indexOf('});', start));
    assert.match(block, /shareActive: hasShareProbe\s*\? \(\) => \{\s*const probe = screenShareProbeFor\(sharePlatform, details\.teamsMethod\)/);
});

test('the ACS join throws the refusal error for a fallback subCode, from both signals', () => {
    const sdk = source('teams-sdk.ts');
    assert.match(sdk, /acsRefusalFallsBack\(\{ subCode: state\.error\.subCode \}\)\) \{\s*throw new AcsJoinRefusedError/);
    assert.match(sdk, /acsRefusalFallsBack\(\{ subCode: state\.endReason\.subCode \}\)\) \{[\s\S]{0,120}throw new AcsJoinRefusedError/);
    // The page carries the subCode with the refusals it recognises in a 403.
    const page = source('teams-sdk-page.ts');
    assert.match(page, /subCode: 5723,/);
    assert.match(page, /subCode: 5222,/);
    assert.match(page, /w\.__lmaJoinError = \{ reason: match\.reason, detail: body\.slice\(0, 400\), subCode: match\.subCode \}/);
});

test('the ACS join leaves the shared page clean for the browser join', () => {
    const sdk = source('teams-sdk.ts');
    // Its listeners come off in the finally, after leaving the call.
    const finallyAt = sdk.lastIndexOf('} finally {');
    const cleanup = sdk.slice(finallyAt);
    assert.match(cleanup, /await this\.leaveCall\(page\);\s*for \(const \[event, handler\] of listeners\) page\.off/);
    assert.match(cleanup, /this\.server\?\.close\(\)/);
    assert.match(cleanup, /this\.identityClient\.deleteUser\(this\.acsUser\)/);
    // No bare page.on left in initialize, which would outlive the join.
    const init = sdk.slice(sdk.indexOf('public async initialize'), finallyAt);
    assert.equal([...init.matchAll(/\bpage\.on\(/g)].length, 1, 'only listen() calls page.on');
    assert.match(init, /page\.on\(event as any, handler\);\s*listeners\.push\(\[event, handler\]\);/);
    // The ACS configuration is only handed to the embed server's own origin.
    assert.match(sdk, /if \(window\.location\.origin !== cfg\.origin\) return;/);
});

test('both Teams joins report which method joined, once admitted', () => {
    const sdk = source('teams-sdk.ts');
    assert.match(sdk, /log\.info\('\[teams-sdk\] joined meeting'\);\s*await reportTeamsJoined\('acs'\);/);
    const teams = source('teams.ts');
    const joined = teams.indexOf('await statusManager.setJoined();');
    const reported = teams.indexOf("await reportTeamsJoined('browser');");
    assert.ok(joined > 0 && reported > joined, 'teams.ts reports the browser join after setJoined');
});
