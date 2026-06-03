/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Routes, Route, useParams } from 'react-router-dom';

import { BreadcrumbGroup } from '@cloudscape-design/components';

import { DEFAULT_PATH, VIRTUAL_PARTICIPANT_PATH } from '../../routes/constants';

// Two crumbs on the VP list page (Meeting Assistant > Virtual Participant).
// Cloudscape renders the last crumb as plain text, so on the list page
// "Virtual Participant" is the non-clickable current location.
export const vpListBreadcrumbItems = [
  { text: 'Meeting Assistant', href: `#${DEFAULT_PATH}` },
  { text: 'Virtual Participant', href: `#${VIRTUAL_PARTICIPANT_PATH}` },
];

const VPListBreadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={vpListBreadcrumbItems} />;

const VPDetailsBreadcrumbs = () => {
  const { vpId } = useParams();
  const items = [
    ...vpListBreadcrumbItems,
    { text: vpId, href: `#${VIRTUAL_PARTICIPANT_PATH}/${encodeURIComponent(vpId)}` },
  ];
  return <BreadcrumbGroup ariaLabel="Breadcrumbs" items={items} />;
};

// Wrap in <Routes> so the detail-page variant can read the :vpId param.
// Mirrors the call-analytics-layout/breadcrumbs.jsx pattern.
const Breadcrumbs = () => (
  <Routes>
    <Route index element={<VPListBreadcrumbs />} />
    <Route path=":vpId" element={<VPDetailsBreadcrumbs />} />
  </Routes>
);

// Backwards-compat: some consumers may import the old name.
export const callListBreadcrumbItems = vpListBreadcrumbItems;

export default Breadcrumbs;
