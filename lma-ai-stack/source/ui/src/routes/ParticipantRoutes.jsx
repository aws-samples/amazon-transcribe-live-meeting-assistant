/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';

import {
  ParticipantAccessDenied,
  ParticipantHome,
  ParticipantLayout,
} from '../components/participant-home/ParticipantHome';
import ParticipantMeetingList from '../components/participant-meetings/ParticipantMeetingList';
import ParticipantMeetingPage from '../components/participant-meetings/ParticipantMeetingPage';
import VoiceEnrollmentPage from '../components/voice-enrollment/VoiceEnrollmentPage';
import MyConsentsPage from '../components/my-consents/MyConsentsPage';
import MyConsentsBreadcrumbs from '../components/my-consents/breadcrumbs';
import MyConsentsToolsPanel from '../components/my-consents/tools-panel';
import SignInLanding from '../components/my-consents/SignInLanding';
import { PARTICIPANT, roleMayOpen } from '../components/common/roles';
import SignOutRedirect from './SignOutRedirect';
import {
  HOME_PATH,
  LOGIN_PATH,
  LOGOUT_PATH,
  MY_CONSENTS_PATH,
  SHARED_MEETINGS_PATH,
  VOICE_ENROLLMENT_PATH,
} from './constants';

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
  if (pathname === MY_CONSENTS_PATH) return <MyConsentsPage offerPrompt />;
  return <ParticipantHome />;
};

const ParticipantPage = () => {
  const { pathname } = useLocation();
  if (pathname === MY_CONSENTS_PATH) {
    return (
      <ParticipantLayout
        breadcrumbs={<MyConsentsBreadcrumbs homeHref={`#${HOME_PATH}`} />}
        tools={<MyConsentsToolsPanel />}
      >
        {pageFor(pathname)}
      </ParticipantLayout>
    );
  }
  return <ParticipantLayout>{pageFor(pathname)}</ParticipantLayout>;
};

/**
 * Where signing in leads: the page the sign-in link named (a meeting's consent
 * link, say) when the Participant role may open it, otherwise the home page.
 * Only a path within the app is followed.
 */
export const afterSignIn = (redirectParam) => {
  if (!redirectParam || !redirectParam.startsWith('/') || redirectParam.startsWith('//')) return HOME_PATH;
  const pathname = redirectParam.split('?')[0];
  return pathname !== LOGIN_PATH && roleMayOpen(PARTICIPANT, pathname) ? redirectParam : HOME_PATH;
};

/**
 * Where signing in lands: the page the sign-in link named, or, with none, the
 * home page, or My consents while the person has no consent on record and has
 * not dismissed the prompt there (SignInLanding).
 */
const SignInTarget = ({ redirectParam = '' }) => {
  const target = afterSignIn(redirectParam);
  if (target !== HOME_PATH) return <Navigate to={target} replace />;
  return <SignInLanding />;
};

SignInTarget.propTypes = { redirectParam: PropTypes.string };

/**
 * The routes of a signed-in Participant: their home page, the meetings shared
 * with them (read-only, through the Participant view operations only), their
 * own consents given in advance (My consents) and their own voice enrollment.
 * Nothing here reads the deployment settings, the member meeting operations or
 * any configuration; every other path says Access denied.
 */
const ParticipantRoutes = ({ redirectParam = '' }) => (
  <Routes>
    <Route path="/" element={<Navigate to={HOME_PATH} replace />} />
    <Route path={LOGIN_PATH} element={<SignInTarget redirectParam={redirectParam} />} />
    <Route path={LOGOUT_PATH} element={<SignOutRedirect />} />
    <Route path="*" element={<ParticipantPage />} />
  </Routes>
);

ParticipantRoutes.propTypes = { redirectParam: PropTypes.string };

export default ParticipantRoutes;
