/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';

import { BreadcrumbGroup } from '@cloudscape-design/components';

import { SKILLS_PATH, DEFAULT_PATH } from '../../routes/constants';

export const skillsBreadcrumbItems = [
  { text: 'Meeting Assistant', href: `#${DEFAULT_PATH}` },
  { text: 'Configuration', href: '#' },
  { text: 'Skills', href: `#${SKILLS_PATH}` },
];

const Breadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={skillsBreadcrumbItems} />;

export default Breadcrumbs;
