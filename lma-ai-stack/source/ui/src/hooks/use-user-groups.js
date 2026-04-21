/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { useState, useEffect } from 'react';
import { fetchAuthSession } from 'aws-amplify/auth';
import { ConsoleLogger } from 'aws-amplify/utils';

const logger = new ConsoleLogger('useUserGroups');

/**
 * Hook to retrieve the current user's Cognito groups using the Amplify v6 API.
 *
 * Replaces the old Amplify v4 pattern:
 *   user?.signInUserSession?.accessToken?.payload['cognito:groups']
 *
 * With the Amplify v6 pattern:
 *   (await fetchAuthSession()).tokens.idToken.payload['cognito:groups']
 *
 * @returns {{ userGroups: string[], isAdmin: boolean }}
 */
const useUserGroups = () => {
  const [userGroups, setUserGroups] = useState([]);

  useEffect(() => {
    const fetchGroups = async () => {
      try {
        const session = await fetchAuthSession();
        const groups = session?.tokens?.idToken?.payload?.['cognito:groups'] || [];
        const groupsArray = Array.isArray(groups) ? groups : [groups];
        logger.debug('User groups:', groupsArray);
        setUserGroups(groupsArray);
      } catch (error) {
        logger.error('Error fetching user groups:', error);
        setUserGroups([]);
      }
    };
    fetchGroups();
  }, []);

  const isAdmin = userGroups.includes('Admin');

  return { userGroups, isAdmin };
};

export default useUserGroups;
