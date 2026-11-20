/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useEffect, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';

import listSummaryProfiles from '../graphql/queries/listSummaryProfiles';

const logger = new ConsoleLogger('useSummaryProfiles');

/** The default profile, offered whatever the read answers. */
export const DEFAULT_SUMMARY_PROFILE = {
  Name: 'default',
  Label: 'Default',
  Description: 'The Transcript Summary sections on the Configuration page.',
  StructuredActionItems: false,
};

/**
 * The summary profiles a meeting can be summarised with (listSummaryProfiles),
 * default first, read each time `enabled` turns true. Until read, and when
 * they could not be read, only the default is offered: the meeting is then
 * summarised as it would be without a choice. `client` is for tests.
 */
const useSummaryProfiles = (enabled = true, { client } = {}) => {
  const [profiles, setProfiles] = useState([DEFAULT_SUMMARY_PROFILE]);
  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    const read = async () => {
      try {
        const response = await (client || generateClient()).graphql({ query: listSummaryProfiles });
        const listed = (response?.data?.listSummaryProfiles || []).filter((p) => p && p.Name);
        if (!cancelled && listed.length) setProfiles(listed);
      } catch (error) {
        logger.warn('The summary profiles could not be read; offering the default only', error);
      }
    };
    read();
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return profiles;
};

/** The Select options for a list of profiles; a stored name no longer listed is kept, so it shows. */
export const summaryProfileOptions = (profiles, current) => {
  const options = (profiles || [DEFAULT_SUMMARY_PROFILE]).map((p) => ({
    value: p.Name,
    label: p.Label || p.Name,
    description: p.Description || undefined,
  }));
  if (current && current !== 'default' && !options.some((o) => o.value === current)) {
    options.push({ value: current, label: current, description: 'Not in the configuration' });
  }
  return options;
};

export default useSummaryProfiles;
