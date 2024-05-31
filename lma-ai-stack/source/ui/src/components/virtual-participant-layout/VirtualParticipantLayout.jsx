// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React, { useState } from 'react';
import { Switch, Route, useRouteMatch } from 'react-router-dom';
import { AppLayout, Flashbar } from '@awsui/components-react';

import { Logger } from 'aws-amplify';
import useNotifications from '../../hooks/use-notifications';

import { appLayoutLabels } from '../common/labels';

import Navigation from './navigation';
import Breadcrumbs from './breadcrumbs';
import ToolsPanel from './tools-panel';

import useAppContext from '../../contexts/app';
import VirtualParticipant from './VirtualParticipant';

const logger = new Logger('VirtualParticipantLayout');

const VirtualParticipantLayout = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const { path } = useRouteMatch();
  // console.log(`StreamAudioLayout Path: ${path}`);
  logger.info('path ', path);

  const notifications = useNotifications();
  const [toolsOpen, setToolsOpen] = useState(false);

  return (
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
            <VirtualParticipant />
          </Route>
        </Switch>
      }
      ariaLabels={appLayoutLabels}
    />
  );
};

export default VirtualParticipantLayout;
