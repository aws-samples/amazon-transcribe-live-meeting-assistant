/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { useParams } from 'react-router-dom';

import { BreadcrumbGroup } from '@awsui/components-react';

import { CALLS_PATH } from '../../routes/constants';
import { callListBreadcrumbItems } from '../call-list/breadcrumbs';

const Breadcrumbs = () => {
  const { callId } = useParams();
  const callDetailsBreadcrumbItems = [...callListBreadcrumbItems, { text: callId, href: `#${CALLS_PATH}/${callId}` }];

  return <BreadcrumbGroup ariaLabel="Breadcrumbs" items={callDetailsBreadcrumbItems} />;
};

export default Breadcrumbs;
