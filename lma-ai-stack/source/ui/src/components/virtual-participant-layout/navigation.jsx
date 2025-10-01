/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Route, Switch } from 'react-router-dom';
import { SideNavigation } from '@awsui/components-react';
import {
  CALLS_PATH,
  MEETINGS_QUERY_PATH,
  DEFAULT_PATH,
  STREAM_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
} from '../../routes/constants';
import { LMA_VERSION } from '../common/constants';

export const callsNavHeader = { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` };
export const callsNavItems = [
  { type: 'link', text: 'Meetings', href: `#${CALLS_PATH}` },
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
const Navigation = ({
  activeHref = `#${VIRTUAL_PARTICIPANT_PATH}`,
  header = callsNavHeader,
  items = callsNavItems,
  onFollowHandler = defaultOnFollowHandler,
}) => (
  <Switch>
    <Route path={VIRTUAL_PARTICIPANT_PATH}>
      <SideNavigation
        items={items || callsNavItems}
        header={header || callsNavHeader}
        activeHref={activeHref || `#${STREAM_AUDIO_PATH}`}
        onFollow={onFollowHandler}
      />
    </Route>
  </Switch>
);

export default Navigation;
