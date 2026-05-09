/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { Routes, Route } from 'react-router-dom';
import { AppLayout, Flashbar } from '@cloudscape-design/components';

import { CallsContext } from '../../contexts/calls';

import useNotifications from '../../hooks/use-notifications';
import useSplitPanel from '../../hooks/use-split-panel';
import useCallsGraphQlApi from '../../hooks/use-calls-graphql-api';

import CallList from '../call-list';
import CallDetails from '../call-details';
import MeetingsQueryLayout from '../meetings-query-layout';
import { appLayoutLabels } from '../common/labels';

import Navigation from './navigation';
import Breadcrumbs from './breadcrumbs';
import ToolsPanel from './tools-panel';
import SplitPanel from './calls-split-panel';

import { loadCachedDateRange } from '../call-list/calls-table-config';

import useAppContext from '../../contexts/app';

const CallAnalyticsLayout = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();

  const notifications = useNotifications();
  const [toolsOpen, setToolsOpen] = useState(false);
  const [selectedItems, setSelectedItems] = useState([]);

  // Seed the meeting-list window from any persisted value so a reload
  // honours the user's previous "Last N hours" / custom selection.
  const initialDateRange = loadCachedDateRange();

  const {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    isCallsListLoading,
    dateRange,
    setLiveTranscriptCallId,
    setIsCallsListLoading,
    setDateRange,
    sendGetTranscriptSegmentsRequest,
    totalCallCount,
    totalCallCountTruncated,
  } = useCallsGraphQlApi({ initialDateRange });

  const { splitPanelOpen, onSplitPanelToggle, splitPanelSize, onSplitPanelResize } = useSplitPanel(selectedItems);

  // eslint-disable-next-line react/jsx-no-constructed-context-values
  const callsContextValue = {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    isCallsListLoading,
    selectedItems,
    sendGetTranscriptSegmentsRequest,
    setIsCallsListLoading,
    setLiveTranscriptCallId,
    setDateRange,
    setToolsOpen,
    setSelectedItems,
    dateRange,
    toolsOpen,
    totalCallCount,
    totalCallCountTruncated,
  };

  return (
    <CallsContext.Provider value={callsContextValue}>
      <AppLayout
        headerSelector="#top-navigation"
        navigation={<Navigation />}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={<Breadcrumbs />}
        notifications={<Flashbar items={notifications} />}
        tools={<ToolsPanel />}
        toolsOpen={toolsOpen}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        splitPanelOpen={splitPanelOpen}
        onSplitPanelToggle={onSplitPanelToggle}
        splitPanelSize={splitPanelSize}
        onSplitPanelResize={onSplitPanelResize}
        splitPanel={<SplitPanel />}
        content={
          <Routes>
            <Route index element={<CallList />} />
            <Route path="query" element={<MeetingsQueryLayout />} />
            <Route path=":callId" element={<CallDetails />} />
          </Routes>
        }
        ariaLabels={appLayoutLabels}
      />
    </CallsContext.Provider>
  );
};

export default CallAnalyticsLayout;
