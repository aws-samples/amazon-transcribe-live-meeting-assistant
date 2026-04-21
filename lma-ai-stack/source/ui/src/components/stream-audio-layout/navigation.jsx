/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { SideNavigation } from '@cloudscape-design/components';
import useSettingsContext from '../../contexts/settings';
import useUserGroups from '../../hooks/use-user-groups';
import { NAV_HEADER, generateNavigationItems } from '../common/navigation-items';
import { STREAM_AUDIO_PATH } from '../../routes/constants';

export const callsNavHeader = NAV_HEADER;

const defaultOnFollowHandler = (ev) => {
  if (ev.detail.href === '#') {
    ev.preventDefault();
    return;
  }
  console.log(ev);
};

/* eslint-disable react/prop-types */
const Navigation = ({
  activeHref = `#${STREAM_AUDIO_PATH}`,
  header = callsNavHeader,
  items,
  onFollowHandler = defaultOnFollowHandler,
}) => {
  const { settings } = useSettingsContext() || {};
  const { isAdmin } = useUserGroups();

  const navigationItems = items || generateNavigationItems(settings, isAdmin);

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
