/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { useLocation } from 'react-router-dom';
import {
  Alert,
  AppLayout,
  Box,
  Container,
  Header,
  Link,
  SideNavigation,
  SpaceBetween,
} from '@cloudscape-design/components';

import CallAnalyticsTopNavigation from '../call-analytics-top-navigation';
import { appLayoutLabels } from '../common/labels';
import { NAV_HEADER } from '../common/navigation-items';
import useAppContext from '../../contexts/app';
import { HOME_PATH, SHARED_MEETINGS_PATH, VOICE_ENROLLMENT_PATH } from '../../routes/constants';

const PARTICIPANT_NAV_HEADER = { ...NAV_HEADER, href: `#${HOME_PATH}` };

/** The Participant role's navigation: its pages and the documentation links. */
export const participantNavigationItems = () => [
  { type: 'link', text: 'Home', href: `#${HOME_PATH}` },
  { type: 'link', text: 'Shared meetings', href: `#${SHARED_MEETINGS_PATH}` },
  { type: 'link', text: 'Voice enrollment', href: `#${VOICE_ENROLLMENT_PATH}` },
  {
    type: 'section',
    text: 'Resources',
    items: [
      {
        type: 'link',
        text: 'LMA Documentation',
        href: 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/',
        external: true,
      },
    ],
  },
];

/** The navigation entry a path is under, so a meeting's page marks Shared meetings. */
const activeHref = (pathname) => {
  if (pathname === SHARED_MEETINGS_PATH || pathname.startsWith(`${SHARED_MEETINGS_PATH}/`)) {
    return `#${SHARED_MEETINGS_PATH}`;
  }
  if (pathname.startsWith(VOICE_ENROLLMENT_PATH)) return `#${VOICE_ENROLLMENT_PATH}`;
  return `#${HOME_PATH}`;
};

/** The page frame of every Participant page: the top navigation, the side navigation and the content. */
export const ParticipantLayout = ({ children }) => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const { pathname } = useLocation();
  return (
    <div>
      <CallAnalyticsTopNavigation />
      <AppLayout
        headerSelector="#top-navigation"
        navigation={
          <SideNavigation
            header={PARTICIPANT_NAV_HEADER}
            items={participantNavigationItems()}
            activeHref={activeHref(pathname)}
          />
        }
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        toolsHide
        content={children}
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

ParticipantLayout.propTypes = { children: PropTypes.node.isRequired };

/** The Participant home page. */
export const ParticipantHome = () => (
  <Container header={<Header variant="h1">Welcome to Live Meeting Assistant</Header>}>
    <SpaceBetween size="s">
      <Box variant="p">
        The meetings their owners have shared with you are under{' '}
        <Link href={`#${SHARED_MEETINGS_PATH}`}>Shared meetings</Link>: their transcript, live while a meeting is in
        progress, its summary, the meeting chat, the recordings and the consent record. You can read them and ask the
        meeting assistant about them; you cannot change them.
      </Box>
      <Box variant="p">
        Under <Link href={`#${VOICE_ENROLLMENT_PATH}`}>Voice enrollment</Link> you can enroll your own voice, so the
        meetings you take part in can name you as their speaker, and delete it again.
      </Box>
    </SpaceBetween>
  </Container>
);

/** What a Participant sees on any page other than theirs. */
export const ParticipantAccessDenied = () => (
  <Box padding="xl">
    <Alert type="error" header="Access denied">
      Your account does not have access to this page. <Link href={`#${HOME_PATH}`}>Go to your home page</Link>.
    </Alert>
  </Box>
);
