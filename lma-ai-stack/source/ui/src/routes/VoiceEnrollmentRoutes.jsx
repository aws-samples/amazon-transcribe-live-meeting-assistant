/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The enrollment portal's own layout, deliberately without the meetings
 * navigation.
 *
 * Enrolment is somebody's own business: the page shows them their own record
 * and nothing about anybody else's, and the actions on it are theirs alone.
 * The usual side navigation is hidden so the page reads as the single,
 * self-contained task it is.
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
