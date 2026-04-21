/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import React, { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { useAuthenticator } from '@aws-amplify/ui-react';

import UnauthRoutes from './UnauthRoutes';
import AuthRoutes from './AuthRoutes';
import useAppContext from '../contexts/app';

import { REDIRECT_URL_PARAM } from './constants';

const logger = new ConsoleLogger('Routes');

const Routes = () => {
  const { user, currentCredentials } = useAppContext();
  const { authStatus } = useAuthenticator((context) => [context.authStatus]);
  const location = useLocation();
  const [urlSearchParams, setUrlSearchParams] = useState(new URLSearchParams({}));
  const [redirectParam, setRedirectParam] = useState('');

  useEffect(() => {
    if (!location?.search) {
      return;
    }
    const searchParams = new URLSearchParams(location.search);
    logger.debug('searchParams:', searchParams);
    setUrlSearchParams(searchParams);
  }, [location]);

  useEffect(() => {
    const redirect = urlSearchParams?.get(REDIRECT_URL_PARAM);
    if (!redirect) {
      return;
    }
    logger.debug('redirect:', redirect);
    setRedirectParam(redirect);
  }, [urlSearchParams]);

  return !(authStatus === 'authenticated' && user && currentCredentials) ? (
    <UnauthRoutes location={location} />
  ) : (
    <AuthRoutes redirectParam={redirectParam} />
  );
};

export default Routes;
