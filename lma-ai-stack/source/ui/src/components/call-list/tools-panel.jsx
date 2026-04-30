/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Meetings List</h2>;
const content = (
  <>
    <p>
      Browse and manage all your meetings in one place. The meetings list displays live and completed meetings with key
      metadata such as meeting name, participants, status, duration, and timestamps.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Search and filter meetings by any field using the text filter</li>
      <li>Sort meetings by column headers</li>
      <li>Select meetings to view details, share, or delete</li>
      <li>Export meeting data to Excel</li>
      <li>Live meetings update in real-time as transcription progresses</li>
      <li>Click any meeting row to drill down into full meeting details</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/web-ui-guide/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Web UI Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/quick-start-guide/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Quick Start Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/user-based-access-control/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> User-Based Access Control
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
