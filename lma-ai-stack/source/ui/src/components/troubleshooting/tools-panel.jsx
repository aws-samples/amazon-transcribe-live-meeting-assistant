/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Troubleshooting</h2>;
const content = (
  <>
    <p>
      The troubleshooting agent answers questions about why something in this deployment failed. It reads the
      deployment&apos;s CloudWatch logs, its Virtual Participant records and its stack parameters, and answers with a
      root cause, up to three recommendations, and the evidence the cause rests on. It reads only: it cannot restart,
      change or delete anything.
    </p>
    <h3>How long it takes</h3>
    <p>
      A diagnosis usually takes one to four minutes, and each one runs Amazon Bedrock and CloudWatch Logs Insights
      queries that are billed to this account. One question runs at a time.
    </p>
    <h3>Not determined</h3>
    <p>
      When the evidence does not establish a cause, the answer says so and names what is missing, rather than offering a
      likely cause.
    </p>
    <h3>Diagnosing one meeting or Virtual Participant</h3>
    <p>
      The <b>Diagnose</b> button on a meeting page or a Virtual Participant&apos;s page runs a diagnosis of that
      resource. Like this page, it is shown to Admin users only.
    </p>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/troubleshooting-agent/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> AI Troubleshooting Agent
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
