/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import { useLocation } from 'react-router-dom';
import {
  Alert,
  AppLayout,
  Box,
  BreadcrumbGroup,
  Container,
  Flashbar,
  Header,
  Link,
  SideNavigation,
  SpaceBetween,
} from '@cloudscape-design/components';

import CallAnalyticsTopNavigation from '../call-analytics-top-navigation';
import { appLayoutLabels } from '../common/labels';
import { NAV_HEADER } from '../common/navigation-items';
import { meetingTitle } from '../common/meeting-header';
import useAppContext from '../../contexts/app';
import useNotifications, { useStickyNotifications } from '../../hooks/use-notifications';
import { HOME_PATH, MY_CONSENTS_PATH, SHARED_MEETINGS_PATH, VOICE_ENROLLMENT_PATH } from '../../routes/constants';
import ParticipantToolsPanel from './tools-panel';

const PARTICIPANT_NAV_HEADER = { ...NAV_HEADER, href: `#${HOME_PATH}` };

/** The Participant role's navigation: its pages and the documentation links. */
export const participantNavigationItems = () => [
  { type: 'link', text: 'Home', href: `#${HOME_PATH}` },
  { type: 'link', text: 'Shared meetings', href: `#${SHARED_MEETINGS_PATH}` },
  { type: 'link', text: 'My consents', href: `#${MY_CONSENTS_PATH}` },
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
  if (pathname === MY_CONSENTS_PATH) return `#${MY_CONSENTS_PATH}`;
  return `#${HOME_PATH}`;
};

// A meeting id as a shared meeting's path holds it (encoded by the list's link).
const decodedCallId = (rest) => {
  try {
    return decodeURIComponent(rest);
  } catch (e) {
    return rest;
  }
};

/** The breadcrumbs of a Participant page: Home, then the page and, on a meeting's page, the meeting. */
export const participantBreadcrumbs = (pathname) => {
  const home = { text: 'Home', href: `#${HOME_PATH}` };
  const shared = { text: 'Shared meetings', href: `#${SHARED_MEETINGS_PATH}` };
  if (pathname.startsWith(`${SHARED_MEETINGS_PATH}/`)) {
    const rest = pathname.slice(SHARED_MEETINGS_PATH.length + 1);
    return [home, shared, { text: meetingTitle(decodedCallId(rest)), href: `#${pathname}` }];
  }
  if (pathname === SHARED_MEETINGS_PATH) return [home, shared];
  if (pathname === MY_CONSENTS_PATH) return [home, { text: 'My consents', href: `#${MY_CONSENTS_PATH}` }];
  if (pathname.startsWith(VOICE_ENROLLMENT_PATH)) {
    return [home, { text: 'Voice enrollment', href: `#${VOICE_ENROLLMENT_PATH}` }];
  }
  return [home];
};

/**
 * The page frame of every Participant page, as every other page has: the top
 * navigation, the side navigation, the breadcrumbs, the notifications
 * (Flashbar), the page's help panel at the right, and the content. A page may
 * give its own `breadcrumbs` and help panel (`tools`); otherwise they follow
 * the path. The help panel is opened from its (i) button or from an Info link
 * on the page, which calls `setToolsOpen` (given by the caller, or kept here).
 */
export const ParticipantLayout = ({
  children,
  breadcrumbs = undefined,
  tools = undefined,
  toolsOpen = undefined,
  setToolsOpen = undefined,
}) => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const { pathname } = useLocation();
  const notifications = useNotifications();
  const stickyNotifications = useStickyNotifications();
  const [ownToolsOpen, setOwnToolsOpen] = useState(false);
  const open = toolsOpen === undefined ? ownToolsOpen : toolsOpen;
  const setOpen = setToolsOpen || setOwnToolsOpen;
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
        breadcrumbs={
          breadcrumbs || <BreadcrumbGroup ariaLabel="Breadcrumbs" items={participantBreadcrumbs(pathname)} />
        }
        notifications={<Flashbar items={notifications} />}
        stickyNotifications={stickyNotifications}
        tools={tools || <ParticipantToolsPanel pathname={pathname} />}
        toolsOpen={open}
        onToolsChange={({ detail }) => setOpen(detail.open)}
        content={children}
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

ParticipantLayout.propTypes = {
  children: PropTypes.node.isRequired,
  breadcrumbs: PropTypes.node,
  tools: PropTypes.node,
  toolsOpen: PropTypes.bool,
  setToolsOpen: PropTypes.func,
};

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
        Under <Link href={`#${MY_CONSENTS_PATH}`}>My consents</Link> you can give consent before a meeting to its being
        recorded and transcribed, for one meeting from the consent link its organizer sends you, or for the meetings one
        organizer holds or every meeting on this deployment until a date, and withdraw it at any time.
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
