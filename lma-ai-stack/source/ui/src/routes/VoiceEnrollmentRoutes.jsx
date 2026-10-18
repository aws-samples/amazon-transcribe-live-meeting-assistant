/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The enrollment portal's own layout, deliberately without the meetings
 * navigation.
 *
 * Somebody invited to enrol their voice is in the `VoiceEnrollee` group, which
 * can reach this page and nothing else. Rendering the usual side navigation
 * would show them a list of destinations that all fail, and would imply they
 * have access to meeting transcripts when they do not.
 */
import React from 'react';
import { AppLayout, Flashbar } from '@cloudscape-design/components';

import VoiceEnrollmentPage from '../components/voice-enrollment/VoiceEnrollmentPage';
import { appLayoutLabels } from '../components/common/labels';
import useNotifications from '../hooks/use-notifications';

const VoiceEnrollmentRoutes = () => {
  const notifications = useNotifications();

  return (
    <AppLayout
      navigationHide
      toolsHide
      notifications={<Flashbar items={notifications} />}
      content={<VoiceEnrollmentPage />}
      ariaLabels={appLayoutLabels}
    />
  );
};

export default VoiceEnrollmentRoutes;
