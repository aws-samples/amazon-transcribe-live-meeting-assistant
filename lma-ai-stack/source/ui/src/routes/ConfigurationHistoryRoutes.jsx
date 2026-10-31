/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { AppLayout, Flashbar } from '@cloudscape-design/components';
import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';
import ConfigurationHistoryPage from '../components/configuration-history/ConfigurationHistoryPage';
import Navigation from '../components/call-analytics-layout/navigation';
import Breadcrumbs from '../components/configuration-history/breadcrumbs';
import ToolsPanel from '../components/configuration-history/tools-panel';
import { appLayoutLabels } from '../components/common/labels';
import useNotifications from '../hooks/use-notifications';
import useAppContext from '../contexts/app';

const ConfigurationHistoryRoutes = () => {
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
        content={<ConfigurationHistoryPage />}
        contentType="table"
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

export default ConfigurationHistoryRoutes;
