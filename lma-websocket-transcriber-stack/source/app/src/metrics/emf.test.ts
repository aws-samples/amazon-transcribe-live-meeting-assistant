// Copyright (c) 2025 Amazon.com
// This file is licensed under the MIT License.
// See the LICENSE file in the project root for full license information.

/**
 * Tests for the Embedded Metric Format records the transcriber writes, and for
 * the per-meeting state that decides when one is written.
 *
 * The record is asserted directly, because the record is the contract:
 * CloudWatch Logs parses it at ingestion and, when it does not match, publishes
 * nothing and reports nothing. The only symptom is an empty widget, which looks
 * exactly like a quiet deployment.
 *
 * Every constraint below is from the embedded metric format specification. The
 * `Dimensions` one is the subtle one: the outer array must hold at least one
 * DimensionSet and a DimensionSet may be empty, so a metric with no dimensions
 * is `[[]]` and `[]` publishes nothing.
 *
 * The session tests cover the part that cannot be got right anywhere else --
 * recognising a meeting's *first* transcript segment, and distinguishing a
 * meeting that produced none from somebody who changed their mind.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import * as catalogue from './catalogue';
import { MetricEmitter } from './emf';
import {
    MINIMUM_SESSION_MS_FOR_SILENCE_TO_BE_A_FAULT,
    noteStreamingSessionEnded,
    noteStreamingSessionStarted,
    noteTranscriptSegment,
    sawTranscriptSegment,
} from './session';
import { CallMetaData } from '../calleventdata/eventtypes';

const NAMESPACE = 'lma-test-stack';

/** Collects the lines an emitter writes, with the errors it reported. */
class Recorder {
    lines: string[] = [];

    errors: string[] = [];

    readonly sink = (line: string): void => {
        this.lines.push(line);
    };

    readonly log = {
        error: (message: string): void => {
            this.errors.push(message);
        },
    };

    get records(): Array<Record<string, unknown>> {
        return this.lines.map((line) => JSON.parse(line) as Record<string, unknown>);
    }

    get only(): Record<string, unknown> {
        assert.equal(this.lines.length, 1, `expected one record, got ${this.lines.length}`);
        return this.records[0]!;
    }
}

const newEmitter = (
    recorder: Recorder,
    namespace = NAMESPACE,
    nowMs = 1_700_000_000_123
): MetricEmitter =>
    new MetricEmitter(namespace, {
        sink: recorder.sink,
        log: recorder.log,
        clock: () => nowMs,
    });

/* eslint-disable @typescript-eslint/no-explicit-any */
const directive = (record: Record<string, unknown>): any =>
    (record['_aws'] as any).CloudWatchMetrics[0];
/* eslint-enable @typescript-eslint/no-explicit-any */

/** A meeting's metadata, carrying only what these tests need. */
const newMeeting = (callId = 'call-1'): CallMetaData =>
    ({
        callId,
        samplingRate: 8000,
        callEvent: 'START',
        activeSpeaker: '',
    }) as CallMetaData;

// ── the record's shape ──────────────────────────────────────────────────────

test('a record is one line of parseable JSON', () => {
    const recorder = new Recorder();
    newEmitter(recorder).count(catalogue.STREAMING_SESSIONS_STARTED);

    assert.equal(recorder.lines.length, 1);
    assert.ok(!recorder.lines[0]!.includes('\n'));
    assert.ok(JSON.parse(recorder.lines[0]!));
});

test('the record carries the members CloudWatch requires', () => {
    const recorder = new Recorder();
    newEmitter(recorder).count(catalogue.STREAMING_SESSIONS_STARTED);

    const record = recorder.only;
    const metadata = record['_aws'] as Record<string, unknown>;
    assert.deepEqual(Object.keys(metadata).sort(), ['CloudWatchMetrics', 'Timestamp']);
    for (const member of ['Namespace', 'Dimensions', 'Metrics']) {
        assert.ok(member in directive(record), `the directive has no ${member}`);
    }
});

test('the timestamp is an integer of milliseconds since the epoch', () => {
    const recorder = new Recorder();
    // A fractional reading is the case that would produce a float if the
    // conversion were missing; the format rejects a float and drops the record.
    newEmitter(recorder, NAMESPACE, 1_700_000_000_123.7).count(
        catalogue.STREAMING_SESSIONS_STARTED
    );

    const timestamp = (recorder.only['_aws'] as Record<string, unknown>)['Timestamp'];
    assert.equal(typeof timestamp, 'number');
    assert.ok(Number.isInteger(timestamp));
    assert.equal(timestamp, 1_700_000_000_124);
});

test('the namespace is the one the emitter was given', () => {
    const recorder = new Recorder();
    newEmitter(recorder, 'some-other-stack').count(catalogue.STREAMING_SESSIONS_STARTED);

    assert.equal(directive(recorder.only).Namespace, 'some-other-stack');
});

test('a metric with no dimensions is one empty dimension set, not an empty array', () => {
    const recorder = new Recorder();
    newEmitter(recorder).count(catalogue.STREAMING_SESSIONS_STARTED);

    assert.deepEqual(directive(recorder.only).Dimensions, [[]]);
});

test('the named metric has a numeric value on the root node', () => {
    const recorder = new Recorder();
    newEmitter(recorder).measure(catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS, 1500);

    const record = recorder.only;
    assert.equal(directive(record).Metrics[0].Name, catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS);
    assert.equal(record[catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS], 1500);
});

test('each metric carries the unit the catalogue gives it', () => {
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    emitter.count(catalogue.STREAMING_SESSIONS_STARTED);
    emitter.measure(catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS, 1);

    const units = recorder.records.map((record) => directive(record).Metrics[0].Unit);
    assert.deepEqual(units, ['Count', 'Milliseconds']);
});

test('a count defaults to one', () => {
    const recorder = new Recorder();
    newEmitter(recorder).count(catalogue.STREAMING_SESSIONS_STARTED);

    assert.equal(recorder.only[catalogue.STREAMING_SESSIONS_STARTED], 1);
});

// ── refusing what would publish a series nobody reads ───────────────────────

test('a name outside the catalogue is not emitted, and is reported', () => {
    const recorder = new Recorder();
    newEmitter(recorder).count('StreamingSessionsStartedd');

    assert.deepEqual(recorder.lines, []);
    assert.ok(recorder.errors.join(' ').includes('StreamingSessionsStartedd'));
});

test('a non-finite value is not emitted', () => {
    // NaN and Infinity both serialise to `null`, which makes the metric target
    // non-numeric and drops the whole record.
    for (const value of [NaN, Infinity, -Infinity]) {
        const recorder = new Recorder();
        newEmitter(recorder).measure(catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS, value);
        assert.deepEqual(recorder.lines, [], `${value} was emitted`);
        assert.equal(recorder.errors.length, 1);
    }
});

// ── the misconfiguration this is all built to prevent ───────────────────────

test('an emitter with no namespace publishes nothing and says so once', () => {
    const recorder = new Recorder();
    const emitter = newEmitter(recorder, '');
    assert.equal(emitter.enabled, false);

    for (let index = 0; index < 20; index += 1) {
        emitter.count(catalogue.STREAMING_SESSIONS_STARTED);
    }

    assert.deepEqual(recorder.lines, []);
    assert.equal(recorder.errors.length, 1, 'reported per metric instead of once');
    assert.ok(recorder.errors[0]!.includes(catalogue.METRIC_NAMESPACE_ENV_VAR));
});

test('a blank namespace is treated as unset', () => {
    // `!Ref` of a parameter left empty renders as an empty string, and a
    // whitespace-only namespace is not one CloudWatch accepts.
    for (const namespace of ['', '   ']) {
        assert.equal(new MetricEmitter(namespace).enabled, false);
    }
});

test('a failing sink does not throw and the record is not retried', () => {
    let calls = 0;
    const errors: string[] = [];
    const emitter = new MetricEmitter(NAMESPACE, {
        sink: () => {
            calls += 1;
            throw new Error('stdout is gone');
        },
        log: { error: (message) => errors.push(message) },
    });

    // Measuring a meeting must never be able to interrupt it.
    emitter.count(catalogue.STREAMING_SESSIONS_STARTED);
    assert.equal(calls, 1);
    assert.equal(errors.length, 1);
});

// ── recognising a meeting's first transcript segment ────────────────────────

test('a session opening is counted once', () => {
    const recorder = new Recorder();
    noteStreamingSessionStarted(newMeeting(), newEmitter(recorder), 1000);

    assert.equal(recorder.only[catalogue.STREAMING_SESSIONS_STARTED], 1);
});

test('the first segment reports the time since the session opened', () => {
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    const meeting = newMeeting();

    noteStreamingSessionStarted(meeting, emitter, 1_000);
    recorder.lines = [];
    noteTranscriptSegment(meeting, emitter, 4_500);

    assert.equal(recorder.only[catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS], 3_500);
});

test('only the first segment of a meeting is measured', () => {
    // Called for every segment of the meeting, from the one writer both engines
    // share, so emitting more than once would turn a per-meeting latency into
    // a per-utterance one and make the distribution meaningless.
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    const meeting = newMeeting();

    noteStreamingSessionStarted(meeting, emitter, 1_000);
    recorder.lines = [];
    noteTranscriptSegment(meeting, emitter, 2_000);
    noteTranscriptSegment(meeting, emitter, 3_000);
    noteTranscriptSegment(meeting, emitter, 90_000);

    assert.equal(recorder.lines.length, 1);
    assert.equal(recorder.only[catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS], 1_000);
});

test('two meetings are measured independently', () => {
    // One task carries many meetings at once, so state shared between them
    // would make the second meeting look instantaneous.
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    const first = newMeeting('call-1');
    const second = newMeeting('call-2');

    noteStreamingSessionStarted(first, emitter, 1_000);
    noteStreamingSessionStarted(second, emitter, 5_000);
    recorder.lines = [];
    noteTranscriptSegment(first, emitter, 2_000);
    noteTranscriptSegment(second, emitter, 9_000);

    const measured = recorder.records.map(
        (record) => record[catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS]
    );
    assert.deepEqual(measured, [1_000, 4_000]);
});

test('a segment for a meeting that never started is not measured', () => {
    const recorder = new Recorder();
    noteTranscriptSegment(newMeeting(), newEmitter(recorder), 2_000);

    assert.deepEqual(recorder.lines, []);
});

test('a negative duration is clamped rather than published', () => {
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    const meeting = newMeeting();

    noteStreamingSessionStarted(meeting, emitter, 5_000);
    recorder.lines = [];
    noteTranscriptSegment(meeting, emitter, 4_000);

    assert.equal(recorder.only[catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS], 0);
});

// ── a meeting that produced nothing ─────────────────────────────────────────

test('a session that transcribed nothing for long enough is counted', () => {
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    const meeting = newMeeting();

    noteStreamingSessionStarted(meeting, emitter, 0);
    recorder.lines = [];
    noteStreamingSessionEnded(meeting, emitter, MINIMUM_SESSION_MS_FOR_SILENCE_TO_BE_A_FAULT);

    assert.equal(recorder.only[catalogue.STREAMING_SESSIONS_WITHOUT_TRANSCRIPT], 1);
});

test('a session that did transcribe is not counted as having produced nothing', () => {
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    const meeting = newMeeting();

    noteStreamingSessionStarted(meeting, emitter, 0);
    noteTranscriptSegment(meeting, emitter, 1_000);
    assert.equal(sawTranscriptSegment(meeting), true);
    recorder.lines = [];
    noteStreamingSessionEnded(meeting, emitter, 600_000);

    assert.deepEqual(recorder.lines, []);
});

test('a session abandoned before the floor is not counted', () => {
    // Somebody who started streaming and changed their mind. Counting it would
    // make the alarm on this metric fire on ordinary use.
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    const meeting = newMeeting();

    noteStreamingSessionStarted(meeting, emitter, 0);
    recorder.lines = [];
    noteStreamingSessionEnded(meeting, emitter, MINIMUM_SESSION_MS_FOR_SILENCE_TO_BE_A_FAULT - 1);

    assert.deepEqual(recorder.lines, []);
});

test('ending the same meeting twice counts it once', () => {
    // The teardown path is guarded against running twice, but the metric must
    // not depend on that guard being the only caller.
    const recorder = new Recorder();
    const emitter = newEmitter(recorder);
    const meeting = newMeeting();

    noteStreamingSessionStarted(meeting, emitter, 0);
    recorder.lines = [];
    noteStreamingSessionEnded(meeting, emitter, 600_000);
    noteStreamingSessionEnded(meeting, emitter, 600_000);

    assert.equal(recorder.lines.length, 1);
});

test('ending a meeting that never started emits nothing', () => {
    const recorder = new Recorder();
    noteStreamingSessionEnded(newMeeting(), newEmitter(recorder), 600_000);

    assert.deepEqual(recorder.lines, []);
});

// ── the catalogue ───────────────────────────────────────────────────────────

test('the catalogue is not empty and its names are unique', () => {
    assert.ok(catalogue.CATALOGUE.length > 0);
    const names = catalogue.CATALOGUE.map((metric) => metric.name);
    assert.equal(new Set(names).size, names.length);
});

test('a millisecond metric is named so a reader knows the unit', () => {
    // The unit is not shown on an alarm notification or in a metric-math
    // expression, so a duration whose name omits it gets compared against a
    // threshold in whatever unit the reader assumed.
    for (const metric of catalogue.CATALOGUE) {
        assert.equal(
            metric.name.endsWith('Ms'),
            metric.unit === 'Milliseconds',
            `${metric.name} and ${metric.unit} disagree`
        );
    }
});

test('the namespace variable is the name the templates set', () => {
    // Spelled out rather than referenced, so renaming it here without renaming
    // it in the task definition fails this test.
    assert.equal(catalogue.METRIC_NAMESPACE_ENV_VAR, 'METRIC_NAMESPACE');
});
