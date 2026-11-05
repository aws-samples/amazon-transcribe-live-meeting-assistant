/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>View / Edit Configuration</h2>;
const content = (
  <>
    <p>
      Every setting LMA reads at run time, grouped by section. Each section starts from the defaults the release ships;
      what you change is kept over them, and everything you leave alone keeps following later releases.
    </p>
    <h3>Profiles</h3>
    <p>
      The <b>Configuration Profiles</b> list shows every profile. The <b>Active</b> one is what every meeting uses; the
      one open in the editor below is marked. Choose a profile&apos;s name to open it. <b>Managed</b> profiles,
      including the deployment default, are rewritten by every stack update, so they are read-only: create a{' '}
      <b>Custom</b> copy to change a setting, then activate the copy.
    </p>
    <p>
      Select profiles to act on them: <b>Compare profiles</b> lists every setting two or more profiles do not all hold
      alike, as saved, and can export the list as CSV or JSON; <b>Activate</b> makes one the active profile;{' '}
      <b>Delete</b> removes Custom profiles that are not active. <b>Import</b> makes a new profile of an exported file,
      and <b>Create profile</b> copies a profile as it is saved.
    </p>
    <h3>Changed values</h3>
    <p>
      A value that differs from the default is marked <b>Changed</b>, or <b>Added</b> for an entry the release does not
      ship, with a rule beside it. <b>Reset to default</b> puts back the shipped value of that one setting;{' '}
      <b>Reset section to defaults</b> does it for a whole section. <b>Actions › Reset all sections to defaults</b> does
      it for every section. A reset changes only the page until you save it.
    </p>
    <h3>Saving</h3>
    <p>
      Values are checked as you edit, against the same rules the save is checked against. A save records one revision in
      the configuration history, with an optional comment and label. If someone else saved after you loaded the page,
      the save is refused; you can then apply your changes over theirs and review the result before saving again.
    </p>
    <p>
      <b>Actions › Save current edits as new profile</b> makes a new Custom profile of what the editor holds, unsaved
      edits included, and leaves the open profile as it is saved.
    </p>
    <h3>Revisions</h3>
    <p>
      <b>Revisions…</b> opens the history of the open profile. Every save records a revision of the whole profile: who
      saved it, when, which sections it changed, and its comment. Select a revision to view, label or restore it, or two
      to compare them. Restoring saves the revision&apos;s settings as a new revision, so the configuration it replaces
      stays in the history and can be restored in turn.
    </p>
    <p>
      The 20 most recent revisions are kept, plus the current one and any that are labelled; label a revision to keep it
      however many saves follow.
    </p>
    <h3>When a change applies</h3>
    <p>
      Each section&apos;s heading says when a saved change to it takes effect, and the save dialog repeats it for the
      sections you changed.
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
