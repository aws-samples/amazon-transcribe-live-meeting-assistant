/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// Readable values for the settings a Virtual Participant was created with, as
// the Create Virtual Participant form asked for them. Each function takes the
// stored value and returns the text shown on the Virtual Participant page, so
// the wording lives in one place and is tested without rendering.

import { MEETING_CONSENT_OPTIONS } from './meeting-consent-options';

export const DEPLOYMENT_DEFAULT = 'Not set (deployment default)';
export const NOT_RECORDED = 'Not recorded';

/** Whether a meeting ID is a full join link rather than an ID or code. */
export const isMeetingUrl = (meetingId) => /^\s*https?:\/\//i.test(meetingId || '');

/** The label the form uses for the meeting ID field, for the value stored. */
export const meetingIdLabel = (platform, meetingId) => {
  if (platform === 'GOOGLE_MEET') return 'Meeting code or link';
  return isMeetingUrl(meetingId) ? 'Meeting URL' : 'Meeting ID';
};

/**
 * The meeting password, without its value: only whether one was given. A join
 * link and a Google Meet code never take one, which the form also reflects.
 */
export const describeMeetingPassword = ({ hasMeetingPassword, meetingPlatform, meetingId }) => {
  if (hasMeetingPassword === true) return 'Set (not shown)';
  if (meetingPlatform === 'GOOGLE_MEET') return 'Not used on Google Meet';
  if (isMeetingUrl(meetingId)) return 'Not set (the meeting link carries it, if any)';
  if (hasMeetingPassword === false) return 'Not set';
  return NOT_RECORDED;
};

/** The Zoom account choice; null for a platform where it does not apply. */
export const describeZoomAccount = ({ meetingPlatform, useStoredZoomAccount }) => {
  if (meetingPlatform !== 'ZOOM') return null;
  if (useStoredZoomAccount === true) return "The launching user's stored Zoom account";
  if (useStoredZoomAccount === false) return 'Joined as a guest';
  return NOT_RECORDED;
};

const formatInZone = (date, timeZone) => {
  const options = {
    dateStyle: 'medium',
    timeStyle: 'short',
    ...(timeZone ? { timeZone } : {}),
  };
  try {
    return new Intl.DateTimeFormat(undefined, options).format(date);
  } catch (_) {
    // An unknown zone name: show the time in this browser's zone instead.
    return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
  }
};

const zoneIsKnown = (timeZone) => {
  try {
    Intl.DateTimeFormat(undefined, { timeZone });
    return true;
  } catch (_) {
    return false;
  }
};

/** The scheduled instant, from meetingTime (epoch seconds) or scheduledFor (ISO). */
export const scheduledDate = ({ meetingTime, scheduledFor }) => {
  if (meetingTime !== null && meetingTime !== undefined && meetingTime !== '') {
    const seconds = Number(meetingTime);
    if (Number.isFinite(seconds) && seconds > 0) return new Date(seconds * 1000);
  }
  if (scheduledFor) {
    const date = new Date(scheduledFor);
    if (!Number.isNaN(date.getTime())) return date;
  }
  return null;
};

/**
 * When the Virtual Participant was asked to join: "Start now", or the scheduled
 * time in the zone it was entered in, and in this browser's zone when that
 * differs. `viewerTimeZone` is a parameter so tests do not depend on the host.
 */
export const describeStart = (vp, viewerTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone) => {
  if (!vp?.isScheduled) {
    return { primary: 'Start now', secondary: null };
  }
  const date = scheduledDate(vp);
  if (!date) return { primary: 'Scheduled (time not recorded)', secondary: null };
  const enteredZone = vp.scheduledTimeZone && zoneIsKnown(vp.scheduledTimeZone) ? vp.scheduledTimeZone : null;
  if (!enteredZone) {
    return {
      primary: `${formatInZone(date, viewerTimeZone)} (${viewerTimeZone || 'your time zone'})`,
      secondary: 'Time zone it was entered in: not recorded',
    };
  }
  const primary = `${formatInZone(date, enteredZone)} (${enteredZone})`;
  const secondary =
    viewerTimeZone && viewerTimeZone !== enteredZone
      ? `${formatInZone(date, viewerTimeZone)} in your time zone (${viewerTimeZone})`
      : null;
  return { primary, secondary };
};

/** On / Off for a Boolean setting, or the deployment default when absent. */
export const describeToggle = (value) => {
  if (value === true) return 'On';
  if (value === false) return 'Off';
  return DEPLOYMENT_DEFAULT;
};

/** The meeting consent choice as the form labels it, or the deployment default. */
export const describeConsent = ({ consentMode, consentRule }) => {
  if (!consentMode) return DEPLOYMENT_DEFAULT;
  const value = consentRule ? `${consentMode}:${consentRule}` : consentMode;
  const option = MEETING_CONSENT_OPTIONS.find((candidate) => candidate.value === value);
  return option ? option.label : value;
};
