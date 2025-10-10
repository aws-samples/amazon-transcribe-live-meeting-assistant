/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';

import CallListBreadCrumbs from '../call-list/breadcrumbs';
import CallDetailsBreadCrumbs from '../call-details/breadcrumbs';

const Breadcrumbs = () => {
  const { path } = useRouteMatch();

  return (
    <Switch>
      <Route exact path={path}>
        <CallListBreadCrumbs />
      </Route>
      <Route path={`${path}/:callId`}>
        <CallDetailsBreadCrumbs />
      </Route>
    </Switch>
  );
};

export default Breadcrumbs;
