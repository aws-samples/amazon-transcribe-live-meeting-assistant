/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useEffect, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import getVirtualParticipantVideoRecording from '../graphql/queries/getVirtualParticipantVideoRecording';

const logger = new ConsoleLogger('useVpVideoRecordingSetting');

/**
 * Whether Meeting Recording › Record Virtual Participant meeting video is on,
 * read each time `enabled` turns true, so a form that opens sees the current
 * value. Null until read, and null when it could not be read: the form then
 * leaves its Record meeting video switch alone, and the Virtual Participant
 * still applies the setting when it starts. `client` is for tests.
 */
const useVpVideoRecordingSetting = (enabled, { client } = {}) => {
  const [allowed, setAllowed] = useState(null);
  useEffect(() => {
    setAllowed(null);
    if (!enabled) return undefined;
    let cancelled = false;
    const read = async () => {
      try {
        const response = await (client || generateClient()).graphql({ query: getVirtualParticipantVideoRecording });
        const value = response?.data?.getVirtualParticipantVideoRecording;
        if (!cancelled) setAllowed(typeof value === 'boolean' ? value : null);
      } catch (error) {
        logger.warn('The Virtual Participant video setting could not be read', error);
        if (!cancelled) setAllowed(null);
      }
    };
    read();
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return allowed;
};

export default useVpVideoRecordingSetting;
