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
