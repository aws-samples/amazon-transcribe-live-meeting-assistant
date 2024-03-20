import React from 'react';
import logo from './logo.svg';
import './Meeting.css'
import { Box, Button, Container, ContentLayout, CopyToClipboard, Grid, Header, Input, Link, SpaceBetween } from '@cloudscape-design/components';
import UserMessage from '../views/UserMessage';
import OtherMessage from '../views/OtherMessage';
import { useNavigation } from '../../context/NavigationContext';
import AssistantMessage from '../views/AssistantMessage';

function Meeting() {
  const { navigate } = useNavigation();

  const [input, setInput] = React.useState("");

  return (
    <ContentLayout
      header={
        <SpaceBetween size="xs" direction='vertical'>
          <SpaceBetween size={'xxxs'}>
            <Header
              variant="h1"
              description="With Amazon Q for Business"
            >
              Amazon Live Meeting Assistant
            </Header>
          </SpaceBetween>

          <SpaceBetween direction="horizontal" size="xs">
            <Button variant='primary'>Start Listening</Button>
            <Button onClick={() => navigate('login')}>Log out</Button>
          </SpaceBetween>
        </SpaceBetween>
      }
    >
      <Container
        fitHeight={true}
        header={
          <Header variant="h2" description="with Amazon Q">
            Meeting Transcript
          </Header>
        }
        footer={
          <SpaceBetween size={'xs'}>
            <Input value={input} placeholder='Ask a question'></Input>
            <Grid gridDefinition={[{ colspan: 10, offset:1 }]}>
              <Button fullWidth={true}>Summarize the meeting</Button>
            </Grid>
          </SpaceBetween>
        }
      >
        <div className='contentPlaceholder'>
          <SpaceBetween size={'s'}>
            <UserMessage/>
            <AssistantMessage responseTo='AGENT' message='For Linux on-demand instances, current rates start at $1.232 per hour.'/>
            <OtherMessage/>
            <UserMessage/>
            <OtherMessage/>
            <UserMessage/>
            <OtherMessage/>
            <UserMessage/>
            <OtherMessage/>
            <UserMessage/>
            <OtherMessage/>
            <UserMessage/>
          </SpaceBetween>
        </div>
      </Container>
    </ContentLayout>
  );
}

export default Meeting;
