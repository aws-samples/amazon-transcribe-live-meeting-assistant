import React, { useCallback, useEffect } from 'react';
import logo from './logo.svg';
import './Capture.css'
import { Box, Button, Container, ContentLayout, CopyToClipboard, FormField, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';
import UserMessage from '../views/UserMessage';
import OtherMessage from '../views/OtherMessage';
import { useNavigation } from '../../context/NavigationContext';
import AssistantMessage from '../views/AssistantMessage';
import ValueWithLabel from '../views/ValueWithLabel';
import { useUserContext } from '../../context/UserContext';
import { useIntegration } from '../../context/ProviderIntegrationContext';
import { useSettings } from '../../context/SettingsContext';

function Capture() {
  const { navigate } = useNavigation();
  const { logout } = useUserContext();
  const settings = useSettings();
  const { currentCall, metadata, isTranscribing, startTranscription, stopTranscription, platform } = useIntegration();

  const [topic, setTopic] = React.useState("");
  const [agentName, setAgentName] = React.useState("");
  const [nameErrorText, setNameErrorText] = React.useState("");
  const [meetingTopicErrorText, setMeetingTopicErrorText] = React.useState("");

  useEffect(() => {
    console.log("Metadata changed");
    setTopic(metadata.meetingTopic);
    setAgentName(metadata.userName);
  }, [setTopic, setAgentName]);

  const startListening = useCallback(() => {
    let foundError = false;
    if (agentName.length < 2) {
      setNameErrorText("Name required");
      foundError = true;
    }
    if (topic.length < 2) {
      setMeetingTopicErrorText("Meeting topic required");
    }
    if (foundError) {
      return;
    } else {
      startTranscription(agentName, topic);
    }
  }, [startTranscription]);

  const stopListening = useCallback(() => {
    stopTranscription();
  }, [stopTranscription]);

  const openInLMA = useCallback(async () => {
    const url = `${settings.cloudfrontEndpoint}/#/calls/${currentCall.callId}`;
    window.open(url, '_blank', 'noreferrer');
  }, [currentCall, settings])

  return (
    <ContentLayout
      header={
          <SpaceBetween size={'xs'}>
            <Header
              variant="h1"
              description="Powered by Amazon Transcribe and Q"
            >
              Amazon Live Meeting Assistant
          </Header>
        </SpaceBetween>
      }
    >
      <Container
        fitHeight={true}
        header={
          <Header variant="h2" description="">
            Meeting Details
          </Header>
        }
      >
        <SpaceBetween size="l">
          <ValueWithLabel label="Platform Detected:">{platform}</ValueWithLabel>
          {(isTranscribing === true ?
            <>
            <Button fullWidth={true} onClick={async () => openInLMA()}>Open in LMA</Button>
              <ValueWithLabel label="Name:">{agentName}</ValueWithLabel>
              <ValueWithLabel label="Meeting Topic:">{topic}</ValueWithLabel>
              <ValueWithLabel label="Active Speaker:">n/a</ValueWithLabel>
              <Button fullWidth={true} variant='primary'  onClick={() => stopListening()}>Stop Listening</Button>

            </>
            :
            <>
              <FormField
                  constraintText="Minimum length is 2 characters."
                  errorText={nameErrorText}
                  label="Your name:"
                >
                <Input value={agentName} onChange={({ detail }) => setAgentName(detail.value)} placeholder='Your name' ></Input>
              </FormField>
              <FormField
                  constraintText="Minimum length is 2 characters."
                  errorText={meetingTopicErrorText}
                  label="Meeting Topic:"
                >
                <Input value={topic} onChange={({ detail }) => setTopic(detail.value)} placeholder='Meeting room topic' inputMode='text'></Input>
              </FormField>
              <Button fullWidth={true} variant='primary'  onClick={() => startListening()}>Start Listening</Button>
            </>
          )}
          <Button fullWidth={true} onClick={() => logout()}>Log out</Button>
        </SpaceBetween>
      </Container>
    </ContentLayout>
  );
}

export default Capture;
