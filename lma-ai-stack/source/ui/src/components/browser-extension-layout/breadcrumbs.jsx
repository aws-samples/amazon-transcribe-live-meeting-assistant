/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';

import { BROWSER_EXTENSION_PATH, DEFAULT_PATH } from '../../routes/constants';

export const browserExtensionBreadcrumbItems = [
  { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` },
  { text: 'Chrome Extension', href: `#${BROWSER_EXTENSION_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={browserExtensionBreadcrumbItems} />;

export default Breadcrumbs;
