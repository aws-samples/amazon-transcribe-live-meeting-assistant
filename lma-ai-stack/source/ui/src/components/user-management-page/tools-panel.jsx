/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@cloudscape-design/components';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>User Management</h2>;
const content = (
  <>
    <p>
      Create and delete LMA users. Only <strong>Admin</strong> users can view or modify this page. New users receive an
      email from Amazon Cognito containing a temporary password and must set a new password at first sign-in.
    </p>
    <h3>Roles</h3>
    <ul>
      <li>
        <strong>Admin</strong> &mdash; full access, including this User Management page and all Configuration pages (MCP
        Servers, Nova Sonic, Transcript Summary).
      </li>
      <li>
        <strong>User</strong> &mdash; standard LMA access (Meetings, Streaming, Virtual Participant). Cannot access
        admin-only pages or APIs.
      </li>
    </ul>
    <h3>Guard rails</h3>
    <ul>
      <li>You cannot delete your own account.</li>
      <li>You cannot delete the last remaining Admin.</li>
      <li>
        If an <em>allowed email domain</em> was configured at deployment time, new user emails must match one of those
        domains.
      </li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/user-management/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> User Management Guide
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
