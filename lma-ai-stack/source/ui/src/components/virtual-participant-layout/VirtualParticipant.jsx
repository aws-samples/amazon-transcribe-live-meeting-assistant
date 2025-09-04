// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';

import { SpaceBetween, Container, Header, ColumnLayout } from '@awsui/components-react';
import '@awsui/global-styles/index.css';

import MeetingForm from './MeetingForm';
import VirtualParticipantList from './VirtualParticipantList';

const VirtualParticipant = () => {
  return (
    <SpaceBetween direction="vertical" size="l">
      <Container header={<Header variant="h2">Start Virtual Participant</Header>}>
        <ColumnLayout columns={1}>
          <MeetingForm />
        </ColumnLayout>
      </Container>

      <VirtualParticipantList />
    </SpaceBetween>
  );
};

export default VirtualParticipant;
