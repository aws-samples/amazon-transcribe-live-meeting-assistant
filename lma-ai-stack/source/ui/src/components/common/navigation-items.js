/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { LMA_VERSION } from './constants';

import {
  CALLS_PATH,
  MEETINGS_QUERY_PATH,
  STREAM_AUDIO_PATH,
  UPLOAD_AUDIO_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  MCP_SERVERS_PATH,
  NOVA_SONIC_CONFIG_PATH,
  TRANSCRIPT_SUMMARY_PATH,
  USER_MANAGEMENT_PATH,
  DEFAULT_PATH,
} from '../../routes/constants';

export const NAV_HEADER = { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` };

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
          text: 'Virtual Participant (Preview)',
          href: `#${VIRTUAL_PARTICIPANT_PATH}`,
          external: true,
        },
        {
          type: 'link',
          text: 'Stream Audio (no extension)',
          href: `#${STREAM_AUDIO_PATH}`,
          external: true,
        },
        {
          type: 'link',
          text: 'Upload Audio',
          href: `#${UPLOAD_AUDIO_PATH}`,
          external: true,
        },
        {
          type: 'link',
          text: 'Download Chrome Extension',
          href: `/lma-chrome-extension-${LMA_VERSION}.zip`,
        },
      ],
    },
  ];

  // Add Configuration section (admin only)
  if (isAdmin) {
    navigationItems.push({
      type: 'section',
      text: 'Configuration',
      items: [
        {
          type: 'link',
          text: 'MCP Servers',
          href: `#${MCP_SERVERS_PATH}`,
        },
        {
          type: 'link',
          text: 'Nova Sonic Config',
          href: `#${NOVA_SONIC_CONFIG_PATH}`,
        },
        {
          type: 'link',
          text: 'Transcript Summary',
          href: `#${TRANSCRIPT_SUMMARY_PATH}`,
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
