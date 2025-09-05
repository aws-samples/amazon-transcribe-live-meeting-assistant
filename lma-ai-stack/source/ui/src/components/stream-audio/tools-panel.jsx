/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel } from '@awsui/components-react';

const header = <h2>Stream Audio</h2>;
const content = (
  <>
    <p>Stream an audio recording or browser source</p>
    <p>Stream an audio recording or browser source</p>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
