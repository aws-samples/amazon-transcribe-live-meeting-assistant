/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useState, useEffect } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';
import { ConsoleLogger } from 'aws-amplify/utils';
import { useAuthenticator } from '@aws-amplify/ui-react';

const DEFAULT_CREDS_REFRESH_INTERVAL_IN_MS = 60 * 15 * 1000;

const logger = new ConsoleLogger('useCurrentSessionCreds');

const useCurrentSessionCreds = ({ credsIntervalInMs = DEFAULT_CREDS_REFRESH_INTERVAL_IN_MS } = {}) => {
  const { authStatus } = useAuthenticator((context) => [context.authStatus]);
  const [currentSession, setCurrentSession] = useState();
  const [currentCredentials, setCurrentCredentials] = useState();

  useEffect(() => {
    let interval = null;

    const refreshCredentials = async () => {
      try {
        const session = await fetchAuthSession();
        setCurrentSession(session);
        setCurrentCredentials(session.credentials);

        // Persist JWT tokens under the legacy localStorage keys that other LMA
        // modules (VNCViewer, websocket streaming client, etc.) read directly.
        try {
          const idToken = session?.tokens?.idToken?.toString();
          const accessToken = session?.tokens?.accessToken?.toString();
          const poolClientId = session?.tokens?.accessToken?.payload?.client_id;
          if (poolClientId) {
            if (idToken) localStorage.setItem(`${poolClientId}idtokenjwt`, idToken);
            if (accessToken) localStorage.setItem(`${poolClientId}accesstokenjwt`, accessToken);
          }
          if (idToken) localStorage.setItem('lma.idtokenjwt', idToken);
          if (accessToken) localStorage.setItem('lma.accesstokenjwt', accessToken);
        } catch (tokenErr) {
          logger.warn('unable to persist JWT tokens to localStorage', tokenErr);
        }
      } catch (error) {
        logger.error('failed to get credentials', error);
      }
    };

    if (authStatus === 'authenticated') {
      refreshCredentials();
      interval = setInterval(refreshCredentials, credsIntervalInMs);
    } else if (authStatus === 'unauthenticated') {
      setCurrentSession(undefined);
      setCurrentCredentials(undefined);
    }

    return () => {
      if (interval) clearInterval(interval);
    };
  }, [authStatus, credsIntervalInMs]);

  return { currentSession, currentCredentials };
};

export default useCurrentSessionCreds;
