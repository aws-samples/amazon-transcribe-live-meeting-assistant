/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { ASR_CONFIG_PATH, DEFAULT_PATH } from '../../routes/constants';

export const asrConfigBreadcrumbItems = [
  { text: 'Meeting Assistant', href: `#${DEFAULT_PATH}` },
  { text: 'Configuration', href: '#' },
  { text: 'ASR Config', href: `#${ASR_CONFIG_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={asrConfigBreadcrumbItems} />;

export default Breadcrumbs;
