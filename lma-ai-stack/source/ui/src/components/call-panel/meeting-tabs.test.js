/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { describe, it, expect } from 'vitest';

import {
  TAB_DETAILS,
  TAB_SUMMARY,
  TAB_SCREEN_SHARE,
  TAB_TRANSCRIPT,
  availableTabs,
  defaultTab,
  resolveTab,
} from './meeting-tabs';

describe('meeting tabs', () => {
  it('opens on the transcript, for a live meeting and an ended one', () => {
    expect(defaultTab({ isLive: true })).toBe(TAB_TRANSCRIPT);
    expect(defaultTab({ isLive: false })).toBe(TAB_TRANSCRIPT);
    expect(resolveTab(null, { isLive: true })).toBe(TAB_TRANSCRIPT);
  });

  it('offers Screen share only for a meeting with key frames', () => {
    expect(availableTabs({ hasKeyframes: false })).toEqual([TAB_SUMMARY, TAB_TRANSCRIPT, TAB_DETAILS]);
    expect(availableTabs({ hasKeyframes: true })).toEqual([TAB_SUMMARY, TAB_TRANSCRIPT, TAB_SCREEN_SHARE, TAB_DETAILS]);
  });

  it('shows a requested tab the meeting has, and the default for one it does not', () => {
    expect(resolveTab(TAB_SUMMARY)).toBe(TAB_SUMMARY);
    expect(resolveTab(TAB_DETAILS, { isLive: true })).toBe(TAB_DETAILS);
    expect(resolveTab(TAB_SCREEN_SHARE, { hasKeyframes: true })).toBe(TAB_SCREEN_SHARE);
    expect(resolveTab(TAB_SCREEN_SHARE, { hasKeyframes: false })).toBe(TAB_TRANSCRIPT);
    expect(resolveTab('agenda')).toBe(TAB_TRANSCRIPT);
  });

  it('opens the Summary tab for a link saved when it was called Overview', () => {
    expect(resolveTab('overview')).toBe(TAB_SUMMARY);
  });
});
