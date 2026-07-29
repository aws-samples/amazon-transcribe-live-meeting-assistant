/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';

import { DESKTOP_CAPTURE_APP_PATH, DEFAULT_PATH } from '../../routes/constants';

export const desktopCaptureAppBreadcrumbItems = [
  { text: 'Meeting Assistant', href: `#${DEFAULT_PATH}` },
  { text: 'Desktop Capture App', href: `#${DESKTOP_CAPTURE_APP_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={desktopCaptureAppBreadcrumbItems} />;

export default Breadcrumbs;
