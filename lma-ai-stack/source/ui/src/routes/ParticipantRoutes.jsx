/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';

import {
  ParticipantAccessDenied,
  ParticipantHome,
  ParticipantLayout,
} from '../components/participant-home/ParticipantHome';
import ParticipantMeetingList from '../components/participant-meetings/ParticipantMeetingList';
import ParticipantMeetingPage from '../components/participant-meetings/ParticipantMeetingPage';
import VoiceEnrollmentPage from '../components/voice-enrollment/VoiceEnrollmentPage';
import { PARTICIPANT, roleMayOpen } from '../components/common/roles';
import SignOutRedirect from './SignOutRedirect';
import { HOME_PATH, LOGIN_PATH, LOGOUT_PATH, SHARED_MEETINGS_PATH, VOICE_ENROLLMENT_PATH } from './constants';

// A meeting id as the path holds it: encoded by the list's link, and decoded
// once; a path that is not valid encoding is taken as it stands.
const pathCallId = (rest) => {
  try {
    return decodeURIComponent(rest);
  } catch (e) {
    return rest;
  }
};

// The page each path the Participant role may open shows; Access denied for
// every other path, as the role's route table says.
const pageFor = (pathname) => {
  if (!roleMayOpen(PARTICIPANT, pathname)) return <ParticipantAccessDenied />;
  if (pathname === SHARED_MEETINGS_PATH) return <ParticipantMeetingList />;
  if (pathname.startsWith(`${SHARED_MEETINGS_PATH}/`)) {
    const callId = pathCallId(pathname.slice(SHARED_MEETINGS_PATH.length + 1));
    return <ParticipantMeetingPage callId={callId} />;
  }
  if (pathname === VOICE_ENROLLMENT_PATH || pathname.startsWith(`${VOICE_ENROLLMENT_PATH}/`)) {
    return <VoiceEnrollmentPage />;
  }
  return <ParticipantHome />;
};

const ParticipantPage = () => {
  const { pathname } = useLocation();
  return <ParticipantLayout>{pageFor(pathname)}</ParticipantLayout>;
};

/**
 * The routes of a signed-in Participant: their home page, the meetings shared
 * with them (read-only, through the Participant view operations only) and
 * their own voice enrollment. Nothing here reads the deployment settings, the
 * member meeting operations or any configuration; every other path says
 * Access denied.
 */
const ParticipantRoutes = () => (
  <Routes>
    <Route path="/" element={<Navigate to={HOME_PATH} replace />} />
    <Route path={LOGIN_PATH} element={<Navigate to={HOME_PATH} replace />} />
    <Route path={LOGOUT_PATH} element={<SignOutRedirect />} />
    <Route path="*" element={<ParticipantPage />} />
  </Routes>
);

export default ParticipantRoutes;
