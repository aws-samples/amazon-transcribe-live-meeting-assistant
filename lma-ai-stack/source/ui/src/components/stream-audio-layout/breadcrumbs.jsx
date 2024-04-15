// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';

import StreamAudioBreadcrumbs from '../stream-audio/breadcrumbs';

const Breadcrumbs = () => {
  const { path } = useRouteMatch();

  return (
    <Switch>
      <Route exact path={path}>
        <StreamAudioBreadcrumbs />
      </Route>
    </Switch>
  );
};

export default Breadcrumbs;
