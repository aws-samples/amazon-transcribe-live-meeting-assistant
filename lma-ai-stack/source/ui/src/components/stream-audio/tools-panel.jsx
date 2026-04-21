/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Stream Audio</h2>;
const content = (
  <>
    <p>
      Stream audio directly from your browser for live transcription and analysis. Use your microphone, system audio, or
      upload a recorded audio/video file to capture meeting content in real time.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Stream from browser microphone or system audio</li>
      <li>Upload and stream pre-recorded audio/video files</li>
      <li>Configure meeting name, speaker name, and language</li>
      <li>Mute/unmute microphone during streaming</li>
      <li>Real-time transcription starts automatically when streaming begins</li>
      <li>Meeting appears in the Meetings List for review after streaming ends</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/stream-audio/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Stream Audio Guide
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
