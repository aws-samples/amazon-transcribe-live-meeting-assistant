/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { Switch, Route, useRouteMatch } from 'react-router-dom';
import { AppLayout, Flashbar } from '@awsui/components-react';

import { Logger } from 'aws-amplify';
import useNotifications from '../../hooks/use-notifications';

import StreamAudio from '../stream-audio/StreamAudio';
import { appLayoutLabels } from '../common/labels';

import Navigation from './navigation';
import Breadcrumbs from './breadcrumbs';
import ToolsPanel from './tools-panel';

import useAppContext from '../../contexts/app';

const logger = new Logger('StreamAudioLayout');

const StreamAudioLayout = () => {
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
            <StreamAudio />
          </Route>
        </Switch>
      }
      ariaLabels={appLayoutLabels}
    />
  );
};

export default StreamAudioLayout;
