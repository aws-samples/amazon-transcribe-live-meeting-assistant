/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { BreadcrumbGroup } from '@cloudscape-design/components';
import { USER_MANAGEMENT_PATH } from '../../routes/constants';

const Breadcrumbs = () => (
  <BreadcrumbGroup
    items={[
      { text: 'Meeting Analytics', href: '#/' },
      { text: 'User Management', href: `#${USER_MANAGEMENT_PATH}` },
    ]}
    ariaLabel="Breadcrumbs"
  />
);

export default Breadcrumbs;
