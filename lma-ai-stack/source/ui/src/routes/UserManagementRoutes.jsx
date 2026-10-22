/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { AppLayout, Flashbar, SpaceBetween } from '@cloudscape-design/components';

import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';
import Navigation from '../components/call-analytics-layout/navigation';
import UserManagementPage from '../components/user-management-page/UserManagementPage';
import Breadcrumbs from '../components/user-management-page/breadcrumbs';
import ToolsPanel from '../components/user-management-page/tools-panel';
import { appLayoutLabels } from '../components/common/labels';
import AdminOnly from '../components/common/AdminOnly';
import useNotifications from '../hooks/use-notifications';
import useAppContext from '../contexts/app';

const UserManagementRoutes = () => {
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
        content={
          <AdminOnly feature="user management">
            <SpaceBetween size="l">
              <UserManagementPage />
            </SpaceBetween>
          </AdminOnly>
        }
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

export default UserManagementRoutes;
