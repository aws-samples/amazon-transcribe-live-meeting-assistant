/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useEffect, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Box, StatusIndicator } from '@cloudscape-design/components';

import { listMyAdvanceConsents } from '../../graphql/queries/advanceConsent';
import { promptDismissed, sessionSub, shouldPrompt } from './consent-prompt';
import useAppContext from '../../contexts/app';
import { HOME_PATH, MY_CONSENTS_PATH } from '../../routes/constants';

const client = generateClient();
const logger = new ConsoleLogger('SignInLanding');

/**
 * Where a Participant lands after signing in without a link: My consents
 * while the deployment takes consent given in advance, they have none on
 * record and they have not dismissed the prompt there; otherwise, or when
 * their consents cannot be read, their home page. In the same tab, as any
 * in-app route.
 */
const SignInLanding = () => {
  const sub = sessionSub(useAppContext());
  const [target, setTarget] = useState(() => (promptDismissed(sub) ? HOME_PATH : ''));

  useEffect(() => {
    if (target) return undefined;
    let cancelled = false;
    (async () => {
      let next = HOME_PATH;
      try {
        const response = await client.graphql({ query: listMyAdvanceConsents });
        if (shouldPrompt(response?.data?.listMyAdvanceConsents, sub)) next = MY_CONSENTS_PATH;
      } catch (error) {
        logger.warn('could not read your consents; opening the home page', error);
      }
      if (!cancelled) setTarget(next);
    })();
    return () => {
      cancelled = true;
    };
  }, [target, sub]);

  if (target) return <Navigate to={target} replace />;
  return (
    <Box padding="xl">
      <StatusIndicator type="loading">Signing you in</StatusIndicator>
    </Box>
  );
};

export default SignInLanding;
