/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
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
