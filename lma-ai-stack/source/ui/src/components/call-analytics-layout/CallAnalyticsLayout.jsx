/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { Routes, Route, useLocation } from 'react-router-dom';
import { AppLayout, Flashbar } from '@cloudscape-design/components';

import { CallsContext } from '../../contexts/calls';

import useNotifications, { useStickyNotifications } from '../../hooks/use-notifications';
import useSplitPanel from '../../hooks/use-split-panel';
import useCallsGraphQlApi from '../../hooks/use-calls-graphql-api';
import useMeetingNotifications from '../../hooks/use-meeting-notifications';
import useNotificationsDrawer from '../../hooks/use-notifications-drawer';
import MeetingNotificationsPanel from '../meeting-notifications/MeetingNotificationsPanel';
import { meetingCallIdOf } from '../meeting-notifications/meeting-route';

import CallList from '../call-list';
import CallDetails from '../call-details';
import MeetingsQueryLayout from '../meetings-query-layout';
import { appLayoutLabels } from '../common/labels';

import Navigation from './navigation';
import Breadcrumbs from './breadcrumbs';
import ToolsPanel from './tools-panel';
import layoutDrawerProps from './layout-drawers';
import SplitPanel from './calls-split-panel';

import { loadCachedDateRange } from '../call-list/calls-table-config';

import useAppContext from '../../contexts/app';

const CallAnalyticsLayout = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const location = useLocation();

  const notifications = useNotifications();
  const stickyNotifications = useStickyNotifications();
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
    sendUpdateSpeakerIdentities,
    sendResolveSpeakerNames,
    sendUpdateCallSummary,
    sendRegenerateCallSummary,
    sendUpdateCallAgenda,
    sendDeriveCallAgenda,
    sendUpdateMeetingPreparation,
    sendRequestMeetingDigest,
    totalCallCount,
    totalCallCountTruncated,
  } = useCallsGraphQlApi({ initialDateRange });

  const { splitPanelOpen, onSplitPanelToggle, splitPanelSize, onSplitPanelResize } = useSplitPanel(selectedItems);

  // A meeting's notifications, in a drawer of their own on its page once it has
  // any (docs/meeting-notifications.md), beside Help, which is a drawer on
  // every page (see layout-drawers.js).
  const meetingCallId = meetingCallIdOf(location.pathname);
  const meetingNotifications = useMeetingNotifications(meetingCallId);
  const drawer = useNotificationsDrawer({
    callId: meetingCallId,
    liveArrivals: meetingNotifications.liveArrivals,
    // While Help is shown, an arrival marks the trigger rather than opening it.
    covered: toolsOpen,
  });
  const drawerProps = layoutDrawerProps({
    drawer,
    showNotifications: Boolean(meetingCallId) && meetingNotifications.items.length > 0,
    visibleCount: meetingNotifications.visible.length,
    notificationsContent: <MeetingNotificationsPanel notifications={meetingNotifications} />,
    toolsContent: <ToolsPanel />,
    toolsOpen,
    setToolsOpen,
  });

  // eslint-disable-next-line react/jsx-no-constructed-context-values
  const callsContextValue = {
    calls,
    callTranscriptPerCallId,
    getCallDetailsFromCallIds,
    isCallsListLoading,
    selectedItems,
    sendGetTranscriptSegmentsRequest,
    sendUpdateSpeakerIdentities,
    sendResolveSpeakerNames,
    sendUpdateCallSummary,
    sendRegenerateCallSummary,
    sendUpdateCallAgenda,
    sendDeriveCallAgenda,
    sendUpdateMeetingPreparation,
    sendRequestMeetingDigest,
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
        stickyNotifications={stickyNotifications}
        // eslint-disable-next-line react/jsx-props-no-spreading
        {...drawerProps}
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
