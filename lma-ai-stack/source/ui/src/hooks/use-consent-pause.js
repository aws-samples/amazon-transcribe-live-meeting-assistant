/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Whether the meeting page should say that transcription is paused for
 * consent: true from CONSENT_PAUSE_GRACE_MS after a live meeting with enforced
 * consent started, for as long as its consent does not allow capture
 * (consentPauseDueAt in common/meeting-context.js). The grace period keeps the
 * page from raising an alarm in the seconds it normally takes for the notice
 * to be posted and answered.
 */
import { useEffect, useState } from 'react';

import { consentPauseDueAt } from '../components/common/meeting-context';

const useConsentPause = ({ isLive, summary, startedAt, consentStartedAt }) => {
  const dueAt = consentPauseDueAt({ isLive, summary, startedAt, consentStartedAt });
  const [due, setDue] = useState(() => dueAt !== null && Date.now() >= dueAt);

  useEffect(() => {
    if (dueAt === null) {
      setDue(false);
      return undefined;
    }
    const wait = dueAt - Date.now();
    if (wait <= 0) {
      setDue(true);
      return undefined;
    }
    setDue(false);
    const timer = setTimeout(() => setDue(true), wait);
    return () => clearTimeout(timer);
  }, [dueAt]);

  return due;
};

export default useConsentPause;
