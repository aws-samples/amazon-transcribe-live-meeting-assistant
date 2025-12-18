/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';
import { AppLayout, Flashbar } from '@awsui/components-react';
import { Logger } from 'aws-amplify';

import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';
import MCPServersPage from '../components/mcp-servers-page/MCPServersPage';
import Navigation from '../components/call-analytics-layout/navigation';
import Breadcrumbs from '../components/mcp-servers-page/breadcrumbs';
import ToolsPanel from '../components/call-analytics-layout/tools-panel';
import { appLayoutLabels } from '../components/common/labels';
import useNotifications from '../hooks/use-notifications';
import useAppContext from '../contexts/app';

const logger = new Logger('MCPServersRoutes');

const MCPServersRoutes = () => {
  const { path } = useRouteMatch();
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const notifications = useNotifications();
  const [toolsOpen, setToolsOpen] = useState(false);

  logger.info('path ', path);

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
          <Switch>
            <Route path={path}>
              <MCPServersPage />
            </Route>
          </Switch>
        }
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

export default MCPServersRoutes;
