/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { AppLayout, Alert, Box, Flashbar, SpaceBetween } from '@cloudscape-design/components';

import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';
import Navigation from '../components/call-analytics-layout/navigation';
import UserManagementPage from '../components/user-management-page/UserManagementPage';
import Breadcrumbs from '../components/user-management-page/breadcrumbs';
import ToolsPanel from '../components/user-management-page/tools-panel';
import { appLayoutLabels } from '../components/common/labels';
import useNotifications from '../hooks/use-notifications';
import useAppContext from '../contexts/app';
import useUserGroups from '../hooks/use-user-groups';

const AccessDenied = () => (
  <Box padding="xl">
    <Alert type="error" header="Access denied">
      Only <strong>Admin</strong> users can view this page.
    </Alert>
  </Box>
);

const UserManagementRoutes = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const notifications = useNotifications();
  const { isAdmin } = useUserGroups();
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
          isAdmin ? (
            <SpaceBetween size="l">
              <UserManagementPage />
            </SpaceBetween>
          ) : (
            <AccessDenied />
          )
        }
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

export default UserManagementRoutes;
