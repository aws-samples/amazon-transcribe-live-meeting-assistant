// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
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
