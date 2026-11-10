/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Navigate, Route, useLocation } from 'react-router-dom';

import { CONFIGURATION_HISTORY_PATH, CONFIGURATION_PATH, MOVED_CONFIGURATION_PAGES } from './constants';

// The parameter that opens the revision history on the View / Edit
// Configuration page. The page reads the same name.
export const HISTORY_PARAM = 'history';

/**
 * The Configuration History page became the revision history dialog of the
 * View / Edit Configuration page: its address opens that page with the dialog
 * open, for the profile it named.
 */
const HistoryRedirect = () => {
  const params = new URLSearchParams(useLocation().search);
  params.set(HISTORY_PARAM, '1');
  return <Navigate to={`${CONFIGURATION_PATH}?${params.toString()}`} replace />;
};

/**
 * A route for each page that once edited one section of the configuration,
 * sending it to that section of the Configuration page, and one for the
 * Configuration History page, so a bookmark keeps working. Elements rather
 * than a component, because <Routes> takes <Route> children only.
 */
const movedConfigurationRoutes = () => [
  ...Object.entries(MOVED_CONFIGURATION_PAGES).map(([path, section]) => (
    <Route
      key={path}
      path={`${path}/*`}
      element={<Navigate to={`${CONFIGURATION_PATH}?section=${section}`} replace />}
    />
  )),
  <Route key={CONFIGURATION_HISTORY_PATH} path={`${CONFIGURATION_HISTORY_PATH}/*`} element={<HistoryRedirect />} />,
];

export default movedConfigurationRoutes;
