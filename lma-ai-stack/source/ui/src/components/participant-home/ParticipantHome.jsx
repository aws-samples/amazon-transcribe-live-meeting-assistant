/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Alert, AppLayout, Box, Container, Header, Link, SideNavigation } from '@cloudscape-design/components';

import CallAnalyticsTopNavigation from '../call-analytics-top-navigation';
import { appLayoutLabels } from '../common/labels';
import { NAV_HEADER } from '../common/navigation-items';
import useAppContext from '../../contexts/app';
import { HOME_PATH } from '../../routes/constants';

const PARTICIPANT_NAV_HEADER = { ...NAV_HEADER, href: `#${HOME_PATH}` };

/** The Participant role's navigation: its home page and the documentation links. */
export const participantNavigationItems = () => [
  { type: 'link', text: 'Home', href: `#${HOME_PATH}` },
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

/** The page frame of every Participant page: the top navigation, the side navigation and the content. */
export const ParticipantLayout = ({ children }) => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  return (
    <div>
      <CallAnalyticsTopNavigation />
      <AppLayout
        headerSelector="#top-navigation"
        navigation={
          <SideNavigation
            header={PARTICIPANT_NAV_HEADER}
            items={participantNavigationItems()}
            activeHref={`#${HOME_PATH}`}
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
    <Box variant="p">Your meetings and consents will appear here.</Box>
  </Container>
);

/** What a Participant sees on any page other than their home page. */
export const ParticipantAccessDenied = () => (
  <Box padding="xl">
    <Alert type="error" header="Access denied">
      Your account does not have access to this page. <Link href={`#${HOME_PATH}`}>Go to your home page</Link>.
    </Alert>
  </Box>
);
