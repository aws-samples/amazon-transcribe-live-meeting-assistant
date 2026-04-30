/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import BrowserExtensionLayout from '../components/browser-extension-layout';
import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';

const BrowserExtensionRoutes = () => (
  <div>
    <CallAnalyticsTopNavigation />
    <BrowserExtensionLayout />
  </div>
);

export default BrowserExtensionRoutes;
