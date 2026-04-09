/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { React } from 'react';
import { Route, Switch, useLocation } from 'react-router-dom';
import { SideNavigation } from '@awsui/components-react';
import useSettingsContext from '../../contexts/settings';
import useAppContext from '../../contexts/app';
import { NAV_HEADER, generateNavigationItems } from '../common/navigation-items';

import {
  CALLS_PATH,
  MEETINGS_QUERY_PATH,
  STREAM_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  MCP_SERVERS_PATH,
  NOVA_SONIC_CONFIG_PATH,
  TRANSCRIPT_SUMMARY_PATH,
  DEFAULT_PATH,
} from '../../routes/constants';

export const callsNavHeader = NAV_HEADER;

const defaultOnFollowHandler = (ev) => {
  // Prevent navigation for Deployment Info items
  if (ev.detail.href === '#') {
    ev.preventDefault();
    return;
  }
  // XXX keep the locked href for our demo pages
  // ev.preventDefault();
  console.log(ev);
};

/* eslint-disable react/prop-types */
const Navigation = ({ header = callsNavHeader, items, onFollowHandler = defaultOnFollowHandler }) => {
  const { settings } = useSettingsContext() || {};
  const { user } = useAppContext();
  const location = useLocation();
  const path = location.pathname;

  // Check if user is admin
  const userGroups = user?.signInUserSession?.accessToken?.payload['cognito:groups'] || [];
  const isAdmin = userGroups.includes('Admin');

  // Generate navigation items dynamically based on settings and user role
  const navigationItems = items || generateNavigationItems(settings, isAdmin);

  let activeHref = `#${DEFAULT_PATH}`;
  if (path.includes(MEETINGS_QUERY_PATH)) {
    activeHref = `#${MEETINGS_QUERY_PATH}`;
  } else if (path.includes(MCP_SERVERS_PATH)) {
    activeHref = `#${MCP_SERVERS_PATH}`;
  } else if (path.includes(NOVA_SONIC_CONFIG_PATH)) {
    activeHref = `#${NOVA_SONIC_CONFIG_PATH}`;
  } else if (path.includes(TRANSCRIPT_SUMMARY_PATH)) {
    activeHref = `#${TRANSCRIPT_SUMMARY_PATH}`;
  } else if (path.includes(CALLS_PATH)) {
    activeHref = `#${CALLS_PATH}`;
  } else if (path.includes(STREAM_AUDIO_PATH)) {
    activeHref = `#${STREAM_AUDIO_PATH}`;
  } else if (path.includes(VIRTUAL_PARTICIPANT_PATH)) {
    activeHref = `#${VIRTUAL_PARTICIPANT_PATH}`;
  }
  return (
    <Switch>
      <Route path={[CALLS_PATH, MCP_SERVERS_PATH, NOVA_SONIC_CONFIG_PATH, TRANSCRIPT_SUMMARY_PATH]}>
        <SideNavigation
          items={navigationItems}
          header={header || callsNavHeader}
          activeHref={activeHref}
          onFollow={onFollowHandler}
        />
      </Route>
    </Switch>
  );
};

export default Navigation;
