/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';

import { BreadcrumbGroup } from '@awsui/components-react';

import { CALLS_PATH, DEFAULT_PATH } from '../../routes/constants';

export const callListBreadcrumbItems = [
  { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` },
  { text: 'Meetings', href: `#${CALLS_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={callListBreadcrumbItems} />;

export default Breadcrumbs;
