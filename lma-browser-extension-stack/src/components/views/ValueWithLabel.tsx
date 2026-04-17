/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Box, Button, Container, ContentLayout, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';

const ValueWithLabel = ({ label, children }:any) => (
  <div>
    <Box variant="awsui-key-label">{label}</Box>
    <div>{children}</div>
  </div>
);

export default ValueWithLabel;