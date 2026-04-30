/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Chrome Extension</h2>;
const content = (
  <>
    <p>
      Install the LMA Chrome extension to start and stop transcription directly from inside meeting tabs on popular
      platforms such as Zoom, Microsoft Teams, Amazon Chime, Cisco Webex, and Google Meet. The extension captures both
      your microphone and remote participant audio from the active browser tab without needing to add a virtual
      participant (bot) to the meeting.
    </p>
    <h3>Features</h3>
    <ul>
      <li>One-click Start/Stop Listening from the meeting tab</li>
      <li>Captures both local and remote audio (tab audio)</li>
      <li>No bot or extra attendee visible to other participants</li>
      <li>Opens live transcript and Meeting Assistant in LMA</li>
      <li>Single sign-on using your LMA credentials</li>
    </ul>
    <h3>Requirements</h3>
    <ul>
      <li>
        You must join the meeting from the meeting platform&apos;s web client inside Chrome (or another Chromium-based
        browser). The extension cannot capture audio from native desktop or mobile meeting apps.
      </li>
    </ul>
    <p>
      <strong>Not sure which option to use?</strong> See the{' '}
      <a href={`${DOCS_BASE}/meeting-sources/`} target="_blank" rel="noopener noreferrer">
        <Icon name="external" /> Meeting Sources comparison
      </a>{' '}
      for a side-by-side of the Chrome Extension, Stream Audio, and Virtual Participant.
    </p>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/browser-extension/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Browser Extension Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/quick-start-guide/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Quick Start Guide
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
