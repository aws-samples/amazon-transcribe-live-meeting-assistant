// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
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
