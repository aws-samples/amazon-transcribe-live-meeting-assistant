/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';

import CallListToolsPanel from '../call-list/tools-panel';
import CallDetailsToolsPanel from '../call-details/tools-panel';

const ToolsPanel = () => {
  const { path } = useRouteMatch();

  return (
    <Switch>
      <Route exact path={path}>
        <CallListToolsPanel />
      </Route>
      <Route path={`${path}/:callId`}>
        <CallDetailsToolsPanel />
      </Route>
    </Switch>
  );
};

export default ToolsPanel;
