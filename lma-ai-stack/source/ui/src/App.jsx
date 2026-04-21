/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { HashRouter } from 'react-router-dom';
import { Authenticator, ThemeProvider, useAuthenticator } from '@aws-amplify/ui-react';
import { ConsoleLogger } from 'aws-amplify/utils';
// eslint-disable-next-line import/no-unresolved
import '@aws-amplify/ui-react/styles.css';

import { AppContext } from './contexts/app';

import useAwsConfig from './hooks/use-aws-config';
import useCurrentSessionCreds from './hooks/use-current-session-creds';

import Routes from './routes/Routes';

import './App.css';

ConsoleLogger.LOG_LEVEL = import.meta.env.DEV ? 'DEBUG' : 'WARN';
const logger = new ConsoleLogger('App');

const AppContent = () => {
  const awsConfig = useAwsConfig();
  const { authStatus: authState, user } = useAuthenticator((context) => [context.authStatus, context.user]);
  const { currentSession, currentCredentials } = useCurrentSessionCreds({});
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

const App = () => (
  <ThemeProvider>
    <Authenticator.Provider>
      <AppContent />
    </Authenticator.Provider>
  </ThemeProvider>
);

export default App;
