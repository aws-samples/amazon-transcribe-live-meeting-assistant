/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Desktop Capture App</h2>;
const content = (
  <>
    <p>
      The LMA Desktop Capture App is a native application that streams your microphone and your computer&apos;s system
      (meeting) audio to LMA. Because it captures operating-system audio rather than a browser tab, it can transcribe
      meetings joined from native desktop apps &mdash; Zoom, Microsoft Teams, Cisco Webex, Slack huddles, or phone
      bridges &mdash; with no bot or extra attendee.
    </p>
    <h3>Features</h3>
    <ul>
      <li>Transcribes native (non-browser) meeting apps</li>
      <li>Captures your mic (AGENT) and system/meeting audio (CALLER) as separate channels</li>
      <li>No bot or extra attendee in the meeting</li>
      <li>Preconfigured for this deployment; sign in with your LMA username/password</li>
    </ul>
    <h3>Availability</h3>
    <ul>
      <li>macOS 13+ &mdash; available now</li>
      <li>Windows &mdash; planned</li>
      <li>iPhone / iPad, Android &mdash; under consideration</li>
    </ul>
    <h3>Requirements (macOS)</h3>
    <ul>
      <li>macOS 13 (Ventura) or later</li>
      <li>Apple command-line tools (the installer prompts you if missing)</li>
      <li>Microphone and Screen Recording permissions (Screen Recording enables system-audio capture)</li>
    </ul>
    <p>
      <strong>Not sure which option to use?</strong> See the{' '}
      <a href={`${DOCS_BASE}/meeting-sources/`} target="_blank" rel="noopener noreferrer">
        <Icon name="external" /> Meeting Sources comparison
      </a>{' '}
      for a side-by-side of the Desktop Capture App, Chrome Extension, Stream Audio, and Virtual Participant.
    </p>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> LMA Documentation
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/quick-start-guide/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Quick Start Guide
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
