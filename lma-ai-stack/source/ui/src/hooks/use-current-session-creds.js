/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useState, useEffect } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';
import { ConsoleLogger } from 'aws-amplify/utils';
import { useAuthenticator } from '@aws-amplify/ui-react';

import { groupsNameARole } from '../components/common/roles';

const DEFAULT_CREDS_REFRESH_INTERVAL_IN_MS = 60 * 15 * 1000;

const logger = new ConsoleLogger('useCurrentSessionCreds');

// Whether this page load has already asked for fresh tokens; asked at most once.
let refreshedForRole = false;

/**
 * The session, with tokens that name the caller's role.
 *
 * Every token the user pool issues names the caller's role in cognito:groups
 * (its pre token generation trigger). A token cached from before that trigger
 * existed names none, and the GraphQL API refuses it, so such a session is
 * refreshed once, which issues tokens through the trigger. The identity pool
 * gives such a token no credentials either, which can fail the first read
 * outright; that failure is answered with the same one refresh.
 */
export const fetchSessionNamingRole = async () => {
  let session;
  try {
    session = await fetchAuthSession();
  } catch (error) {
    if (refreshedForRole) throw error;
    refreshedForRole = true;
    logger.debug('session could not be read; refreshing its tokens once', error);
    return fetchAuthSession({ forceRefresh: true });
  }
  const groups = session?.tokens?.accessToken?.payload?.['cognito:groups'] || [];
  if (!session?.tokens || groupsNameARole(groups) || refreshedForRole) {
    return session;
  }
  refreshedForRole = true;
  logger.debug('session tokens name no role; refreshing them');
  return fetchAuthSession({ forceRefresh: true });
};

/** For tests: forget that this page load refreshed its tokens. */
export const resetRoleRefreshForTests = () => {
  refreshedForRole = false;
};

const useCurrentSessionCreds = ({ credsIntervalInMs = DEFAULT_CREDS_REFRESH_INTERVAL_IN_MS } = {}) => {
  const { authStatus } = useAuthenticator((context) => [context.authStatus]);
  const [currentSession, setCurrentSession] = useState();
  const [currentCredentials, setCurrentCredentials] = useState();

  useEffect(() => {
    let interval = null;

    const refreshCredentials = async () => {
      try {
        const session = await fetchSessionNamingRole();
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
