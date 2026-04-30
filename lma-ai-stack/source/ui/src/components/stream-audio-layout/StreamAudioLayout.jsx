/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import { AppLayout, Flashbar } from '@cloudscape-design/components';

import useNotifications from '../../hooks/use-notifications';

import StreamAudio from '../stream-audio/StreamAudio';
import { appLayoutLabels } from '../common/labels';

import Navigation from './navigation';
import Breadcrumbs from './breadcrumbs';
import ToolsPanel from './tools-panel';

import useAppContext from '../../contexts/app';

const StreamAudioLayout = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
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
      content={<StreamAudio mode="stream" />}
      ariaLabels={appLayoutLabels}
    />
  );
};

export default StreamAudioLayout;
