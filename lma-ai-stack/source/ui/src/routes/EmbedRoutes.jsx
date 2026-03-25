/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 *
 * EmbedRoutes - Route handler for the embeddable component page.
 * Renders EmbedPage without any navigation chrome (no top nav, sidebar, breadcrumbs).
 */
import React from 'react';
import { Route, Switch, useRouteMatch } from 'react-router-dom';
import { Logger } from 'aws-amplify';

import EmbedPage from '../components/embed';

const logger = new Logger('EmbedRoutes');

const EmbedRoutes = () => {
  const { path } = useRouteMatch();
  logger.info('Embed route path:', path);

  return (
    <Switch>
      <Route path={path}>
        <EmbedPage />
      </Route>
    </Switch>
  );
};

export default EmbedRoutes;
