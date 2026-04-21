/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';

import { BreadcrumbGroup } from '@cloudscape-design/components';

import { MEETINGS_QUERY_PATH, DEFAULT_PATH } from '../../routes/constants';

export const meetingsQueryBreadcrumbItems = [
  { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` },
  { text: 'Meetings Query Tool', href: `#${MEETINGS_QUERY_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={meetingsQueryBreadcrumbItems} />;

export default Breadcrumbs;
