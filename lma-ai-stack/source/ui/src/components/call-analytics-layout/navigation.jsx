// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { React } from 'react';
import { Route, Switch, useLocation } from 'react-router-dom';
import { SideNavigation } from '@awsui/components-react';
import { LMA_VERSION } from '../common/constants';
import useSettingsContext from '../../contexts/settings';

import {
  CALLS_PATH,
  MEETINGS_QUERY_PATH,
  STREAM_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  DEFAULT_PATH,
} from '../../routes/constants';

export const callsNavHeader = { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` };

const generateNavigationItems = (settings) => {
  const navigationItems = [
    { type: 'link', text: 'Meetings List', href: `#${CALLS_PATH}` },
    { type: 'link', text: 'Meetings Query Tool', href: `#${MEETINGS_QUERY_PATH}` },
    {
      type: 'section',
      text: 'Sources',
      items: [
        {
          type: 'link',
          text: 'Download Chrome Extension',
          href: `/lma-chrome-extension-${LMA_VERSION}.zip`,
        },
        {
          type: 'link',
          text: 'Stream Audio (no extension)',
          href: `#${STREAM_AUDIO_PATH}`,
          external: true,
        },
        {
          type: 'link',
          text: 'Virtual Participant (Preview)',
          href: `#${VIRTUAL_PARTICIPANT_PATH}`,
          external: true,
        },
      ],
    },
  ];

  // Add Development Info section if settings are available
  if (settings?.StackName || settings?.Version || settings?.BuildDateTime) {
    const developmentInfoItems = [];

    if (settings?.StackName) {
      // Extract the main stack name (e.g., "LMA-6" from "LMA-6-AISTACK-1D23YP4RN3QZE")
      const mainStackName = settings.StackName.split('-AISTACK')[0] || settings.StackName;
      developmentInfoItems.push({
        type: 'link',
        text: `Stack Name: ${mainStackName}`,
        href: '#',
      });
    }

    if (settings?.BuildDateTime) {
      // Format the build date time to include both date and time
      let buildDateTime = settings.BuildDateTime;
      if (settings.BuildDateTime.includes('T')) {
        // Convert ISO format to readable format: "2025-08-20T17:23:00Z" -> "2025-08-20 17:23:00"
        buildDateTime = settings.BuildDateTime.replace('T', ' ').replace('Z', '');
      }
      developmentInfoItems.push({
        type: 'link',
        text: `Build: ${buildDateTime}`,
        href: '#',
      });
    }

    if (settings?.Version) {
      developmentInfoItems.push({
        type: 'link',
        text: `Version: ${settings.Version}`,
        href: '#',
      });
    }

    navigationItems.push({
      type: 'section',
      text: 'Development Info',
      items: developmentInfoItems,
    });
  }

  // Add Resources section
  navigationItems.push({
    type: 'section',
    text: 'Resources',
    items: [
      {
        type: 'link',
        text: 'Blog Post',
        href: 'https://www.amazon.com/live-meeting-assistant',
        external: true,
      },
      {
        type: 'link',
        text: 'Source Code',
        href: 'https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant',
        external: true,
      },
    ],
  });

  return navigationItems;
};

const defaultOnFollowHandler = (ev) => {
  // Prevent navigation for Development Info items
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
  const location = useLocation();
  const path = location.pathname;

  // Generate navigation items dynamically based on settings
  const navigationItems = items || generateNavigationItems(settings);

  let activeHref = `#${DEFAULT_PATH}`;
  if (path.includes(MEETINGS_QUERY_PATH)) {
    activeHref = `#${MEETINGS_QUERY_PATH}`;
  } else if (path.includes(CALLS_PATH)) {
    activeHref = `#${CALLS_PATH}`;
  } else if (path.includes(STREAM_AUDIO_PATH)) {
    activeHref = `#${STREAM_AUDIO_PATH}`;
  } else if (path.includes(VIRTUAL_PARTICIPANT_PATH)) {
    activeHref = `#${VIRTUAL_PARTICIPANT_PATH}`;
  }
  return (
    <Switch>
      <Route path={CALLS_PATH}>
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
