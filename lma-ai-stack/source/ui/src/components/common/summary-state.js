/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The small state object stored alongside a meeting's summary text.
 *
 * It answers two questions the summary text cannot: where the text came from,
 * and whether a regeneration is in flight. Both matter to the meeting page --
 * the first because replacing somebody's own words deserves a confirmation, the
 * second because regenerating calls a model once per prompt template and so
 * takes long enough that the page has to say something while it happens.
 *
 * Kept as one JSON string on the Call rather than as several fields, for the
 * same reason SpeakerIdentities is: one attribute to add, one to subscribe to,
 * and room to record more about a run later without another schema change.
 *
 * Every reader goes through parseSummaryState, so a meeting with no state at all
 * -- which is every meeting summarised before this existed, and every meeting
 * in a deployment with summaries switched off -- reads as a generated summary
 * with nothing running rather than as an error.
 *
 * Two kinds of run write it. A regeneration, started from the Regenerate
 * button, writes no `trigger`. The end-of-meeting summary, which the Kinesis
 * pipeline queues when a meeting ends, writes `trigger: 'meeting-end'`: the
 * ENDED status write marks it running, and the summary write marks it idle or
 * failed. The meeting page words the two differently -- "Summary in progress"
 * against "Regenerating" -- and waits a shorter time for the second.
 */

export const SOURCE_GENERATED = 'generated';
export const SOURCE_MANUAL = 'manual';
export const STATUS_RUNNING = 'running';
export const STATUS_FAILED = 'failed';
export const STATUS_IDLE = 'idle';
export const TRIGGER_MEETING_END = 'meeting-end';

/**
 * How long the end-of-meeting summary is waited for before the page stops
 * saying it is on its way and offers Regenerate instead.
 *
 * The summary of a long meeting takes a few minutes; this is comfortably past
 * that, and catches the run whose outcome was never written -- a summary
 * function killed mid-run, or an event lost on the way -- which would otherwise
 * leave the meeting showing "Summary in progress" for good. Must match
 * MEETING_END_PATIENCE_SECONDS in the regenerator Lambda, which accepts a
 * regeneration in place of the run once this has passed.
 */
export const MEETING_END_PATIENCE_MS = 600 * 1000;

/**
 * How long a run may be marked as in flight before the mark is ignored.
 *
 * Must match SUMMARY_REGENERATION_STALE_SECONDS in the regenerator Lambda: the
 * Lambda uses it to decide whether to accept a new run, and this uses it to
 * decide whether to offer one, so a disagreement would either show an enabled
 * button that is refused or a disabled one that would have been accepted.
 */
export const STALE_RUN_MS = 1200 * 1000;

/**
 * How far ahead of now a run's start stamp may be and still be believed.
 *
 * The stamp is written by the regenerator Lambda from its clock and read here
 * from the browser's, so a small disagreement is ordinary. Anything further
 * ahead is not a clock, and is not believed: a stamp dated hours from now would
 * otherwise show this meeting as regenerating, and disable both controls, until
 * that hour arrived. Must match CLOCK_SKEW_ALLOWANCE_SECONDS in the Lambda.
 */
export const CLOCK_SKEW_ALLOWANCE_MS = 60 * 1000;

export const parseSummaryState = (raw) => {
  if (!raw) {
    return {};
  }
  if (typeof raw === 'object') {
    return raw;
  }
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (e) {
    return {};
  }
};

export const serializeSummaryState = (state) => JSON.stringify(state || {});

/**
 * True when a regeneration started recently enough to still be running.
 *
 * Only a stamp inside the window [now - STALE_RUN_MS, now + skew allowance]
 * counts. The staleness cut-off is what stops a run whose Lambda was killed
 * from disabling the controls for ever; the forward allowance, and treating an
 * unreadable stamp as finished, do the same for a stamp this cannot ever read
 * its way out of.
 */
/** True when the state belongs to the end-of-meeting summary rather than a regeneration. */
export const isMeetingEndRun = (state) => state?.trigger === TRIGGER_MEETING_END;

/**
 * When a running mark was stamped, or null when it is not a mark this can
 * believe.
 *
 * `updateCallSummaryText` stores whatever state string it is given, so the
 * shape of this field is not something a reader can take for granted: a mark
 * with no usable stamp, or one dated further ahead than two clocks disagree by,
 * reads as finished rather than holding this meeting's controls with nothing
 * able to clear it.
 */
const runningSince = (state, now) => {
  if (state?.status !== STATUS_RUNNING) {
    return null;
  }
  const started = new Date(state.startedAt ?? '').getTime();
  if (Number.isNaN(started) || started > now + CLOCK_SKEW_ALLOWANCE_MS) {
    return null;
  }
  return started;
};

export const isRegenerating = (state, now = Date.now()) => {
  if (isMeetingEndRun(state)) {
    return false;
  }
  const started = runningSince(state, now);
  return started !== null && started > now - STALE_RUN_MS;
};

/**
 * True while the end-of-meeting summary is on its way: queued when the meeting
 * ended, not yet written, and not yet waited for longer than
 * MEETING_END_PATIENCE_MS.
 *
 * Only ever true for a meeting the pipeline marked. With summaries switched off
 * nothing marks it, so such a meeting goes straight to "Not available" rather
 * than promising a summary that is not coming.
 */
export const isSummaryInProgress = (state, now = Date.now()) => {
  if (!isMeetingEndRun(state)) {
    return false;
  }
  const started = runningSince(state, now);
  return started !== null && started > now - MEETING_END_PATIENCE_MS;
};

/**
 * True when the end-of-meeting summary was queued but has been waited for
 * longer than MEETING_END_PATIENCE_MS with no outcome: the page stops showing
 * progress and offers Regenerate.
 */
export const isSummaryOverdue = (state, now = Date.now()) => {
  if (!isMeetingEndRun(state)) {
    return false;
  }
  const started = runningSince(state, now);
  return started !== null && started <= now - MEETING_END_PATIENCE_MS;
};

/**
 * How long until an end-of-meeting summary in progress becomes overdue, or
 * null when it is not in progress. What the page sets its timer by, so the
 * fallback appears without anything else having to change on the meeting.
 */
export const msUntilSummaryOverdue = (state, now = Date.now()) => {
  if (!isSummaryInProgress(state, now)) {
    return null;
  }
  return runningSince(state, now) + MEETING_END_PATIENCE_MS - now;
};

/** True when the stored text is something a person wrote rather than generated. */
export const isHandEdited = (state) => state?.source === SOURCE_MANUAL;

/**
 * The failure to show, or null.
 *
 * Suppressed once a run is going again, so pressing the button a second time
 * does not leave the previous failure sitting next to the new spinner.
 *
 * And suppressed once it is old. A failure describes a run just attempted --
 * the message the Lambda writes says "just now" -- but nothing dismisses the
 * stored state, so a meeting whose last regeneration failed would show that
 * error to every later visitor for as long as the meeting existed. It is shown
 * over the same window a run may be in flight for, measured from the `updatedAt`
 * the Lambda writes beside it, and a failure that cannot be dated is not shown:
 * an undateable one would never age out, which is the case being fixed.
 *
 * A failed end-of-meeting summary is the exception, and is shown for as long
 * as it is the meeting's state. It is not a press somebody made a moment ago
 * but the reason the meeting has no summary, and it stays true until a
 * regeneration replaces it.
 */
export const regenerationFailure = (state, now = Date.now()) => {
  if (isRegenerating(state, now) || state?.status !== STATUS_FAILED) {
    return null;
  }
  if (isMeetingEndRun(state)) {
    return state.message || 'the summary could not be generated';
  }
  const at = new Date(state.updatedAt ?? '').getTime();
  if (Number.isNaN(at) || at < now - STALE_RUN_MS || at > now + CLOCK_SKEW_ALLOWANCE_MS) {
    return null;
  }
  return state.message || 'could not regenerate the summary';
};

export const SUMMARY_IN_PROGRESS_TEXT = 'Summary in progress…';
export const SUMMARY_FAILED_TEXT = 'The summary could not be generated.';
export const SUMMARY_OVERDUE_TEXT = 'The summary is taking longer than expected.';

/**
 * What to say in place of a meeting's summary when it has none to show, or
 * null to say whatever the reader says for no summary ("Not available").
 *
 * For the readers that show the summary without its controls -- the meetings
 * list, the embedded summary panel, the disabled download menu -- so each says
 * the same thing the meeting page does.
 */
export const summaryStatusText = (state, now = Date.now()) => {
  if (isSummaryInProgress(state, now)) return SUMMARY_IN_PROGRESS_TEXT;
  if (isSummaryOverdue(state, now)) return SUMMARY_OVERDUE_TEXT;
  if (isMeetingEndRun(state) && state?.status === STATUS_FAILED) return SUMMARY_FAILED_TEXT;
  return null;
};

export default parseSummaryState;
