// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import PropTypes from 'prop-types';
import { Logger } from 'aws-amplify';
import { Redirect, Route, Switch } from 'react-router-dom';

import { AmplifySignOut } from '@aws-amplify/ui-react';

import { SettingsContext } from '../contexts/settings';
import useParameterStore from '../hooks/use-parameter-store';
import useAppContext from '../contexts/app';

import CallsRoutes from './CallsRoutes';
import StreamAudioRoutes from './StreamAudioRoutes';
import VirtualParticipantRoutes from './VirtualParticipantRoutes';

import {
  CALLS_PATH,
  DEFAULT_PATH,
  LOGIN_PATH,
  LOGOUT_PATH,
  STREAM_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
} from './constants';

const logger = new Logger('AuthRoutes');

const AuthRoutes = ({ redirectParam }) => {
  const { currentCredentials } = useAppContext();
  const settings = useParameterStore(currentCredentials);

  // eslint-disable-next-line react/jsx-no-constructed-context-values
  const settingsContextValue = {
    settings,
  };
  logger.debug('settingsContextValue', settingsContextValue);

  return (
    <SettingsContext.Provider value={settingsContextValue}>
      <Switch>
        <Route path={CALLS_PATH}>
          <CallsRoutes />
        </Route>
        <Route path={LOGIN_PATH}>
          <Redirect to={!redirectParam || redirectParam === LOGIN_PATH ? DEFAULT_PATH : `${redirectParam}`} />
        </Route>
        <Route path={LOGOUT_PATH}>
          <AmplifySignOut />
        </Route>
        <Route path={STREAM_AUDIO_PATH}>
          <StreamAudioRoutes />
        </Route>
        <Route path={VIRTUAL_PARTICIPANT_PATH}>
          <VirtualParticipantRoutes />
        </Route>
        <Route>
          <Redirect to={DEFAULT_PATH} />
        </Route>
      </Switch>
    </SettingsContext.Provider>
  );
};

AuthRoutes.propTypes = {
  redirectParam: PropTypes.string.isRequired,
};

export default AuthRoutes;
