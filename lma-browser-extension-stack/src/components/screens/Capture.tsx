/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useCallback, useEffect } from 'react';
import logo from './logo.svg';
import './Capture.css'
import { Alert, Box, Button, Checkbox, Container, ContentLayout, CopyToClipboard, FormField, Grid, Header, Icon, Input, Link, Modal, SpaceBetween } from '@cloudscape-design/components';
import UserMessage from '../views/UserMessage';
import OtherMessage from '../views/OtherMessage';
import { useNavigation } from '../../context/NavigationContext';
import AssistantMessage from '../views/AssistantMessage';
import ValueWithLabel from '../views/ValueWithLabel';
import { useUserContext } from '../../context/UserContext';
import { useIntegration } from '../../context/ProviderIntegrationContext';
import { useSettings } from '../../context/SettingsContext';
import { nameFromIdToken } from '../../lib/identity';
import { prefillField } from '../../lib/capture';
import { INVALID_MEETING_ID, meetingIdFrom } from '../../lib/continue-meeting';
import ConsentPanel from '../views/ConsentPanel';
import {
  ConsentSettings,
  ConsentStep,
  consentPayload,
  consentRequired,
  describeConsentState,
  emptyStep,
  fetchConsentSettings,
  renderNotice,
  startBlockedReason,
  transcriberUrl,
} from '../../lib/consent';

function Capture() {
  const { navigate } = useNavigation();
  const { user, logout } = useUserContext();
  const settings = useSettings();
  const { currentCall, muted, setMuted, paused, setPaused, activeSpeaker, metadata, fetchMetadata, isTranscribing, startTranscription, stopTranscription, platform, startError, setStartError, consentFrame, postInMeetingChat, reportObjection } = useIntegration();
  // "Continue an existing meeting…" (lib/continue-meeting.ts).
  const [continuing, setContinuing] = React.useState(false);
  const [continueInput, setContinueInput] = React.useState("");
  const [continueErrorText, setContinueErrorText] = React.useState("");

  const [topic, setTopic] = React.useState("");
  const [agentName, setAgentName] = React.useState("");
  const [nameErrorText, setNameErrorText] = React.useState("");
  const [meetingTopicErrorText, setMeetingTopicErrorText] = React.useState("");
  const [showDisclaimer, setShowDisclaimer] = React.useState(false);
  // What we last prefilled these two fields with. A field still holding our own
  // prefill is ours to refresh when a reader reports again or the user switches
  // to another meeting tab; a field holding anything else is theirs, and the
  // readers report often enough that overwriting it would eat what they typed.
  const prefilled = React.useRef({ topic: "", agentName: "" });

  // The consent step (lib/consent.ts): the transcriber's settings, read on a
  // short connection when the panel opens and again after each capture, and
  // the user's step for the next meeting, which starts afresh every time.
  const [consentSettings, setConsentSettings] = React.useState<ConsentSettings | null>(null);
  const [consentStep, setConsentStep] = React.useState<ConsentStep>(emptyStep());
  useEffect(() => {
    if (isTranscribing || !settings.wssEndpoint || !user?.access_token) return undefined;
    let cancelled = false;
    fetchConsentSettings(transcriberUrl(settings.wssEndpoint, user)).then((answer) => {
      if (!cancelled) setConsentSettings(answer);
    });
    setConsentStep(emptyStep());
    return () => { cancelled = true; };
  }, [isTranscribing, settings.wssEndpoint, user]);

  // The notice as shown and as START records it.
  const consentMeetingName = (continuing && meetingIdFrom(continueInput)) || topic;
  const noticeText = consentRequired(consentSettings)
    ? renderNotice(consentSettings?.noticeTemplate, {
      lmaUser: agentName,
      meeting: consentMeetingName,
      consentPhrase: consentSettings?.consentPhrase,
      objectionPhrase: consentSettings?.objectionPhrase,
    })
    : '';

  // componentDidMount:
  useEffect(() => {
    // Your code here
    fetchMetadata();
  }, []);

  useEffect(() => {
    console.log("Metadata changed");
    if (!metadata) {
      return;
    }
    const nextTopic = prefillField(topic, prefilled.current.topic, metadata.meetingTopic);
    if (nextTopic !== topic) {
      prefilled.current.topic = nextTopic;
      setTopic(nextTopic);
    }
    const nextName = prefillField(agentName, prefilled.current.agentName, metadata.userName);
    if (nextName !== agentName) {
      prefilled.current.agentName = nextName;
      setAgentName(nextName);
    }
  }, [metadata, topic, agentName, setTopic, setAgentName]);

  // The name the meeting page gives us is better, but it is often missing: a
  // changed page, a lobby we have not been admitted from, or a platform with no
  // reader. Fall back to the signed-in user so the field is not left blank.
  // Only while the field is untouched and nothing has been put in it yet, so
  // that a name the user clears is not refilled under them on the next render.
  useEffect(() => {
    if (agentName || prefilled.current.agentName) {
      return;
    }
    const loginName = nameFromIdToken(user.id_token);
    if (loginName) {
      prefilled.current.agentName = loginName;
      setAgentName(loginName);
    }
  }, [user, agentName, setAgentName]);

  const validateForm = useCallback(() => {
    let isValid = true;
    if (agentName === undefined || agentName.trim().length === 0) {
      setNameErrorText("Name required.")
      isValid = false
    } else {
      setNameErrorText("");
    }
    if (!continuing && (topic === undefined || topic.trim().length === 0)) {
      setMeetingTopicErrorText("Topic required.")
      isValid = false;
    } else {
      setMeetingTopicErrorText("");
    }
    if (continuing && !meetingIdFrom(continueInput)) {
      setContinueErrorText(INVALID_MEETING_ID);
      isValid = false;
    } else {
      setContinueErrorText("");
    }
    return isValid;
  }, [topic, agentName, continuing, continueInput, nameErrorText, setNameErrorText, meetingTopicErrorText, setMeetingTopicErrorText]);

  const startListening = useCallback(() => {
    // eslint-disable-next-line no-useless-escape
    const sanitized = topic.replace(/[\/?#%\+&]/g, '|');
    // Recorded as the prefilled value too, when that is what it came from:
    // sanitizing it would otherwise look like the user had edited the field,
    // and it would stop tracking the meeting page from here on.
    if (topic === prefilled.current.topic) {
      prefilled.current.topic = sanitized;
    }
    setTopic(sanitized);

    if (validateForm() === false) {
      return;
    }
    const blocked = startBlockedReason(consentSettings, consentStep);
    setStartError(blocked);
    if (blocked) {
      return;
    }
    setShowDisclaimer(true);
  }, [settings, validateForm, showDisclaimer, consentSettings, consentStep, setStartError]);

  const disclaimerConfirmed = useCallback(() => {
    startTranscription(user, agentName, topic, continuing ? continueInput : undefined,
      consentPayload(consentSettings, consentStep, { lmaUser: agentName, meetingName: consentMeetingName, noticeText }));
  }, [user, agentName, topic, continuing, continueInput, startTranscription, consentSettings, consentStep, consentMeetingName, noticeText])

  const stopListening = useCallback(() => {
    stopTranscription();
  }, [stopTranscription]);

  const openInLMA = useCallback(async () => {
    const url = `${settings.cloudfrontEndpoint}/#/calls/${currentCall.callId}`;
    window.open(url, '_blank', 'noreferrer');
  }, [currentCall, settings])

  const mute = useCallback(() => {
    setMuted(true);
  }, [muted, setMuted]);

  const unmute = useCallback(() => {
    setMuted(false);
  }, [muted, setMuted]);

  const [version, setVersion] = React.useState("");
  useEffect(() => {
    if (chrome && chrome.runtime) {
      const manifestData = chrome.runtime.getManifest();
      setVersion(manifestData.version_name || manifestData.version)
    } else {
      setVersion("dev/web");
    }
  }, [version, setVersion]);

  const consentPanel = consentRequired(consentSettings) && consentSettings ? (
    <ConsentPanel
      settings={consentSettings}
      noticeText={noticeText}
      step={consentStep}
      onStepChange={(next) => { setConsentStep(next); setStartError(""); }}
      onPostInChat={() => postInMeetingChat(noticeText)}
      capturing={isTranscribing}
      status={describeConsentState(consentFrame)}
      onObjection={(name) => reportObjection(name)}
    />
  ) : consentSettings?.available === false ? (
    <Alert type="warning" header="Meeting consent settings unavailable">
      {consentSettings.message} The transcriber applies the settings in force when you start.
    </Alert>
  ) : null;

  return (
    <ContentLayout
      header={
        <SpaceBetween size={'xs'}>
          <Header
            variant="h1"
            description="Powered by Amazon Transcribe and Amazon Bedrock"
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
        <Modal
          onDismiss={() => setShowDisclaimer(false)}
          visible={showDisclaimer}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={async () => {
                  setShowDisclaimer(false);
                }}>Cancel</Button>
                <Button variant="primary" onClick={async () => {
                  setShowDisclaimer(false);
                  disclaimerConfirmed();
                }}>Agree</Button>
              </SpaceBetween>
            </Box>
          }
          header="Important:"
        >
          <Icon name="status-warning"></Icon>&nbsp;
          {settings.recordingDisclaimer}
        </Modal>
        <SpaceBetween size="l">
          <ValueWithLabel label="Platform Detected:">{platform}</ValueWithLabel>
          {(isTranscribing === true ?
            <>
              <Button fullWidth={true} onClick={async () => openInLMA()}>Open in LMA</Button>
              <ValueWithLabel label="Name:">{agentName}</ValueWithLabel>
              {currentCall.sessionNumber ?
                <ValueWithLabel label="Continuing meeting:">{`${currentCall.callId} (session ${currentCall.sessionNumber})`}</ValueWithLabel>
                :
                <ValueWithLabel label="Meeting Topic:">{topic}</ValueWithLabel>
              }
              <ValueWithLabel label="Active Speaker:">{activeSpeaker}</ValueWithLabel>
              {
                paused === true ?
                  <>
                    <Button fullWidth={true} iconName="microphone-off" onClick={() => setPaused(false)}>Unmute All</Button>
                  </>
                  :
                  <>
                    <Button fullWidth={true} iconName="microphone" onClick={() => setPaused(true)}>Mute All</Button>
                  </>
              }
              <Button fullWidth={true} variant='primary' onClick={() => stopListening()}>Stop Listening</Button>
              {consentPanel}
            </>
            :
            <>
              <FormField
                stretch={true}
                constraintText=""
                errorText={nameErrorText}
                label="Your name:"
              >
                <Input value={agentName} onChange={({ detail }) => setAgentName(detail.value)} placeholder='Your name' ></Input>
              </FormField>
              {!continuing && (
                <FormField
                  stretch={true}
                  constraintText=""
                  errorText={meetingTopicErrorText}
                  label="Meeting Topic:"
                >
                  <Input value={topic} onChange={({ detail }) => setTopic(detail.value)} placeholder='Meeting room topic' inputMode='text'></Input>
                </FormField>
              )}
              <Checkbox
                checked={continuing}
                onChange={({ detail }) => { setContinuing(detail.checked); setStartError(""); }}
                description="Add to a meeting that already exists, even one shown as Done, on the same transcript. For the meeting's owner and Admins."
              >
                Continue an existing meeting…
              </Checkbox>
              {continuing && (
                <FormField
                  stretch={true}
                  errorText={continueErrorText}
                  label="Meeting ID or link:"
                  description="The meeting ID, or the link to the meeting's page in LMA."
                >
                  <Input
                    value={continueInput}
                    onChange={({ detail }) => { setContinueInput(detail.value); setStartError(""); }}
                    placeholder='Meeting ID or https://…/#/calls/…'
                    ariaLabel='Meeting ID or link'
                  ></Input>
                </FormField>
              )}
              {consentPanel}
              {startError && <Alert type="error">{startError}</Alert>}
              <Button fullWidth={true} variant='primary' onClick={() => startListening()}>Start Listening</Button>
            </>
          )}
          <Grid gridDefinition={[{ colspan: 6 }, { colspan: 6 }]}>
            {muted === true ?
              <Button iconAlign="left" iconName="microphone-off" fullWidth={true} onClick={() => unmute()}>Unmute Me</Button>
              :
              <Button iconAlign="left" iconName="microphone" fullWidth={true} onClick={() => mute()}>Mute Me</Button>
            }
            <Button fullWidth={true} onClick={() => logout()}>Log out</Button>
          </Grid>

          <Grid gridDefinition={[{ colspan: 10, offset: 1 }]}>
            <div className='version'>{version}</div>
          </Grid>
        </SpaceBetween>
      </Container>
    </ContentLayout>
  );
}

export default Capture;
