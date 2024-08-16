// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
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
