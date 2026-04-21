/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Meeting Details</h2>;
const content = (
  <>
    <p>
      View comprehensive details for a single meeting, including real-time and completed transcriptions, AI-generated
      summaries, action items, and sentiment analysis. Use the Meeting Assistant to ask questions about the
      conversation.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Live transcript with speaker identification and timestamps</li>
      <li>AI-generated meeting summaries, action items, and topics</li>
      <li>Sentiment analysis per speaker turn</li>
      <li>Translation to multiple languages</li>
      <li>Audio/video recording playback</li>
      <li>Meeting Assistant chat for real-time Q&amp;A about the meeting</li>
      <li>Download transcript and summary data</li>
      <li>Share meetings with other users</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/web-ui-guide/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Web UI Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/transcript-summarization/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Transcript Summarization
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/transcription-and-translation/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Transcription &amp; Translation
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
