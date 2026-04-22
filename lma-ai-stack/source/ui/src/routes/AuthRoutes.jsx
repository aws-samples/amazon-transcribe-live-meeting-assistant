/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { signOut } from 'aws-amplify/auth';
import React, { useEffect } from 'react';
import PropTypes from 'prop-types';
import { Navigate, Route, Routes } from 'react-router-dom';

import { SettingsContext } from '../contexts/settings';
import useParameterStore from '../hooks/use-parameter-store';
import useAppContext from '../contexts/app';

import CallsRoutes from './CallsRoutes';
import StreamAudioRoutes from './StreamAudioRoutes';
import UploadAudioRoutes from './UploadAudioRoutes';
import VirtualParticipantRoutes from './VirtualParticipantRoutes';
import BrowserExtensionRoutes from './BrowserExtensionRoutes';
import MeetingsQueryRoutes from './MeetingsQueryRoutes';
import MCPServersRoutes from './MCPServersRoutes';
import NovaSonicConfigRoutes from './NovaSonicConfigRoutes';
import TranscriptSummaryRoutes from './TranscriptSummaryRoutes';
import UserManagementRoutes from './UserManagementRoutes';
import EmbedRoutes from './EmbedRoutes';

import {
  CALLS_PATH,
  DEFAULT_PATH,
  LOGIN_PATH,
  LOGOUT_PATH,
  STREAM_AUDIO_PATH,
  UPLOAD_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  BROWSER_EXTENSION_PATH,
  MEETINGS_QUERY_PATH,
  MCP_SERVERS_PATH,
  NOVA_SONIC_CONFIG_PATH,
  TRANSCRIPT_SUMMARY_PATH,
  USER_MANAGEMENT_PATH,
  EMBED_PATH,
} from './constants';

const logger = new ConsoleLogger('AuthRoutes');

const SignOutRedirect = () => {
  useEffect(() => {
    signOut()
      .catch((err) => logger.error('signOut error', err))
      .finally(() => {
        window.location.reload();
      });
  }, []);
  return null;
};

const AuthRoutes = ({ redirectParam }) => {
  const { currentCredentials } = useAppContext();
  const settings = useParameterStore(currentCredentials);

  // eslint-disable-next-line react/jsx-no-constructed-context-values
  const settingsContextValue = { settings };
  logger.debug('settingsContextValue', settingsContextValue);

  return (
    <SettingsContext.Provider value={settingsContextValue}>
      <Routes>
        <Route path={`${CALLS_PATH}/*`} element={<CallsRoutes />} />
        <Route
          path={LOGIN_PATH}
          element={
            <Navigate to={!redirectParam || redirectParam === LOGIN_PATH ? DEFAULT_PATH : `${redirectParam}`} replace />
          }
        />
        <Route path={LOGOUT_PATH} element={<SignOutRedirect />} />
        <Route path={`${MEETINGS_QUERY_PATH}/*`} element={<MeetingsQueryRoutes />} />
        <Route path={`${STREAM_AUDIO_PATH}/*`} element={<StreamAudioRoutes />} />
        <Route path={`${UPLOAD_AUDIO_PATH}/*`} element={<UploadAudioRoutes />} />
        <Route path={`${VIRTUAL_PARTICIPANT_PATH}/*`} element={<VirtualParticipantRoutes />} />
        <Route path={`${BROWSER_EXTENSION_PATH}/*`} element={<BrowserExtensionRoutes />} />
        <Route path={`${MCP_SERVERS_PATH}/*`} element={<MCPServersRoutes />} />
        <Route path={`${NOVA_SONIC_CONFIG_PATH}/*`} element={<NovaSonicConfigRoutes />} />
        <Route path={`${TRANSCRIPT_SUMMARY_PATH}/*`} element={<TranscriptSummaryRoutes />} />
        <Route path={`${USER_MANAGEMENT_PATH}/*`} element={<UserManagementRoutes />} />
        <Route path={`${EMBED_PATH}/*`} element={<EmbedRoutes />} />
        <Route path="*" element={<Navigate to={DEFAULT_PATH} replace />} />
      </Routes>
    </SettingsContext.Provider>
  );
};

AuthRoutes.propTypes = {
  redirectParam: PropTypes.string.isRequired,
};

export default AuthRoutes;
