/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useEffect, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import getMeetingConsentDefaults from '../graphql/queries/getMeetingConsentDefaults';

const logger = new ConsoleLogger('useMeetingConsentDefaults');

/**
 * The deployment's meeting consent mode, rule and per-meeting-choice switch
 * ({ Mode, Rule, AllowPerMeetingOverride }), read each time `enabled` turns
 * true, so a form that opens sees the current settings. Null until read, and
 * null when they could not be read: the form then offers every choice, and the
 * VP and the meeting consent resolver still apply the deployment's rule.
 * `client` is for tests.
 */
const useMeetingConsentDefaults = (enabled, { client } = {}) => {
  const [defaults, setDefaults] = useState(null);
  useEffect(() => {
    // Forget the last answer, so a reopened form does not act on it while the
    // new read is in flight.
    setDefaults(null);
    if (!enabled) return undefined;
    let cancelled = false;
    const read = async () => {
      try {
        const response = await (client || generateClient()).graphql({ query: getMeetingConsentDefaults });
        if (!cancelled) setDefaults(response?.data?.getMeetingConsentDefaults || null);
      } catch (error) {
        logger.warn('The meeting consent settings could not be read; offering every choice', error);
        if (!cancelled) setDefaults(null);
      }
    };
    read();
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return defaults;
};

export default useMeetingConsentDefaults;
