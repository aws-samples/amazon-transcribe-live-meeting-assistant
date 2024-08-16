// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';

import { BreadcrumbGroup } from '@awsui/components-react';

import { DEFAULT_PATH, VIRTUAL_PARTICIPANT_PATH } from '../../routes/constants';

export const callListBreadcrumbItems = [
  { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` },
  { text: 'Virtual Participant (Preview)', href: `#${VIRTUAL_PARTICIPANT_PATH}` },
];

const Breadcrumbs = () => {
  const { path } = useRouteMatch();

  return (
    <Switch>
      <Route exact path={path}>
        <BreadcrumbGroup ariaLabel="Breadcrumbs" items={callListBreadcrumbItems} />
      </Route>
    </Switch>
  );
};

export default Breadcrumbs;
