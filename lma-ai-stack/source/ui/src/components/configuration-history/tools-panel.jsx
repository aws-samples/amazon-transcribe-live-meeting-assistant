/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Configuration History</h2>;
const content = (
  <>
    <p>
      The Configuration page and the pages for single parts of it — Transcript Summary, Nova Sonic, Transcription Engine
      and the chat buttons — all save into one configuration, which also holds the meeting cost prices. Every save
      records a revision of the whole of it: who saved it, when, which parts it changed, and its comment.
    </p>
    <h3>Restoring</h3>
    <p>
      Restoring a revision saves its settings as a new revision, replacing the whole configuration, including the
      settings of every configuration page. The configuration it replaces stays in the history, so a restore can itself
      be undone by restoring the revision before it.
    </p>
    <h3>What is kept</h3>
    <p>
      The 20 most recent revisions, plus the current one and any that are labelled. Select a revision and choose{' '}
      <b>Label</b> to keep it however many saves follow. A deployment that brings nothing new records nothing, so it
      does not push your history out.
    </p>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/configuration-store/#revisions-and-audit`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Revisions and audit
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
