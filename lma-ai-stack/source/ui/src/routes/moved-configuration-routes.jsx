/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Navigate, Route } from 'react-router-dom';

import { CONFIGURATION_PATH, MOVED_CONFIGURATION_PAGES } from './constants';

/**
 * A route for each page that once edited one section of the configuration,
 * sending it to that section of the Configuration page, so a bookmark keeps
 * working. Elements rather than a component, because <Routes> takes <Route>
 * children only.
 */
const movedConfigurationRoutes = () =>
  Object.entries(MOVED_CONFIGURATION_PAGES).map(([path, section]) => (
    <Route
      key={path}
      path={`${path}/*`}
      element={<Navigate to={`${CONFIGURATION_PATH}?section=${section}`} replace />}
    />
  ));

export default movedConfigurationRoutes;
