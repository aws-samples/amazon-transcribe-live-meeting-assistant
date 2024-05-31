import React, { useState } from 'react';

import {
  Form,
  SpaceBetween,
  FormField,
  Input,
  Select,
  Button,
  TimeInput,
  Alert,
  DatePicker,
} from '@awsui/components-react';

const MeetingForm = () => {
  const [meetingPlatform, setMeetingPlatform] = useState('');
  const [meetingId, setMeetingId] = useState('');
  const [meetingPassword, setMeetingPassword] = useState('');
  const [meetingName, setMeetingName] = useState('');
  const [meetingDate, setMeetingDate] = useState('');
  const [meetingTime, setMeetingTime] = useState('');

  const [meetingTimeError, setMeetingTimeError] = useState('');

  const meetingPlatforms = [
    { label: 'Amazon Chime', disabled: false, value: 'Chime' },
    { label: 'Zoom', disabled: false, value: 'Zoom' },
    { label: 'Microsoft Teams', disabled: true, value: 'Teams' },
    { label: 'Google Meet', disabled: true, value: 'Meet' },
  ];

  const validateMeetingTime = (time) => {
    if (!time) {
      setMeetingTimeError('');
    } else if (time.length !== 5) {
      setMeetingTimeError('Meeting time is incomplete.');
    } else {
      const meetingDateTime = new Date(meetingDate);
      meetingDateTime.setDate(meetingDateTime.getDate() + 1);
      const [hour, minute] = time.split(':').map(Number);
      meetingDateTime.setHours(hour, minute, 0, 0);

      const minuteDifference = (meetingDateTime.getTime() - new Date().getTime()) / (1000 * 60);

      if (minuteDifference >= 2) {
        setMeetingTimeError('');
      } else {
        setMeetingTimeError('Meeting time must be at least two minutes out from now.');
      }
    }
  };

  const submitMeetingForm = () => {
    let meetingDateTimeFormatted = '';
    if (meetingTime) {
      const dateStr = `${meetingDate}T${meetingTime}`;
      const meetingDateTime = new Date(dateStr);
      meetingDateTime.setMinutes(meetingDateTime.getMinutes() - 2);
      meetingDateTimeFormatted = meetingDateTime.toISOString().slice(0, -5);
    }

    const meeting = {
      meetingPlatform: meetingPlatform.value,
      meetingID: meetingId.replace(/ /g, ''),
      meetingPassword,
      meetingName,
      meetingTime: meetingDateTimeFormatted,
    };
    console.log(meeting);
  };

  return (
    <form
      id="meetingForm"
      onSubmit={(e) => {
        e.preventDefault();
        submitMeetingForm();
      }}
    >
      <Form variant="embedded">
        <SpaceBetween direction="vertical" size="l">
          <FormField label="Meeting Name">
            <Input onChange={({ detail }) => setMeetingName(detail.value)} value={meetingName} />
          </FormField>
          <FormField label="Meeting Platform">
            <Select
              onChange={({ detail }) => setMeetingPlatform(detail.selectedOption)}
              options={meetingPlatforms}
              selectedOption={meetingPlatform}
            />
          </FormField>

          <FormField label="Meeting ID">
            <Input onChange={({ detail }) => setMeetingId(detail.value)} value={meetingId} />
          </FormField>

          <FormField label="Meeting Password">
            <Input
              onChange={({ detail }) => setMeetingPassword(detail.value)}
              value={meetingPassword}
              type="password"
            />
          </FormField>

          <FormField
            label="Meeting Time"
            description="Choose a date and local time that is at least two minutes out from now."
          >
            <SpaceBetween direction="horizontal" size="l">
              <DatePicker
                onChange={({ detail }) => setMeetingDate(detail.value)}
                onBlur={() => validateMeetingTime(meetingTime)}
                value={meetingDate}
                isDateEnabled={(date) => {
                  const currentDate = new Date() - 1;
                  return date > currentDate;
                }}
                placeholder="YYYY/MM/DD"
                controlId="date"
              />
              <TimeInput
                onChange={({ detail }) => setMeetingTime(detail.value)}
                onBlur={() => validateMeetingTime(meetingTime)}
                value={meetingTime}
                disabled={meetingDate.length !== 10}
                format="hh:mm"
                placeholder="hh:mm (24-hour format)"
                use24Hour
              />
            </SpaceBetween>
            {meetingTimeError && <Alert type="error">{meetingTimeError}</Alert>}
          </FormField>

          <FormField>
            <SpaceBetween direction="horizontal" size="l">
              <Button variant="normal" form="meetingForm" disabled={!meetingId || !meetingName}>
                Invite Now
              </Button>
              <Button
                variant="primary"
                form="meetingForm"
                disabled={!meetingId || !meetingName || !meetingTime || !!meetingTimeError}
              >
                Invite Later
              </Button>
            </SpaceBetween>
          </FormField>
        </SpaceBetween>
      </Form>
    </form>
  );
};
export default MeetingForm;
