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
 * -- which is every meeting summarised before this existed, and every summary
 * the Kinesis pipeline writes -- reads as a generated summary with nothing
 * running rather than as an error.
 */

export const SOURCE_GENERATED = 'generated';
export const SOURCE_MANUAL = 'manual';
export const STATUS_RUNNING = 'running';
export const STATUS_FAILED = 'failed';
export const STATUS_IDLE = 'idle';

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
export const isRegenerating = (state, now = Date.now()) => {
  if (state?.status !== STATUS_RUNNING) {
    return false;
  }
  // `updateCallSummaryText` stores whatever state string it is given, so the
  // shape of this field is not something a reader can take for granted: a mark
  // with no usable stamp reads as finished rather than disabling the controls on
  // this meeting with nothing able to clear it.
  const started = new Date(state.startedAt ?? '').getTime();
  if (Number.isNaN(started) || started > now + CLOCK_SKEW_ALLOWANCE_MS) {
    return false;
  }
  return started > now - STALE_RUN_MS;
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
 */
export const regenerationFailure = (state, now = Date.now()) => {
  if (isRegenerating(state, now) || state?.status !== STATUS_FAILED) {
    return null;
  }
  const at = new Date(state.updatedAt ?? '').getTime();
  if (Number.isNaN(at) || at < now - STALE_RUN_MS || at > now + CLOCK_SKEW_ALLOWANCE_MS) {
    return null;
  }
  return state.message || 'could not regenerate the summary';
};

export default parseSummaryState;
