/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The wiring that makes both engines honour one switch.
 *
 * `diarizeVirtualParticipant` used to be read only when opening an on-demand
 * engine session, so on a deployment using Amazon Transcribe -- the default --
 * turning it on did nothing at all and nothing said so. Asserted at the source
 * level, like scribe-asr-channels.test.ts, because these modules build AWS
 * clients at import time.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';

const read = (name: string): string =>
    readFileSync(new URL(`./${name}`, import.meta.url).pathname.replace('/dist/', '/src/'), 'utf8');

const scribe = read('scribe.ts');
const kinesis = read('kinesis-stream.ts');

test('the Transcribe path is told whether to separate voices', () => {
    // Read from the same switch the on-demand path passes to its session, so a
    // deployment cannot have it on for one engine and off for the other.
    assert.match(scribe, /setDiarization\(switches\.diarizeVirtualParticipant\)/);
});

test('the switch is read once per meeting, before either engine is chosen', () => {
    const fetchAt = scribe.indexOf('await fetchAsrRuntimeSwitches()');
    const setAt = scribe.indexOf('setDiarization(switches.diarizeVirtualParticipant)');
    const engineAt = scribe.indexOf("resolveVpAsrEngine(switches) === 'microvm'");
    assert.ok(fetchAt > -1 && setAt > -1 && engineAt > -1);
    assert.ok(fetchAt < setAt, 'the switch must be fetched before it is applied');
    assert.ok(
        setAt < engineAt,
        'applied before the engine branch, so the Transcribe fallback is covered too',
    );
});

test('Transcribe is still asked for the labels this now consumes', () => {
    // The request was always there; the labels were thrown away. Removing it
    // would silently turn every diarized VP meeting back into one speaker.
    assert.match(scribe, /ShowSpeakerLabel: true/);
});

test('separation is off unless the deployment asks for it', () => {
    // A deployment that has not turned it on must get exactly today's
    // attribution, so the default has to be false and the setter strict.
    assert.match(kinesis, /private diarize = false;/);
    assert.match(kinesis, /this\.diarize = enabled === true;/);
});

test('the undiarized path is untouched by the branch', () => {
    // The original item loop and its binary-search segment assembly must still
    // be reachable, since that is what every deployment uses today.
    assert.match(kinesis, /if \(this\.diarize\) \{/);
    assert.match(kinesis, /this\.addItemToSegment\(item, segments, timeOffsetSeconds\)/);
});

test('the diarized branch resets the roster timeline on a final result', () => {
    // The undiarized path resets after its item loop; the diarized branch
    // returns early, so it has to do the same or the timeline grows for the
    // whole meeting and segment ids drift.
    const start = kinesis.indexOf('if (this.diarize) {');
    const end = kinesis.indexOf('const alternative = result.Alternatives[0];', start);
    assert.ok(start > -1 && end > start);
    const branch = kinesis.slice(start, end);
    assert.match(branch, /if \(!result\.IsPartial\)/);
    assert.match(branch, /this\.startTimes = \[\];/);
});

test('the roster name comes from the timeline the undiarized path already keeps', () => {
    // One source for "who did the platform say was talking", so a diarized and
    // an undiarized segment name the same participant for the same moment.
    assert.match(kinesis, /private rosterNameAt\(/);
    assert.match(kinesis, /this\.binarySearch\(this\.startTimes, absoluteStart\)/);
});
