/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The cells a consent given in advance is shown with, on My consents and on
 * the All consents view: its status (with an administrator's note) and the
 * exact text agreed to, with its version and hash.
 */
import React from 'react';
import { Box, ExpandableSection, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';

import { consentStatusLabel } from '../common/advance-consent';

export const when = (iso) => (iso ? new Date(iso).toLocaleString() : '-');

export const statusCell = (c) => (
  <SpaceBetween size="xxxs">
    <StatusIndicator type={c.Status === 'active' ? 'success' : 'stopped'}>
      {`${consentStatusLabel(c)}${c.WithdrawnAt ? ` ${when(c.WithdrawnAt)}` : ''}`}
    </StatusIndicator>
    {c.WithdrawnNote && (
      <Box color="text-body-secondary" fontSize="body-s">
        {c.WithdrawnNote}
      </Box>
    )}
  </SpaceBetween>
);

export const textCell = (c) => (
  <ExpandableSection headerText="Show">
    <SpaceBetween size="xxs">
      <Box variant="p">{c.NoticeText}</Box>
      <Box color="text-body-secondary" fontSize="body-s">
        {`Version ${c.NoticeVersion}; SHA-256 ${c.NoticeHash}${
          c.DeviceDetailsKept ? '; sign-in device details kept' : ''
        }`}
      </Box>
    </SpaceBetween>
  </ExpandableSection>
);
