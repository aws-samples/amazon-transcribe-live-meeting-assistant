/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import {
  CALLS_PATH,
  MEETINGS_QUERY_PATH,
  STREAM_AUDIO_PATH,
  UPLOAD_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  BROWSER_EXTENSION_PATH,
  DESKTOP_CAPTURE_APP_PATH,
  MCP_SERVERS_PATH,
  USER_MANAGEMENT_PATH,
  CONFIGURATION_HISTORY_PATH,
  CONFIGURATION_PATH,
  VOICE_ENROLLMENT_PATH,
  DEFAULT_PATH,
} from '../../routes/constants';

export const NAV_HEADER = { text: 'Meeting Assistant', href: `#${DEFAULT_PATH}` };

/**
 * Generate consistent navigation items for all layouts.
 * @param {object} settings - LMA settings from SSM parameter store
 * @param {boolean} isAdmin - Whether the current user is an admin
 * @returns {Array} Navigation items array for SideNavigation component
 */
export const generateNavigationItems = (settings, isAdmin) => {
  const navigationItems = [
    { type: 'link', text: 'Meetings List', href: `#${CALLS_PATH}` },
    { type: 'link', text: 'Meetings Query Tool', href: `#${MEETINGS_QUERY_PATH}` },
    {
      type: 'section',
      text: 'Sources',
      items: [
        {
          type: 'link',
          text: 'Virtual Participant',
          href: `#${VIRTUAL_PARTICIPANT_PATH}`,
          external: true,
        },
        {
          type: 'link',
          text: 'Stream Audio (Mic+Browser)',
          href: `#${STREAM_AUDIO_PATH}`,
          external: true,
        },
        {
          type: 'link',
          text: 'Upload Audio (File)',
          href: `#${UPLOAD_AUDIO_PATH}`,
          external: true,
        },
        {
          type: 'link',
          text: 'Chrome Extension',
          href: `#${BROWSER_EXTENSION_PATH}`,
        },
        {
          type: 'link',
          text: 'Desktop Capture App',
          href: `#${DESKTOP_CAPTURE_APP_PATH}`,
        },
      ],
    },
  ];

  // Enrolling your own voice is self-service and open to every user, not an
  // admin task: a voiceprint is biometric data about a person, so only that
  // person can consent to it being stored. Offered only where the deployment
  // has enrollment switched on, since the portal has nothing to talk to
  // otherwise. External rather than nested, because the page is a standalone
  // task with its own layout.
  if (`${settings?.VoiceEnrollmentAvailable}` === 'true') {
    navigationItems.push({
      type: 'link',
      text: 'Enrol My Voice',
      href: `#${VOICE_ENROLLMENT_PATH}`,
      external: true,
    });
  }

  // Add Configuration section (admin only)
  if (isAdmin) {
    navigationItems.push({
      type: 'section',
      text: 'Configuration',
      items: [
        // Every section of the runtime configuration, edited from its schema.
        {
          type: 'link',
          text: 'Configuration',
          href: `#${CONFIGURATION_PATH}`,
        },
        {
          type: 'link',
          text: 'MCP Servers',
          href: `#${MCP_SERVERS_PATH}`,
        },
        {
          type: 'link',
          text: 'Configuration History',
          href: `#${CONFIGURATION_HISTORY_PATH}`,
        },
        {
          type: 'link',
          text: 'User Management',
          href: `#${USER_MANAGEMENT_PATH}`,
        },
      ],
    });
  }

  // Add Deployment Info section if settings are available
  if (settings?.StackName || settings?.Version || settings?.BuildDateTime) {
    const deploymentInfoItems = [];

    if (settings?.StackName) {
      deploymentInfoItems.push({
        type: 'link',
        text: `Stack Name: ${settings.StackName}`,
        href: '#',
      });
    }

    if (settings?.BuildDateTime) {
      let buildDateTime = settings.BuildDateTime;
      if (settings.BuildDateTime.includes('T')) {
        buildDateTime = settings.BuildDateTime.replace('T', ' ').replace('Z', '');
      }
      deploymentInfoItems.push({
        type: 'link',
        text: `Build: ${buildDateTime}`,
        href: '#',
      });
    }

    if (settings?.Version) {
      deploymentInfoItems.push({
        type: 'link',
        text: `Version: ${settings.Version}`,
        href: '#',
      });
    }

    navigationItems.push({
      type: 'section',
      text: 'Deployment Info',
      items: deploymentInfoItems,
    });
  }

  // Add Resources section
  navigationItems.push({
    type: 'section',
    text: 'Resources',
    items: [
      {
        type: 'link',
        text: 'LMA Documentation',
        href: 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant/',
        external: true,
      },
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
