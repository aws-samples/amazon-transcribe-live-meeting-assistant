/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@awsui/components-react';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Voice Assistant Configuration</h2>;
const content = (
  <>
    <p>
      Configure Amazon Nova Sonic voice assistant settings for real-time voice interaction during meetings. Customize
      system prompts, tool definitions, and voice parameters to tailor the assistant&apos;s behavior.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Edit the system prompt that guides the voice assistant&apos;s responses</li>
      <li>Configure tool definitions for function calling capabilities</li>
      <li>Adjust voice and speech parameters</li>
      <li>Test configuration changes before applying to live meetings</li>
      <li>Reset to default configuration at any time</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/nova-sonic-setup/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Nova Sonic Setup Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/voice-assistant/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Voice Assistant Overview
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/meeting-assistant/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Meeting Assistant
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
