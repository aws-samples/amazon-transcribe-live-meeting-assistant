// Copyright (c) 2025 Amazon.com
// This file is licensed under the MIT License.
// See the LICENSE file in the project root for full license information.

/**
 * Per-meeting metric state, and the three things worth counting about it.
 *
 * Why this lives in the transcriber rather than in the Call Event Processor,
 * which is where the rest of the meeting-lifecycle counts come from: the
 * processor cannot tell a meeting's first transcript segment from its
 * hundredth. It handles one Kinesis record at a time, ten shards in parallel,
 * and the meeting record it reads carries no "transcribing yet?" state -- a
 * meeting's `Status` goes `STARTED` straight to `ENDED` and never passes
 * through `TRANSCRIBING`, so every segment of every live meeting looks equally
 * like the first. Answering it there would mean a new conditional write per
 * segment.
 *
 * Here it is free. This service holds one `CallMetaData` per meeting for the
 * meeting's whole duration, and both transcription engines pass that same
 * object into the single function that writes a segment to Kinesis.
 *
 * The state hangs off a `WeakMap` keyed on that object rather than off a new
 * field on it. Two reasons: `index.ts` builds `callMetadata` from an explicit
 * allowlist of client-supplied fields, so a field added there is both easy to
 * drop by accident and something a client could try to set; and a `WeakMap`
 * entry is reclaimed when the meeting's metadata is, so there is no teardown
 * step that can be forgotten and nothing left behind by a socket that never
 * closed cleanly.
 */

import { CallMetaData } from '../calleventdata/eventtypes';
import * as catalogue from './catalogue';
import { METRICS, MetricEmitter } from './emf';

/**
 * How long a session must run before producing no transcript counts as a fault.
 *
 * Below this, a session with no transcript is overwhelmingly somebody who
 * started streaming and changed their mind, which is not worth an operator's
 * attention and would make the alarm on this metric useless. Thirty seconds is
 * long enough to cover that and short enough that a meeting which genuinely
 * failed to transcribe is still counted.
 */
export const MINIMUM_SESSION_MS_FOR_SILENCE_TO_BE_A_FAULT = 30_000;

interface SessionMetricState {
    /**
     * When the meeting's audio session opened.
     *
     * Deliberately not the session's `startStreamTime`, which looks like the
     * same thing and is not: the Amazon Transcribe path resets that on every
     * stream reconnect, so a meeting that reconnected would report its time to
     * first segment as the time since the reconnect.
     */
    readonly openedAtMs: number;
    sawTranscriptSegment: boolean;
}

const stateByMeeting = new WeakMap<CallMetaData, SessionMetricState>();

/** Count a meeting's audio session opening, and start its clock. */
export const noteStreamingSessionStarted = (
    meeting: CallMetaData,
    emitter: MetricEmitter = METRICS,
    nowMs: number = Date.now()
): void => {
    stateByMeeting.set(meeting, { openedAtMs: nowMs, sawTranscriptSegment: false });
    emitter.count(catalogue.STREAMING_SESSIONS_STARTED);
};

/**
 * Record that a transcript segment reached Kinesis for this meeting.
 *
 * Called for every segment, from the one writer both engines share, and emits
 * on the first one only. Safe to call for a meeting that was never registered:
 * a connection producing segments without a START frame has no clock to
 * measure against, so there is nothing to report.
 */
export const noteTranscriptSegment = (
    meeting: CallMetaData,
    emitter: MetricEmitter = METRICS,
    nowMs: number = Date.now()
): void => {
    const state = stateByMeeting.get(meeting);
    if (state === undefined || state.sawTranscriptSegment) {
        return;
    }
    state.sawTranscriptSegment = true;
    // Clamped at zero rather than trusted: the two readings are taken at
    // different call sites, and a negative duration would be published as one.
    emitter.measure(
        catalogue.TIME_TO_FIRST_TRANSCRIPT_SEGMENT_MS,
        Math.max(0, nowMs - state.openedAtMs)
    );
};

/**
 * Count a meeting that produced nothing, if that is what happened.
 *
 * Emits nothing in the ordinary case, so the metric counts faults rather than
 * endings -- `MeetingsEnded`, from the Call Event Processor, counts endings.
 * Called from the single teardown path, inside the guard that already stops it
 * running twice for one meeting.
 */
export const noteStreamingSessionEnded = (
    meeting: CallMetaData,
    emitter: MetricEmitter = METRICS,
    nowMs: number = Date.now()
): void => {
    const state = stateByMeeting.get(meeting);
    if (state === undefined) {
        return;
    }
    stateByMeeting.delete(meeting);
    if (state.sawTranscriptSegment) {
        return;
    }
    if (nowMs - state.openedAtMs < MINIMUM_SESSION_MS_FOR_SILENCE_TO_BE_A_FAULT) {
        return;
    }
    emitter.count(catalogue.STREAMING_SESSIONS_WITHOUT_TRANSCRIPT);
};

/** Whether this meeting has produced a transcript segment. For tests. */
export const sawTranscriptSegment = (meeting: CallMetaData): boolean =>
    stateByMeeting.get(meeting)?.sawTranscriptSegment ?? false;
