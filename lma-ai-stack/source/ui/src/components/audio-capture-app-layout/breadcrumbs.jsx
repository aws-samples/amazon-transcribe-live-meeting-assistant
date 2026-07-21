/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';

import { AUDIO_CAPTURE_APP_PATH, DEFAULT_PATH } from '../../routes/constants';

export const audioCaptureAppBreadcrumbItems = [
  { text: 'Meeting Assistant', href: `#${DEFAULT_PATH}` },
  { text: 'Audio Capture App', href: `#${AUDIO_CAPTURE_APP_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={audioCaptureAppBreadcrumbItems} />;

export default Breadcrumbs;
