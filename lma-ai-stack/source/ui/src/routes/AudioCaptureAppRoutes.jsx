/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import AudioCaptureAppLayout from '../components/audio-capture-app-layout';
import CallAnalyticsTopNavigation from '../components/call-analytics-top-navigation';

const AudioCaptureAppRoutes = () => (
  <div>
    <CallAnalyticsTopNavigation />
    <AudioCaptureAppLayout />
  </div>
);

export default AudioCaptureAppRoutes;
