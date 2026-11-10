/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * One troubleshooting job as the reader sees it: a progress line while it runs,
 * the error when it failed, and the agent's answer when it completed.
 *
 * The answer is Markdown in three sections (Root Cause, Recommendations, and an
 * Evidence section inside <details>). It is model output, so it renders through
 * the shared sanitizing rehype chain (common/markdown-plugins.js), whose
 * allowlist keeps <details> and <summary>.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import ReactMarkdown from 'react-markdown';
import { Alert, Box, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';

import markdownRehypePlugins from '../common/markdown-plugins';
import './troubleshooting.css';

export const JOB_SHAPE = PropTypes.shape({
  JobId: PropTypes.string,
  Status: PropTypes.string,
  Question: PropTypes.string,
  Answer: PropTypes.string,
  Error: PropTypes.string,
  Truncated: PropTypes.bool,
  CreatedAt: PropTypes.string,
});

/** Whole seconds since an ISO timestamp, or 0 if it cannot be read. */
export const secondsSince = (iso, now = Date.now()) => {
  const started = Date.parse(iso || '');
  return Number.isNaN(started) ? 0 : Math.max(0, Math.round((now - started) / 1000));
};

const formatElapsed = (seconds) => {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}m ${String(rest).padStart(2, '0')}s` : `${rest}s`;
};

const Elapsed = ({ since = undefined }) => {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return <span data-testid="troubleshooting-elapsed">{formatElapsed(secondsSince(since, now))}</span>;
};

Elapsed.propTypes = { since: PropTypes.string };

const TroubleshootingAnswer = ({ job = null, error = null, starting = false }) => {
  if (error && (!job || job.Status !== 'COMPLETED')) {
    return (
      <Alert type="error" header="The diagnosis did not complete">
        {error}
      </Alert>
    );
  }
  if (starting || !job) {
    return <StatusIndicator type="loading">Starting the diagnosis…</StatusIndicator>;
  }
  if (job.Status === 'FAILED') {
    return (
      <Alert type="error" header="The diagnosis did not complete">
        {job.Error || 'The diagnosis could not be completed.'}
      </Alert>
    );
  }
  if (job.Status !== 'COMPLETED') {
    return (
      <StatusIndicator type="loading">
        Diagnosing — reading this deployment&apos;s logs and records. This usually takes one to four minutes.{' '}
        <Elapsed since={job.CreatedAt} />
      </StatusIndicator>
    );
  }
  return (
    <SpaceBetween size="s">
      {job.Truncated && (
        <Alert type="info">
          The agent reached one of its limits before it finished searching, so this answer rests on the evidence it had
          gathered by then.
        </Alert>
      )}
      <Box>
        <div className="troubleshooting-answer" data-testid="troubleshooting-answer">
          <ReactMarkdown rehypePlugins={markdownRehypePlugins}>{job.Answer || ''}</ReactMarkdown>
        </div>
      </Box>
    </SpaceBetween>
  );
};

TroubleshootingAnswer.propTypes = {
  job: JOB_SHAPE,
  error: PropTypes.string,
  starting: PropTypes.bool,
};

export default TroubleshootingAnswer;
