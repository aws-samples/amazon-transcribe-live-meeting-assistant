import React from 'react';
import { Box, Button, Container, ContentLayout, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';

const ValueWithLabel = ({ label, children }:any) => (
  <div>
    <Box variant="awsui-key-label">{label}</Box>
    <div>{children}</div>
  </div>
);

export default ValueWithLabel;