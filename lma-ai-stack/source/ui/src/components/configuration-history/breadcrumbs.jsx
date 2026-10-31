/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { CONFIGURATION_HISTORY_PATH, DEFAULT_PATH } from '../../routes/constants';

export const configurationHistoryBreadcrumbItems = [
  { text: 'Meeting Assistant', href: `#${DEFAULT_PATH}` },
  { text: 'Configuration', href: '#' },
  { text: 'Configuration History', href: `#${CONFIGURATION_HISTORY_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={configurationHistoryBreadcrumbItems} />;

export default Breadcrumbs;
