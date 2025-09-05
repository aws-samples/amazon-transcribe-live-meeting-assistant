/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';

import StreamAudioToolsPanel from '../stream-audio/tools-panel';

const ToolsPanel = () => {
  const { path } = useRouteMatch();

  return (
    <Switch>
      <Route exact path={path}>
        <StreamAudioToolsPanel />
      </Route>
    </Switch>
  );
};

export default ToolsPanel;
