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
import { PARTICIPANT, roleMayOpen } from '../components/common/roles';
import SignOutRedirect from './SignOutRedirect';
import { HOME_PATH, LOGIN_PATH, LOGOUT_PATH } from './constants';

// A page by the role's route table: the home page where the Participant role
// may open the path, Access denied everywhere else.
const ParticipantPage = () => {
  const { pathname } = useLocation();
  return (
    <ParticipantLayout>
      {roleMayOpen(PARTICIPANT, pathname) ? <ParticipantHome /> : <ParticipantAccessDenied />}
    </ParticipantLayout>
  );
};

/**
 * The routes of a signed-in Participant. Nothing here reads a meeting, the
 * deployment settings or any GraphQL operation; the page itself is the
 * Participant's home page, and every other path says Access denied.
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
