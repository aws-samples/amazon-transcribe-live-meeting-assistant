/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * My consents for an Admin or a User: their own consents given in advance,
 * and a meeting's consent page opened from its link, in the app's standard
 * layout (top navigation, side navigation, notifications, breadcrumbs and the
 * help panel). A Participant's My consents is in ParticipantRoutes, in the
 * same layout with the Participant's navigation.
 */
import React, { useState } from 'react';
import { AppLayout, Flashbar } from '@cloudscape-design/components';

import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';
import Navigation from '../components/call-analytics-layout/navigation';
import MyConsentsPage from '../components/my-consents/MyConsentsPage';
import Breadcrumbs from '../components/my-consents/breadcrumbs';
import ToolsPanel from '../components/my-consents/tools-panel';
import { appLayoutLabels } from '../components/common/labels';
import useNotifications, { useStickyNotifications } from '../hooks/use-notifications';
import useAppContext from '../contexts/app';

const MyConsentsRoutes = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const notifications = useNotifications();
  const stickyNotifications = useStickyNotifications();
  const [toolsOpen, setToolsOpen] = useState(false);

  return (
    <div>
      <CallAnalyticsTopNavigation />
      <AppLayout
        headerSelector="#top-navigation"
        navigation={<Navigation />}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={<Breadcrumbs />}
        notifications={<Flashbar items={notifications} />}
        stickyNotifications={stickyNotifications}
        tools={<ToolsPanel />}
        toolsOpen={toolsOpen}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        content={<MyConsentsPage />}
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

export default MyConsentsRoutes;
