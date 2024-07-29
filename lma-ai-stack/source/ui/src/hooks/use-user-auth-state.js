// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect } from 'react';
import { onAuthUIStateChange } from '@aws-amplify/ui-components';
import { Logger } from 'aws-amplify';

const logger = new Logger('useUserAuthState');

const useUserAuthState = (awsconfig) => {
  const [authState, setAuthState] = useState();
  const [user, setUser] = useState();

  useEffect(() => {
    onAuthUIStateChange((nextAuthState, authData) => {
      logger.debug('auth state change nextAuthState:', nextAuthState);
      logger.debug('auth state change authData:', authData);
      setAuthState(nextAuthState);
      setUser(authData);
      if (authData) {
        // prettier-ignore
        localStorage.setItem(`${authData.pool.clientId}idtokenjwt`, authData.signInUserSession.idToken.jwtToken);
        // prettier-ignore
        localStorage.setItem(`${authData.pool.clientId}accesstokenjwt`, authData.signInUserSession.accessToken.jwtToken);
        // prettier-ignore
        localStorage.setItem(`${authData.pool.clientId}refreshtoken`, authData.signInUserSession.refreshToken.jwtToken);
      }
    });
  }, [awsconfig]);

  return { authState, user };
};

export default useUserAuthState;
