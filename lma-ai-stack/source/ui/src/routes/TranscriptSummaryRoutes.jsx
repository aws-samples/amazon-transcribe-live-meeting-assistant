/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { AppLayout, Flashbar } from '@cloudscape-design/components';
import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';
import TranscriptSummaryPage from '../components/transcript-summary-config/TranscriptSummaryPage';
import Navigation from '../components/call-analytics-layout/navigation';
import Breadcrumbs from '../components/transcript-summary-config/breadcrumbs';
import ToolsPanel from '../components/transcript-summary-config/tools-panel';
import { appLayoutLabels } from '../components/common/labels';
import useNotifications from '../hooks/use-notifications';
import useAppContext from '../contexts/app';

const TranscriptSummaryRoutes = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const notifications = useNotifications();
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
        tools={<ToolsPanel />}
        toolsOpen={toolsOpen}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        content={<TranscriptSummaryPage />}
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

export default TranscriptSummaryRoutes;
