// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';
import { Logger } from 'aws-amplify';

import CallAnalyticsLayout from '../components/call-analytics-layout';
import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';

const logger = new Logger('CallsRoutes');

const CallsRoutes = () => {
  const { path } = useRouteMatch();
  logger.info('path ', path);

  return (
    <Switch>
      <Route path={path}>
        <div>
          <CallAnalyticsTopNavigation />
          <CallAnalyticsLayout />
        </div>
      </Route>
    </Switch>
  );
};

export default CallsRoutes;
