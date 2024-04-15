// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
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
