// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';
import { Logger } from 'aws-amplify';

import { CALLS_PATH } from '../../routes/constants';

import CallListSplitPanel from '../call-list/CallListSplitPanel';

const logger = new Logger('CallsSplitPanel');

const CallsSplitPanel = () => {
  const { path } = useRouteMatch();
  logger.debug('path', path);
  return (
    <Switch>
      <Route exact path={CALLS_PATH}>
        <CallListSplitPanel />
      </Route>
    </Switch>
  );
};

export default CallsSplitPanel;
