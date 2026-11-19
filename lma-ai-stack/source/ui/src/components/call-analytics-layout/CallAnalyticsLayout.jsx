/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { Routes, Route, useLocation } from 'react-router-dom';
import { AppLayout, Flashbar } from '@cloudscape-design/components';

import { CallsContext } from '../../contexts/calls';

import useNotifications from '../../hooks/use-notifications';
import useSplitPanel from '../../hooks/use-split-panel';
import useCallsGraphQlApi from '../../hooks/use-calls-graphql-api';
import useMeetingNotifications from '../../hooks/use-meeting-notifications';
import useNotificationsDrawer, { NOTIFICATIONS_DRAWER_ID } from '../../hooks/use-notifications-drawer';
import MeetingNotificationsPanel from '../meeting-notifications/MeetingNotificationsPanel';
import { meetingCallIdOf } from '../meeting-notifications/meeting-route';

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

/** The help panel's drawer, beside the Notifications drawer on a meeting page. */
const TOOLS_DRAWER_ID = 'tools';

const CallAnalyticsLayout = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const location = useLocation();

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
    sendUpdateSpeakerIdentities,
    sendResolveSpeakerNames,
    sendUpdateCallSummary,
    sendRegenerateCallSummary,
    sendUpdateCallAgenda,
    sendDeriveCallAgenda,
    sendUpdateMeetingPreparation,
    totalCallCount,
    totalCallCountTruncated,
  } = useCallsGraphQlApi({ initialDateRange });

  const { splitPanelOpen, onSplitPanelToggle, splitPanelSize, onSplitPanelResize } = useSplitPanel(selectedItems);

  // A meeting's notifications, in a drawer of their own on its page once it has
  // any (docs/meeting-notifications.md). A layout with drawers shows the help
  // panel as one of them, so it is offered as a drawer there too.
  const meetingCallId = meetingCallIdOf(location.pathname);
  const meetingNotifications = useMeetingNotifications(meetingCallId);
  const drawer = useNotificationsDrawer({
    callId: meetingCallId,
    liveArrivals: meetingNotifications.liveArrivals,
  });
  const showNotificationsDrawer = Boolean(meetingCallId) && meetingNotifications.items.length > 0;
  const visibleCount = meetingNotifications.visible.length;
  let activeDrawerId = null;
  if (toolsOpen) activeDrawerId = TOOLS_DRAWER_ID;
  else if (drawer.open) activeDrawerId = NOTIFICATIONS_DRAWER_ID;
  const drawerProps = showNotificationsDrawer
    ? {
        drawers: [
          {
            id: NOTIFICATIONS_DRAWER_ID,
            content: <MeetingNotificationsPanel notifications={meetingNotifications} />,
            trigger: { iconName: 'notification' },
            badge: drawer.badge,
            resizable: true,
            defaultSize: drawer.size,
            onResize: ({ detail }) => drawer.resize(detail.size),
            ariaLabels: {
              drawerName: `Notifications (${visibleCount})`,
              closeButton: 'Close notifications',
              triggerButton: drawer.badge ? 'Notifications, has new notifications' : 'Notifications',
              resizeHandle: 'Resize notifications',
            },
          },
          {
            id: TOOLS_DRAWER_ID,
            content: <ToolsPanel />,
            trigger: { iconName: 'status-info' },
            ariaLabels: { drawerName: 'Help', closeButton: 'Close help', triggerButton: 'Help' },
          },
        ],
        activeDrawerId,
        onDrawerChange: ({ detail }) => {
          setToolsOpen(detail.activeDrawerId === TOOLS_DRAWER_ID);
          // Opening Help in its place is not closing it: only a close of the
          // Notifications drawer itself keeps it from opening by itself again.
          if (detail.activeDrawerId === NOTIFICATIONS_DRAWER_ID) drawer.setOpen(true);
          else if (activeDrawerId === NOTIFICATIONS_DRAWER_ID && detail.activeDrawerId === null) drawer.setOpen(false);
          else drawer.hide();
        },
      }
    : {
        tools: <ToolsPanel />,
        toolsOpen,
        onToolsChange: ({ detail }) => setToolsOpen(detail.open),
      };

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
