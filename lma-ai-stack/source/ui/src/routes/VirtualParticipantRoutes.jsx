/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';
import { Logger } from 'aws-amplify';

import VirtualParticipantLayout from '../components/virtual-participant-layout';
import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';

const logger = new Logger('VirtualParticipantRoutes');

const VirtualParticipantRoutes = () => {
  const { path } = useRouteMatch();
  logger.info('path ', path);

  return (
    <Switch>
      <Route path={path}>
        <div>
          <CallAnalyticsTopNavigation />
          <VirtualParticipantLayout />
        </div>
      </Route>
    </Switch>
  );
};

export default VirtualParticipantRoutes;
