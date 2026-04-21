/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Meetings Query Tool</h2>;
const content = (
  <>
    <p>
      Ask natural language questions about your meeting transcripts using an Amazon Bedrock Knowledge Base. Get
      AI-powered answers with references to specific meetings and conversation excerpts.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Natural language Q&amp;A over your meeting transcripts</li>
      <li>AI-generated answers with source citations from specific meetings</li>
      <li>Conversational follow-up questions with session context</li>
      <li>Click meeting references to navigate directly to meeting details</li>
      <li>Requires Transcript Knowledge Base to be enabled during deployment</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/meetings-query-tool/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Meetings Query Tool Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/web-ui-guide/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Web UI Guide
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
