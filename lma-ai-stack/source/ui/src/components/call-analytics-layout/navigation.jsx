// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import { React } from 'react';
import { Route, Switch, useLocation } from 'react-router-dom';
import { SideNavigation } from '@awsui/components-react';
import { LMA_VERSION } from '../common/constants';

import {
  CALLS_PATH,
  MEETINGS_QUERY_PATH,
  STREAM_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  DEFAULT_PATH,
} from '../../routes/constants';

export const callsNavHeader = { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` };
export const callsNavItems = [
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
  {
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
  },
];

const defaultOnFollowHandler = (ev) => {
  // XXX keep the locked href for our demo pages
  // ev.preventDefault();
  console.log(ev);
};

/* eslint-disable react/prop-types */
const Navigation = ({ header = callsNavHeader, items = callsNavItems, onFollowHandler = defaultOnFollowHandler }) => {
  const location = useLocation();
  const path = location.pathname;
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
          items={items || callsNavItems}
          header={header || callsNavHeader}
          activeHref={activeHref}
          onFollow={onFollowHandler}
        />
      </Route>
    </Switch>
  );
};

export default Navigation;
