/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Link } from '@awsui/components-react';

/* eslint-disable react/prop-types, jsx-a11y/anchor-is-valid */
export const InfoLink = ({ id, onFollow }) => (
  <Link variant="info" id={id} onFollow={onFollow}>
    Info
  </Link>
);

export default InfoLink;
