/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@awsui/components-react';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Virtual Participant</h2>;
const content = (
  <>
    <p>
      Send an AI-powered virtual participant (bot) to join meetings on platforms like Zoom, Microsoft Teams, Google
      Meet, Amazon Chime, and others. The bot captures audio for real-time transcription without requiring browser audio
      streaming.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Join meetings on popular platforms by pasting the meeting link</li>
      <li>Monitor active virtual participants and their connection status</li>
      <li>View status timeline showing join, recording, and leave events</li>
      <li>Optionally view the bot&apos;s screen via VNC viewer</li>
      <li>Stop or remove virtual participants from active meetings</li>
      <li>Transcription and analysis happen automatically once the bot joins</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/virtual-participant/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Virtual Participant Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/web-ui-guide/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Web UI Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/transcription-and-translation/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Transcription &amp; Translation
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
