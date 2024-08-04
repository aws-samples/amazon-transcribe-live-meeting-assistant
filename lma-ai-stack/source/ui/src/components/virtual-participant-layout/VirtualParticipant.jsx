// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import React from 'react';

import { Container, Header, ColumnLayout } from '@awsui/components-react';
import '@awsui/global-styles/index.css';

// import useAppContext from '../../contexts/app';
// import useSettingsContext from '../../contexts/settings';

import MeetingForm from './MeetingForm';

const VirtualParticipant = () => {
  const test = '5';
  // const { currentSession } = useAppContext();
  // const { settings } = useSettingsContext();
  // const JWT_TOKEN = currentSession.getAccessToken().getJwtToken();
  console.log(test);

  return (
    <Container header={<Header variant="h2">Virtual Participant (Preview)</Header>}>
      <ColumnLayout columns={2}>
        <MeetingForm />
      </ColumnLayout>
    </Container>
  );
};

export default VirtualParticipant;
