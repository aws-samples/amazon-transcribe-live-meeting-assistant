/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel } from '@awsui/components-react';

const header = <h2>Meetings</h2>;
const content = (
  <>
    <p>View a list of meetings and related information.</p>
    <p>Use the search bar to filter on any field.</p>
    <p>To drill down even further into the details, select an individual meeting.</p>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
