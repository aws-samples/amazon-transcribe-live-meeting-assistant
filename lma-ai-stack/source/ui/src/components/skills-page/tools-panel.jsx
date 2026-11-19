/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>Skills</h2>;
const content = (
  <>
    <p>
      A skill is guidance you write for the meeting assistant: a name, a description that says when it applies, Markdown
      instructions, and optional text reference files. The assistant&apos;s prompt lists each allowed skill by name and
      description, and the assistant reads a skill&apos;s instructions only when a request matches it.
    </p>
    <h3>On this page</h3>
    <ul>
      <li>Create, edit and delete skills (Admin)</li>
      <li>Import a skill from a SKILL.md, an Amazon Quick skill .md, or a .zip of a skill folder</li>
      <li>Export a skill as a SKILL.md or a .zip, and the whole library as one JSON file</li>
    </ul>
    <p>
      Skills hold text only: an import leaves out scripts and other files that are not text. Every signed-in user can
      read the skills, so do not put credentials in them. Which skills the assistant uses, for the owner&apos;s requests
      and for requests made in the meeting, is set per skill under Meeting Assistant › Tool selection on the
      Configuration page.
    </p>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/meeting-assistant-skills/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Meeting Assistant Skills
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
