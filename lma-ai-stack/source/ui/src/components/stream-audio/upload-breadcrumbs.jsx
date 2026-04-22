/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';

import { BreadcrumbGroup } from '@cloudscape-design/components';

import { UPLOAD_AUDIO_PATH, DEFAULT_PATH } from '../../routes/constants';

export const uploadAudioBreadcrumbItems = [
  { text: 'Meeting Analytics', href: `#${DEFAULT_PATH}` },
  { text: 'Upload Audio', href: `#${UPLOAD_AUDIO_PATH}` },
];

const UploadBreadcrumbs = () => <BreadcrumbGroup ariaLabel="Breadcrumbs" items={uploadAudioBreadcrumbItems} />;

export default UploadBreadcrumbs;
