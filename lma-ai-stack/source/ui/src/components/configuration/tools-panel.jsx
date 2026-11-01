/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Configuration</h2>;
const content = (
  <>
    <p>
      Every setting LMA reads at run time, grouped by section. Each section starts from the defaults the release ships;
      what you change is kept over them, and everything you leave alone keeps following later releases.
    </p>
    <h3>Changed values</h3>
    <p>
      A value that differs from the default is marked <b>Changed</b>, or <b>Added</b> for an entry the release does not
      ship, with a rule beside it. <b>Reset to default</b> puts back the shipped value of that one setting;{' '}
      <b>Reset section to defaults</b> does it for a whole section.
    </p>
    <h3>Saving</h3>
    <p>
      Values are checked as you edit, against the same rules the save is checked against. A save records one revision in
      the configuration history, with an optional comment and label. If someone else saved after you loaded the page,
      the save is refused; you can then apply your changes over theirs and review the result before saving again.
    </p>
    <h3>When a change applies</h3>
    <p>
      Each section says so: Nova Sonic settings at the next meeting the voice assistant joins, chat buttons in chat
      panels opened after the save, the transcription engine within 30 seconds, and the summary prompts and meeting cost
      prices within about five minutes.
    </p>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/configuration-guide/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Configuration Guide
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/configuration-store/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Unified Configuration Store
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
