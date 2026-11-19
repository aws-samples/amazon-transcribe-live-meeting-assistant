/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { signOut, fetchUserAttributes } from 'aws-amplify/auth';
import React, { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Box, Button, Modal, SpaceBetween, TopNavigation } from '@cloudscape-design/components';
import useAppContext from '../../contexts/app';
import useUserGroups from '../../hooks/use-user-groups';
import useNotificationCount from '../../hooks/use-notification-count';
import useNotificationPreferences from '../../hooks/use-notification-preferences';
import { meetingCallIdOf, meetingHref } from '../meeting-notifications/meeting-route';
import NotificationPreferencesModal from '../meeting-notifications/NotificationPreferencesModal';

const logger = new ConsoleLogger('TopNavigation');

/* eslint-disable react/prop-types */
const SignOutModal = ({ visible, setVisible }) => {
  async function handleSignOut() {
    try {
      await signOut();
      logger.debug('signed out');
      window.location.reload();
    } catch (error) {
      logger.error('error signing out: ', error);
    }
  }
  return (
    <Modal
      onDismiss={() => setVisible(false)}
      visible={visible}
      closeAriaLabel="Close modal"
      size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={() => setVisible(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => handleSignOut()}>
              Sign Out
            </Button>
          </SpaceBetween>
        </Box>
      }
      header="Sign Out"
    >
      Sign out of the application?
    </Modal>
  );
};

const CallAnalyticsTopNavigation = () => {
  const { user, authState } = useAppContext();
  const { isAdmin } = useUserGroups();
  const [email, setEmail] = useState('');
  const [isSignOutModalVisible, setIsSignOutModalVisiblesetVisible] = useState(false);
  const [isPreferencesVisible, setIsPreferencesVisible] = useState(false);
  const location = useLocation();
  const signedIn = authState === 'authenticated' && Boolean(user?.username);
  const { mutedEverywhere, unmuteEverywhere } = useNotificationPreferences({ enabled: signedIn });
  // Meeting notifications that arrived during this visit, for meetings this
  // user owns or that are shared with them (docs/meeting-notifications.md).
  const notificationCount = useNotificationCount({
    username: signedIn ? user.username : null,
    currentCallId: meetingCallIdOf(location.pathname),
    mutedEverywhere,
  });

  useEffect(() => {
    let cancelled = false;
    const loadEmail = async () => {
      try {
        const attrs = await fetchUserAttributes();
        if (!cancelled && attrs?.email) {
          setEmail(attrs.email);
        }
      } catch (error) {
        logger.error('error fetching user attributes: ', error);
      }
    };
    if (authState === 'authenticated') {
      loadEmail();
    }
    return () => {
      cancelled = true;
    };
  }, [authState]);

  const fallbackId = user?.signInDetails?.loginId || user?.username || 'user';
  const displayEmail = email || fallbackId;
  const roleLabel = isAdmin ? 'admin' : 'user';
  const userId = `${displayEmail} (${roleLabel})`;
  return (
    <>
      <div id="top-navigation" style={{ position: 'sticky', top: 0, zIndex: 1002 }}>
        <TopNavigation
          identity={{ href: '#', title: 'Live Meeting Assistant' }}
          i18nStrings={{ overflowMenuTriggerText: 'More' }}
          utilities={[
            {
              type: 'menu-dropdown',
              iconName: 'notification',
              ariaLabel:
                notificationCount.total > 0
                  ? `Meeting notifications, ${notificationCount.total} during this visit`
                  : 'Meeting notifications',
              title: 'Meeting notifications during this visit',
              badge: notificationCount.total > 0,
              text: notificationCount.total > 0 ? String(notificationCount.total) : undefined,
              items: [
                ...(notificationCount.meetings.length === 0
                  ? [{ id: 'none', text: 'None during this visit', disabled: true }]
                  : notificationCount.meetings.map((meeting) => ({
                      id: `meeting:${meeting.callId}`,
                      text: `${meeting.callId} (${meeting.count})`,
                      href: meetingHref(meeting.callId),
                    }))),
                { id: 'notification-settings', text: 'Notification settings' },
              ],
              onItemClick: ({ detail }) => {
                if (detail.id === 'notification-settings') setIsPreferencesVisible(true);
              },
            },
            {
              type: 'menu-dropdown',
              text: userId,
              description: userId,
              iconName: 'user-profile',
              items: [
                {
                  id: 'signout',
                  type: 'button',
                  text: (
                    <Button variant="primary" onClick={() => setIsSignOutModalVisiblesetVisible(true)}>
                      Sign out
                    </Button>
                  ),
                },
                {
                  id: 'support-group',
                  text: 'Resources',
                  items: [
                    {
                      id: 'lma-documentation',
                      text: 'LMA Documentation',
                      href: 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/',
                      external: true,
                      externalIconAriaLabel: ' (opens in new tab)',
                    },
                    {
                      id: 'documentation',
                      text: 'Blog Post',
                      href: 'https://www.amazon.com/live-meeting-assistant',
                      external: true,
                      externalIconAriaLabel: ' (opens in new tab)',
                    },
                    {
                      id: 'source',
                      text: 'Source Code',
                      href: 'https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant',
                      external: true,
                      externalIconAriaLabel: ' (opens in new tab)',
                    },
                  ],
                },
              ],
            },
          ]}
        />
      </div>
      <SignOutModal visible={isSignOutModalVisible} setVisible={setIsSignOutModalVisiblesetVisible} />
      <NotificationPreferencesModal
        visible={isPreferencesVisible}
        onDismiss={() => setIsPreferencesVisible(false)}
        mutedEverywhere={mutedEverywhere}
        onUnmute={unmuteEverywhere}
      />
    </>
  );
};

export default CallAnalyticsTopNavigation;
