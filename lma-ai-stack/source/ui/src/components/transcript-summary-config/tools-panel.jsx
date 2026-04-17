/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@awsui/components-react';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Transcript Summary Configuration</h2>;
const content = (
  <>
    <p>
      Customize the LLM prompt templates used for generating meeting summaries, action items, topics, and other
      post-meeting insights. Configure which AI model to use and fine-tune the prompts to match your organization&apos;s
      needs.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Edit prompt templates for meeting summaries and action items</li>
      <li>Configure the LLM model used for summarization</li>
      <li>Customize output format and content focus areas</li>
      <li>Preview and test prompt changes</li>
      <li>Use Lambda hook functions for advanced customization</li>
      <li>Reset to default prompt templates at any time</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/transcript-summarization/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Transcript Summarization Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/lambda-hook-functions/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Lambda Hook Functions
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
