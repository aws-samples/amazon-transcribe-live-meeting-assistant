import React, { useEffect } from 'react';
import logo from './logo.svg';
import './Capture.css'
import { Box, Button, Container, ContentLayout, CopyToClipboard, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';
import UserMessage from '../views/UserMessage';
import OtherMessage from '../views/OtherMessage';
import { useNavigation } from '../../context/NavigationContext';
import AssistantMessage from '../views/AssistantMessage';
import ValueWithLabel from '../views/ValueWithLabel';
import { useUserContext } from '../../context/UserContext';
import { useIntegration } from '../../context/ProviderIntegrationContext';

function Capture() {
  const { navigate } = useNavigation();
  const { logout } = useUserContext();
  const { metadata, isTranscribing, startTranscription, stopTranscription, platform } = useIntegration();

  const [topic, setTopic] = React.useState("");
  const [agentName, setAgentName] = React.useState("");

  useEffect(() => {
    console.log("Metadata changed");
    setTopic(metadata.meetingTopic);
    setAgentName(metadata.userName);

  }, [metadata]);

  const startListening = () => {
    startTranscription(agentName, topic);
  }

  const stopListening = () => {
    stopTranscription();
  }

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
              <ValueWithLabel label="Name:">{agentName}</ValueWithLabel>
              <ValueWithLabel label="Meeting Topic:">{topic}</ValueWithLabel>
              <ValueWithLabel label="Active Speaker:">n/a</ValueWithLabel>
              <Button fullWidth={true} variant='primary'  onClick={() => stopListening()}>Stop Listening</Button>

            </>
            :
            <>
              <ValueWithLabel label="Name:">
                <Input value={agentName} onChange={({ detail }) => setAgentName(detail.value)} placeholder='Your Name'></Input>
              </ValueWithLabel>
              <ValueWithLabel label="Meeting Topic:">
              <Input value={topic} onChange={({ detail }) => setTopic(detail.value)} placeholder='Meeting room topic'></Input>
              </ValueWithLabel>
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
