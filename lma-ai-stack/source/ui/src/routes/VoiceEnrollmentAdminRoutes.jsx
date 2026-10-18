/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The admin side of voice enrollment, in the usual application layout.
 *
 * Distinct from VoiceEnrollmentRoutes, which is the enrollee's own portal and
 * deliberately has no navigation: somebody invited to enrol can reach that page
 * and nothing else, including this one.
 */
import React, { useState } from 'react';
import { AppLayout, Alert, Box, Flashbar, SpaceBetween } from '@cloudscape-design/components';

import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';
import Navigation from '../components/call-analytics-layout/navigation';
import VoiceEnrollmentAdminPage from '../components/voice-enrollment/VoiceEnrollmentAdminPage';
import { appLayoutLabels } from '../components/common/labels';
import useNotifications from '../hooks/use-notifications';
import useAppContext from '../contexts/app';
import useSettingsContext from '../contexts/settings';
import useUserGroups from '../hooks/use-user-groups';

const AccessDenied = () => (
  <Box padding="xl">
    <Alert type="error" header="Access denied">
      Only <strong>Admin</strong> users can view this page.
    </Alert>
  </Box>
);

const NotEnabled = () => (
  <Box padding="xl">
    <Alert type="info" header="Voice enrollment is not enabled">
      This deployment does not store voiceprints. An administrator can turn it on by setting the
      <strong> EnableVoiceEnrollment</strong> stack parameter to <code>true</code>, which creates the enrollment table,
      the consent log and the enrollment portal. Doing so means storing biometric data about people, which carries
      obligations — see the voice enrollment documentation before enabling it.
    </Alert>
  </Box>
);

const VoiceEnrollmentAdminRoutes = () => {
  const { navigationOpen, setNavigationOpen } = useAppContext();
  const { settings } = useSettingsContext();
  const notifications = useNotifications();
  const { isAdmin } = useUserGroups();
  const [toolsOpen, setToolsOpen] = useState(false);

  const enabled = `${settings?.VoiceEnrollmentAvailable}` === 'true';

  let content = <VoiceEnrollmentAdminPage />;
  if (!isAdmin) {
    content = <AccessDenied />;
  } else if (!enabled) {
    // Checked after the admin test on purpose: a non-admin learns only that
    // they cannot see the page, not what the deployment does or does not have.
    content = <NotEnabled />;
  }

  return (
    <div>
      <CallAnalyticsTopNavigation />
      <AppLayout
        headerSelector="#top-navigation"
        navigation={<Navigation />}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        notifications={<Flashbar items={notifications} />}
        toolsHide
        toolsOpen={toolsOpen}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        content={<SpaceBetween size="l">{content}</SpaceBetween>}
        ariaLabels={appLayoutLabels}
      />
    </div>
  );
};

export default VoiceEnrollmentAdminRoutes;
