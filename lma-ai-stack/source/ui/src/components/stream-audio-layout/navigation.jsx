/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Route, Switch } from 'react-router-dom';
import { SideNavigation } from '@awsui/components-react';
import useSettingsContext from '../../contexts/settings';
import useAppContext from '../../contexts/app';
import { NAV_HEADER, generateNavigationItems } from '../common/navigation-items';
import { STREAM_AUDIO_PATH } from '../../routes/constants';

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
const Navigation = ({
  activeHref = `#${STREAM_AUDIO_PATH}`,
  header = callsNavHeader,
  items,
  onFollowHandler = defaultOnFollowHandler,
}) => {
  const { settings } = useSettingsContext() || {};
  const { user } = useAppContext();

  // Check if user is admin
  const userGroups = user?.signInUserSession?.accessToken?.payload['cognito:groups'] || [];
  const isAdmin = userGroups.includes('Admin');

  const navigationItems = items || generateNavigationItems(settings, isAdmin);

  return (
    <Switch>
      <Route path={STREAM_AUDIO_PATH}>
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
