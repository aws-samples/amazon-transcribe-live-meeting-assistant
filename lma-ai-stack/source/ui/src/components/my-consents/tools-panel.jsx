/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

/** My consents' help panel. */
const ToolsPanel = () => (
  <HelpPanel header={<h2>My consents</h2>}>
    <p>
      Meetings recorded with Live Meeting Assistant can ask everyone for consent. Here you can record your consent
      before a meeting, so a meeting that counts it does not have to ask you, and withdraw it at any time.
    </p>
    <h3>What a consent can cover</h3>
    <ul>
      <li>
        <strong>One meeting</strong>, from the consent link its organizer sends you.
      </li>
      <li>
        <strong>Meetings organized by one person</strong>, by their email address, until a date you choose, optionally
        only those with one label.
      </li>
      <li>
        <strong>All meetings on this deployment</strong>, until a date you choose.
      </li>
    </ul>
    <p>
      A consent counts in a meeting only when you are on that meeting&apos;s participant list and your name in the
      meeting matches it. The meeting still tells everyone it is recorded, and you can still object in any meeting: an
      objection always wins.
    </p>
    <h3>What is kept</h3>
    <p>
      Each consent keeps your email address, your account, when you gave it, what it covers, until when, and the exact
      text you agreed to, with its hash. Withdrawing stops it counting from then on; a meeting in progress that counted
      it treats the withdrawal as your objection.
    </p>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/meeting-consent/#consent-given-in-advance`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Consent given in advance
        </a>
      </li>
    </ul>
  </HelpPanel>
);

export default ToolsPanel;
