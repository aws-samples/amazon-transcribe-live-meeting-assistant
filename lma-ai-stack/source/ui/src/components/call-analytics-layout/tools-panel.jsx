/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Routes, Route } from 'react-router-dom';

import CallListToolsPanel from '../call-list/tools-panel';
import CallDetailsToolsPanel from '../call-details/tools-panel';
import MeetingsQueryToolsPanel from '../meetings-query-layout/tools-panel';

const ToolsPanel = () => (
  <Routes>
    <Route index element={<CallListToolsPanel />} />
    <Route path="query" element={<MeetingsQueryToolsPanel />} />
    <Route path=":callId" element={<CallDetailsToolsPanel />} />
  </Routes>
);

export default ToolsPanel;
