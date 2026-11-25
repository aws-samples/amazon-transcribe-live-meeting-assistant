/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * My consents for an Admin or a User: their own consents given in advance,
 * and a meeting's consent page opened from its link. Its own layout, without
 * the meetings navigation, as the voice enrollment portal: what it shows and
 * does is the person's own.
 */
import React from 'react';
import { AppLayout, Flashbar } from '@cloudscape-design/components';

import MyConsentsPage from '../components/my-consents/MyConsentsPage';
import { appLayoutLabels } from '../components/common/labels';
import useNotifications from '../hooks/use-notifications';

const MyConsentsRoutes = () => {
  const notifications = useNotifications();

  return (
    <AppLayout
      navigationHide
      toolsHide
      notifications={<Flashbar items={notifications} />}
      content={<MyConsentsPage />}
      ariaLabels={appLayoutLabels}
    />
  );
};

export default MyConsentsRoutes;
