// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Amplify, Logger } from 'aws-amplify';
import { HashRouter } from 'react-router-dom';

import { AppContext } from './contexts/app';

import useUserAuthState from './hooks/use-user-auth-state';
import useAwsConfig from './hooks/use-aws-config';
import useCurrentSessionCreds from './hooks/use-current-session-creds';

import Routes from './routes/Routes';

import './App.css';

Amplify.Logger.LOG_LEVEL = process.env.NODE_ENV === 'development' ? 'DEBUG' : 'WARNING';
const logger = new Logger('App');

const App = () => {
  const awsConfig = useAwsConfig();
  const { authState, user } = useUserAuthState(awsConfig);
  const { currentSession, currentCredentials } = useCurrentSessionCreds({ authState });
  const [errorMessage, setErrorMessage] = useState();
  const [navigationOpen, setNavigationOpen] = useState(true);

  // eslint-disable-next-line react/jsx-no-constructed-context-values
  const appContextValue = {
    authState,
    awsConfig,
    errorMessage,
    currentCredentials,
    currentSession,
    setErrorMessage,
    user,
    navigationOpen,
    setNavigationOpen,
  };
  logger.debug('appContextValue', appContextValue);

  return (
    <div className="App">
      <AppContext.Provider value={appContextValue}>
        <HashRouter>
          <Routes />
        </HashRouter>
      </AppContext.Provider>
    </div>
  );
};

export default App;
