/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Routes, Route } from 'react-router-dom';

import CallListBreadCrumbs from '../call-list/breadcrumbs';
import CallDetailsBreadCrumbs from '../call-details/breadcrumbs';

const Breadcrumbs = () => (
  <Routes>
    <Route index element={<CallListBreadCrumbs />} />
    <Route path=":callId" element={<CallDetailsBreadCrumbs />} />
  </Routes>
);

export default Breadcrumbs;
