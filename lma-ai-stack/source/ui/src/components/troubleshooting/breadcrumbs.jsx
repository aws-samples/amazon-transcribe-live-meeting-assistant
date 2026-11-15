/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { DEFAULT_PATH, TROUBLESHOOTING_PATH } from '../../routes/constants';

export const troubleshootingBreadcrumbItems = [
  { text: 'Meeting Assistant', href: `#${DEFAULT_PATH}` },
  { text: 'Troubleshooting', href: `#${TROUBLESHOOTING_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={troubleshootingBreadcrumbItems} />;

export default Breadcrumbs;
