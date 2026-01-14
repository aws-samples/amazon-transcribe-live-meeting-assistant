/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';

import { BreadcrumbGroup } from '@awsui/components-react';

import { MCP_SERVERS_PATH, DEFAULT_PATH } from '../../routes/constants';

export const mcpServersBreadcrumbItems = [
  { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` },
  { text: 'Configuration', href: '#' },
  { text: 'MCP Servers', href: `#${MCP_SERVERS_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={mcpServersBreadcrumbItems} />;

export default Breadcrumbs;
