/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting page's tabs, and which one is shown. Kept free of React so the
 * rules can be tested directly; use-meeting-tab.js keeps the choice in the URL.
 */

export const TAB_SUMMARY = 'summary';
export const TAB_TRANSCRIPT = 'transcript';
export const TAB_SCREEN_SHARE = 'screen-share';
export const TAB_DETAILS = 'details';

/** The query parameter that holds the tab: `#/calls/<Meeting ID>?tab=summary`. */
export const TAB_PARAM = 'tab';

/**
 * The tab a meeting opens on when the link names none, or names one it does
 * not have. The transcript, for a meeting that is running and for one that has
 * ended: it is what most visits are for, and what a live meeting is watched on.
 */
// eslint-disable-next-line no-unused-vars
export const defaultTab = ({ isLive = false } = {}) => TAB_TRANSCRIPT;

/** The tabs a meeting has, in order. Screen share only for a meeting with key frames. */
export const availableTabs = ({ hasKeyframes = false } = {}) => [
  TAB_SUMMARY,
  TAB_TRANSCRIPT,
  ...(hasKeyframes ? [TAB_SCREEN_SHARE] : []),
  TAB_DETAILS,
];

// Tabs by the names they had before, so a link saved then opens the same tab.
const RENAMED_TABS = { overview: TAB_SUMMARY };

/** The tab to show for a requested one: it, when the meeting has it, and otherwise the default. */
export const resolveTab = (requested, { isLive = false, hasKeyframes = false } = {}) => {
  const tab = RENAMED_TABS[requested] || requested;
  return availableTabs({ hasKeyframes }).includes(tab) ? tab : defaultTab({ isLive });
};
