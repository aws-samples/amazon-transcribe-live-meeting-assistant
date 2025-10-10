/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
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
