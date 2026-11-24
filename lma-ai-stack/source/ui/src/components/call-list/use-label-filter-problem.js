/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { useEffect, useState } from 'react';

import { labelFilterProblem } from '../common/meeting-preparation';

/** How long the label filter waits after the last keystroke to say why it matches nothing. */
export const LABEL_PROBLEM_DELAY_MS = 600;

/**
 * Why the meeting list's label filter can match nothing (labelFilterProblem),
 * or null. The reason shows once typing pauses for LABEL_PROBLEM_DELAY_MS
 * rather than from the first character, so a key being typed is not flagged,
 * and it clears as soon as the filter is one that can match.
 */
const useLabelFilterProblem = (labelFilter) => {
  const [problem, setProblem] = useState(null);
  useEffect(() => {
    const found = labelFilterProblem(labelFilter);
    if (!found) {
      setProblem(null);
      return undefined;
    }
    const timer = setTimeout(() => setProblem(found), LABEL_PROBLEM_DELAY_MS);
    return () => clearTimeout(timer);
  }, [labelFilter]);
  return problem;
};

export default useLabelFilterProblem;
