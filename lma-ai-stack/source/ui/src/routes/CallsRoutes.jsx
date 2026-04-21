/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import CallAnalyticsLayout from '../components/call-analytics-layout';
import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';

const CallsRoutes = () => (
  <div>
    <CallAnalyticsTopNavigation />
    <CallAnalyticsLayout />
  </div>
);

export default CallsRoutes;
