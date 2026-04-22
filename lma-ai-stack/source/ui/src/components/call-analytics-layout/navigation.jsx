/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { useLocation } from 'react-router-dom';
import { SideNavigation } from '@cloudscape-design/components';
import useSettingsContext from '../../contexts/settings';
import useUserGroups from '../../hooks/use-user-groups';
import { NAV_HEADER, generateNavigationItems } from '../common/navigation-items';

import {
  CALLS_PATH,
  MEETINGS_QUERY_PATH,
  STREAM_AUDIO_PATH,
  UPLOAD_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  MCP_SERVERS_PATH,
  NOVA_SONIC_CONFIG_PATH,
  TRANSCRIPT_SUMMARY_PATH,
  USER_MANAGEMENT_PATH,
  DEFAULT_PATH,
} from '../../routes/constants';

export const callsNavHeader = NAV_HEADER;

const defaultOnFollowHandler = (ev) => {
  if (ev.detail.href === '#') {
    ev.preventDefault();
    return;
  }
  console.log(ev);
};

const NAV_PATHS = [
  CALLS_PATH,
  MCP_SERVERS_PATH,
  NOVA_SONIC_CONFIG_PATH,
  TRANSCRIPT_SUMMARY_PATH,
  USER_MANAGEMENT_PATH,
  STREAM_AUDIO_PATH,
  UPLOAD_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  MEETINGS_QUERY_PATH,
];

/* eslint-disable react/prop-types */
const Navigation = ({ header = callsNavHeader, items, onFollowHandler = defaultOnFollowHandler }) => {
  const { settings } = useSettingsContext() || {};
  const { isAdmin } = useUserGroups();
  const location = useLocation();
  const path = location.pathname;

  const navigationItems = items || generateNavigationItems(settings, isAdmin);

  if (!NAV_PATHS.some((p) => path === p || path.startsWith(`${p}/`) || path.startsWith(`${p}?`))) {
    return null;
  }

  let activeHref = `#${DEFAULT_PATH}`;
  if (path.includes(MEETINGS_QUERY_PATH)) {
    activeHref = `#${MEETINGS_QUERY_PATH}`;
  } else if (path.includes(MCP_SERVERS_PATH)) {
    activeHref = `#${MCP_SERVERS_PATH}`;
  } else if (path.includes(NOVA_SONIC_CONFIG_PATH)) {
    activeHref = `#${NOVA_SONIC_CONFIG_PATH}`;
  } else if (path.includes(TRANSCRIPT_SUMMARY_PATH)) {
    activeHref = `#${TRANSCRIPT_SUMMARY_PATH}`;
  } else if (path.includes(USER_MANAGEMENT_PATH)) {
    activeHref = `#${USER_MANAGEMENT_PATH}`;
  } else if (path.includes(CALLS_PATH)) {
    activeHref = `#${CALLS_PATH}`;
  } else if (path.includes(STREAM_AUDIO_PATH)) {
    activeHref = `#${STREAM_AUDIO_PATH}`;
  } else if (path.includes(UPLOAD_AUDIO_PATH)) {
    activeHref = `#${UPLOAD_AUDIO_PATH}`;
  } else if (path.includes(VIRTUAL_PARTICIPANT_PATH)) {
    activeHref = `#${VIRTUAL_PARTICIPANT_PATH}`;
  }

  return (
    <SideNavigation
      items={navigationItems}
      header={header || callsNavHeader}
      activeHref={activeHref}
      onFollow={onFollowHandler}
    />
  );
};

export default Navigation;
