/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// The rules of the Virtual Participant form, shared by the Create Virtual
// Participant form and the form that edits a scheduled Virtual Participant.

import { googleMeetCode, googleMeetIdError } from './google-meet';
import { MAX_AGENDA_CHARS } from '../common/agenda';
import { isMeetingUrl } from './launch-settings';

export const EMPTY_VP_FORM = {
  meetingName: '',
  meetingPlatform: 'ZOOM',
  meetingId: '',
  meetingPassword: '',
  meetingDate: '',
  meetingTime: '',
  agenda: '',
};

export const PLATFORM_OPTIONS = [
  { label: 'Zoom', value: 'ZOOM', disabled: false },
  { label: 'Chime', value: 'CHIME', disabled: false },
  { label: 'Teams', value: 'TEAMS', disabled: false },
  { label: 'WebEx', value: 'WEBEX', disabled: false },
  { label: 'Google Meet', value: 'GOOGLE_MEET', disabled: false },
];

/** How far ahead a new Virtual Participant may be scheduled. */
export const CREATE_MIN_LEAD_MINUTES = 2;
/**
 * How far ahead a scheduled Virtual Participant's settings may be changed, and
 * its new time set. The updateScheduledVirtualParticipant resolver holds the
 * same bound, so the schedule can be moved well before it would fire.
 */
export const EDIT_MIN_LEAD_MINUTES = 5;

/**
 * Why the date and time fields cannot be used to schedule, or '' when they can.
 * An empty time is not an error here; whether one is required is the caller's.
 */
export const meetingTimeError = (meetingDate, meetingTime, minLeadMinutes, now = new Date()) => {
  if (!meetingTime) return '';
  if (meetingTime.length !== 5) return 'Meeting time is incomplete.';
  if (!meetingDate) return 'Please select a meeting date first.';
  const meetingDateTime = new Date(`${meetingDate}T${meetingTime}`);
  const minuteDifference = (meetingDateTime.getTime() - now.getTime()) / (1000 * 60);
  if (minuteDifference >= minLeadMinutes) return '';
  return `Meeting time must be at least ${minLeadMinutes === 2 ? 'two' : minLeadMinutes} minutes from now.`;
};

/** Epoch seconds for the form's date and time, read in this browser's time zone. */
export const formMeetingTimestamp = (meetingDate, meetingTime) => {
  if (!meetingDate || !meetingTime) return null;
  return Math.floor(new Date(`${meetingDate}T${meetingTime}`).getTime() / 1000);
};

const pad = (value) => String(value).padStart(2, '0');

/** The form's date ("YYYY-MM-DD") and time ("hh:mm") for epoch seconds, in this browser's zone. */
export const formDateAndTime = (epochSeconds) => {
  const seconds = Number(epochSeconds);
  if (!Number.isFinite(seconds) || seconds <= 0) return { meetingDate: '', meetingTime: '' };
  const date = new Date(seconds * 1000);
  return {
    meetingDate: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    meetingTime: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
  };
};

/** This browser's IANA time zone, or null. */
export const browserTimeZone = () => {
  const { timeZone } = Intl.DateTimeFormat().resolvedOptions();
  return timeZone || null;
};

export const agendaIsTooLong = (agenda) => (agenda || '').trim().length > MAX_AGENDA_CHARS;

/** The meeting ID field's error, which only Google Meet has. */
export const meetingIdError = (form) =>
  form.meetingPlatform === 'GOOGLE_MEET' ? googleMeetIdError(form.meetingId) : '';

/**
 * The meeting ID and password as they are submitted. A join URL carries its
 * password server-side and the password field is hidden, so a (possibly stale)
 * separate password is never submitted with one; Google Meet has no meeting
 * password, and is stored as its meeting code. `passwordApplies` says whether
 * the platform and ID take a password at all.
 */
export const submittedMeetingFields = (form) => {
  const isGoogleMeet = form.meetingPlatform === 'GOOGLE_MEET';
  const passwordApplies = !isMeetingUrl(form.meetingId) && !isGoogleMeet;
  const meetingId = isGoogleMeet
    ? googleMeetCode(form.meetingId) || (form.meetingId || '').replace(/ /g, '')
    : (form.meetingId || '').replace(/ /g, '');
  return {
    meetingId,
    meetingPassword: passwordApplies ? form.meetingPassword || '' : '',
    passwordApplies,
  };
};

/** Whether the shared fields can be submitted (name, ID, agenda length). */
export const formFieldsAreValid = (form) =>
  !!form.meetingName && !!form.meetingId && !meetingIdError(form) && !agendaIsTooLong(form.agenda);
