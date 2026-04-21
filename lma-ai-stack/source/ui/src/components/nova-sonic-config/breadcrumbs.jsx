/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { NOVA_SONIC_CONFIG_PATH, DEFAULT_PATH } from '../../routes/constants';

export const novaSonicConfigBreadcrumbItems = [
  { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` },
  { text: 'Configuration', href: '#' },
  { text: 'Nova Sonic Config', href: `#${NOVA_SONIC_CONFIG_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={novaSonicConfigBreadcrumbItems} />;

export default Breadcrumbs;
