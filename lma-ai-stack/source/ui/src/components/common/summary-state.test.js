/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The summary state object is read by the meeting page to decide three things:
 * whether to show a spinner, whether to show a failure, and whether replacing
 * the text needs a confirmation first. Every meeting summarised before this
 * field existed has no state at all, and the Kinesis pipeline never writes one,
 * so "absent" has to read as a generated summary with nothing running rather
 * than as an error.
 */
import { describe, it, expect } from 'vitest';

import {
  SOURCE_GENERATED,
  SOURCE_MANUAL,
  STALE_RUN_MS,
  STATUS_FAILED,
  STATUS_IDLE,
  STATUS_RUNNING,
  isHandEdited,
  isRegenerating,
  parseSummaryState,
  regenerationFailure,
  serializeSummaryState,
} from './summary-state';

const NOW = Date.parse('2026-03-04T12:00:00.000Z');
const iso = (offsetMs) => new Date(NOW + offsetMs).toISOString();

describe('parseSummaryState', () => {
  it('reads a meeting with no state as empty rather than throwing', () => {
    expect(parseSummaryState(undefined)).toEqual({});
    expect(parseSummaryState(null)).toEqual({});
    expect(parseSummaryState('')).toEqual({});
  });

  it('reads malformed stored state as empty', () => {
    expect(parseSummaryState('not json')).toEqual({});
    expect(parseSummaryState('"a string"')).toEqual({});
    expect(parseSummaryState('[1, 2]')).toEqual({});
  });

  it('round-trips through serializeSummaryState', () => {
    const state = { source: SOURCE_MANUAL, status: STATUS_IDLE, updatedAt: iso(0) };
    expect(parseSummaryState(serializeSummaryState(state))).toEqual(state);
  });
});

describe('isRegenerating', () => {
  it('is false for a meeting with no state', () => {
    expect(isRegenerating({}, NOW)).toBe(false);
  });

  it('is true while a run is marked as going', () => {
    expect(isRegenerating({ status: STATUS_RUNNING, startedAt: iso(-5000) }, NOW)).toBe(true);
  });

  it('ignores a mark older than the stale cut-off', () => {
    // A run whose Lambda was killed would otherwise disable the button for ever.
    expect(isRegenerating({ status: STATUS_RUNNING, startedAt: iso(-STALE_RUN_MS - 1000) }, NOW)).toBe(false);
  });

  it('trusts a mark with no timestamp', () => {
    expect(isRegenerating({ status: STATUS_RUNNING }, NOW)).toBe(true);
  });

  it('is false for an unreadable timestamp', () => {
    expect(isRegenerating({ status: STATUS_RUNNING, startedAt: 'yesterday' }, NOW)).toBe(false);
  });

  it('is false once the run reached an outcome', () => {
    expect(isRegenerating({ status: STATUS_IDLE, startedAt: iso(-1000) }, NOW)).toBe(false);
    expect(isRegenerating({ status: STATUS_FAILED, startedAt: iso(-1000) }, NOW)).toBe(false);
  });
});

describe('isHandEdited', () => {
  it('is true only for a summary somebody wrote', () => {
    expect(isHandEdited({ source: SOURCE_MANUAL })).toBe(true);
    expect(isHandEdited({ source: SOURCE_GENERATED })).toBe(false);
    expect(isHandEdited({})).toBe(false);
    expect(isHandEdited(undefined)).toBe(false);
  });
});

describe('regenerationFailure', () => {
  it('is null when nothing failed', () => {
    expect(regenerationFailure({}, NOW)).toBeNull();
    expect(regenerationFailure({ status: STATUS_IDLE }, NOW)).toBeNull();
  });

  it('reports the stored message', () => {
    expect(regenerationFailure({ status: STATUS_FAILED, message: 'model unavailable' }, NOW)).toBe('model unavailable');
  });

  it('falls back to a sentence when no message was stored', () => {
    expect(regenerationFailure({ status: STATUS_FAILED }, NOW)).toBeTruthy();
  });

  it('is suppressed once another run is going', () => {
    // Otherwise the previous failure sits next to the new spinner.
    expect(regenerationFailure({ status: STATUS_RUNNING, startedAt: iso(-1000), message: 'old' }, NOW)).toBeNull();
  });
});
