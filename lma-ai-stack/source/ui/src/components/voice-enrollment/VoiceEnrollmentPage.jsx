/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The voice-enrollment portal.
 *
 * The person using this page is often not an LMA user -- somebody from another
 * company, a contractor in a meeting room -- and the whole reason the page
 * exists is that a consent the meeting host asserts on their behalf would be
 * worthless. So the flow is deliberately theirs, in this order:
 *
 *   1. read the terms, in full, in the text whose hash will be stored;
 *   2. tick a box and press a button -- two separate affirmative acts, no
 *      pre-ticked anything and no consent implied by continuing;
 *   3. only then is any audio recorded.
 *
 * Consent is recorded BEFORE the recording step, so a record exists even if
 * somebody walks away at step 3. Declining is a normal outcome of this page and
 * leaves nothing stored.
 */
import { ConsoleLogger } from 'aws-amplify/utils';
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  Container,
  ContentLayout,
  FormField,
  Header,
  Input,
  SpaceBetween,
  StatusIndicator,
  TextContent,
} from '@cloudscape-design/components';

import {
  enrolMyVoice,
  grantVoiceConsent,
  revokeVoiceConsent,
  voiceEnrollmentTerms,
} from '../../graphql/queries/voiceEnrollment';
import { clipToPcm } from './pcm';

/* eslint-disable react/prop-types */

const client = generateClient();
const logger = new ConsoleLogger('VoiceEnrollmentPage');

/**
 * Enough speech for a stable voiceprint, and the same numbers the backend
 * enforces -- shown here so somebody knows when they have done enough rather
 * than being told after the fact.
 */
const MIN_SECONDS = 20;
const MIN_CLIPS = 3;

/**
 * What to read aloud.
 *
 * Phonetically varied on purpose: a voiceprint from one short phrase recognises
 * that phrase more than it recognises the person. Mundane on purpose too --
 * nobody should have to say anything about themselves to enrol.
 */
const SCRIPT = [
  'The quick brown fox jumps over the lazy dog, and the early bird catches the worm.',
  'I would like a large coffee, two slices of toast, and the morning paper, please.',
  'Thursday afternoon was unusually warm, so we walked along the river to the bridge.',
  'Seventeen, forty-two, ninety-eight, three hundred and six, one thousand and five.',
];

const VoiceEnrollmentPage = () => {
  const [state, setState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [agreed, setAgreed] = useState(false);
  const [displayName, setDisplayName] = useState('');
  const [clips, setClips] = useState([]);
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const recorder = useRef(null);
  const chunks = useRef([]);
  const stream = useRef(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await client.graphql({ query: voiceEnrollmentTerms });
      const result = response?.data?.voiceEnrollmentTerms;
      setState(result);
      setDisplayName(result?.DisplayName || '');
    } catch (problem) {
      logger.error('could not load the enrollment page', problem);
      setError('Could not load this page. Please try again.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Release the microphone on unmount: a page that quietly holds it open after
  // somebody navigates away is not acceptable on a page about consent.
  useEffect(
    () => () => {
      if (stream.current) {
        stream.current.getTracks().forEach((track) => track.stop());
      }
    },
    [],
  );

  const totalSeconds = clips.reduce((sum, clip) => sum + clip.seconds, 0);
  const enough = totalSeconds >= MIN_SECONDS && clips.length >= MIN_CLIPS;
  const status = state?.Status || 'none';
  // Whether a consent currently STANDS, which is not the same as whether one
  // was ever given. A withdrawal and an expiry both leave the record in place
  // with status 'revoked' -- that is what preserves the fact of the consent
  // after the voiceprint is destroyed -- so testing only that a status exists
  // treated a withdrawn person as still consented, and the page then offered
  // them neither the terms nor the recorder. The way back has to be open: the
  // terms say withdrawing is reversible, and the retention pass puts everybody
  // in this state eventually.
  const consented = ['pending', 'active'].includes(status) && !state?.NeedsReconsent;

  const giveConsent = async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await client.graphql({ query: grantVoiceConsent });
      const result = response?.data?.grantVoiceConsent;
      if (!result?.Success) {
        setError(result?.Message || 'Could not record your consent.');
        return;
      }
      setNotice('Your consent has been recorded. You can record your voice now.');
      await load();
    } catch (problem) {
      logger.error('could not record consent', problem);
      setError('Could not record your consent. Please try again.');
    } finally {
      setBusy(false);
    }
  };

  const startRecording = async () => {
    setError(null);
    try {
      stream.current = await window.navigator.mediaDevices.getUserMedia({ audio: true });
      chunks.current = [];
      recorder.current = new window.MediaRecorder(stream.current);
      recorder.current.ondataavailable = (event) => chunks.current.push(event.data);
      recorder.current.onstop = async () => {
        const blob = new Blob(chunks.current, { type: recorder.current.mimeType });
        const context = new window.AudioContext();
        try {
          const clip = await clipToPcm(blob, (buffer) => context.decodeAudioData(buffer));
          setClips((current) => [...current, clip]);
        } catch (problem) {
          logger.error('could not read that recording', problem);
          setError('Could not read that recording. Please try again.');
        } finally {
          context.close();
        }
      };
      recorder.current.start();
      setRecording(true);
    } catch (problem) {
      logger.error('could not reach the microphone', problem);
      setError('Could not reach your microphone. Please allow access and try again.');
    }
  };

  const stopRecording = () => {
    if (recorder.current && recorder.current.state !== 'inactive') {
      recorder.current.stop();
    }
    if (stream.current) {
      stream.current.getTracks().forEach((track) => track.stop());
      stream.current = null;
    }
    setRecording(false);
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await client.graphql({
        query: enrolMyVoice,
        variables: {
          input: {
            DisplayName: displayName.trim(),
            Samples: clips.map((clip) => clip.pcm),
            TotalSeconds: totalSeconds,
          },
        },
      });
      const result = response?.data?.enrolMyVoice;
      if (!result?.Success) {
        setError(result?.Message || 'Could not store your voiceprint.');
        return;
      }
      // Dropped as soon as they are sent: the recordings exist only to compute
      // the voiceprint, and the terms say they are not kept.
      setClips([]);
      setNotice('Your voice is enrolled. Meetings will label you by name from now on.');
      await load();
    } catch (problem) {
      logger.error('could not enrol', problem);
      setError('Could not store your voiceprint. Please try again.');
    } finally {
      setBusy(false);
    }
  };

  const withdraw = async () => {
    setBusy(true);
    setError(null);
    try {
      const response = await client.graphql({ query: revokeVoiceConsent, variables: { input: {} } });
      const result = response?.data?.revokeVoiceConsent;
      if (!result?.Success) {
        setError(result?.Message || 'Could not withdraw.');
        return;
      }
      setAgreed(false);
      setClips([]);
      setNotice('Your voiceprint has been deleted. No future meeting can recognise your voice.');
      await load();
    } catch (problem) {
      logger.error('could not withdraw', problem);
      setError('Could not withdraw. Please try again.');
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <Box padding="l">
        <StatusIndicator type="loading">Loading</StatusIndicator>
      </Box>
    );
  }

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Let meetings label what you say with your name, instead of “Other Participant”."
        >
          Your voice
        </Header>
      }
    >
      <SpaceBetween direction="vertical" size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}
        {notice && (
          <Alert type="success" dismissible onDismiss={() => setNotice(null)}>
            {notice}
          </Alert>
        )}

        {status === 'active' && !state?.NeedsReconsent && (
          <Container header={<Header variant="h2">You are enrolled</Header>}>
            <SpaceBetween direction="vertical" size="m">
              <TextContent>
                <p>
                  Meetings recorded by this deployment will label you as <strong>{state.DisplayName}</strong>.
                  {state.ConsentExpiresAt &&
                    ` Your consent lasts until ${state.ConsentExpiresAt.slice(0, 10)}, after
                  which you will be asked again.`}
                </p>
              </TextContent>
              {/* Withdrawal is as prominent as enrolling was, and needs nobody's
                  permission. */}
              <Button onClick={withdraw} loading={busy}>
                Delete my voiceprint
              </Button>
            </SpaceBetween>
          </Container>
        )}

        {state?.NeedsReconsent && (
          <Alert type="info" header="The terms have changed">
            What you agreed to before is not what this deployment does now, so your voiceprint is not being used. Read
            the terms below and decide again.
          </Alert>
        )}

        {!consented && (
          <Container header={<Header variant="h2">What you are being asked</Header>}>
            <SpaceBetween direction="vertical" size="m">
              {/* The text whose hash is stored with the consent, rendered as
                  given rather than summarised -- a summary would not be what
                  the stored record attests to. */}
              <TextContent>
                {(state?.Terms || '').split('\n\n').map((paragraph) => (
                  <p key={paragraph.slice(0, 40)}>{paragraph}</p>
                ))}
              </TextContent>
              <Checkbox checked={agreed} onChange={({ detail }) => setAgreed(detail.checked)}>
                I have read the above and I agree to my voiceprint being stored and used as described.
              </Checkbox>
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="primary" disabled={!agreed} loading={busy} onClick={giveConsent}>
                  I agree
                </Button>
                {/* Declining is an ordinary outcome, not an error, and says so. */}
                <Button href="#/" disabled={busy}>
                  No thanks
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        )}

        {consented && status === 'pending' && (
          <Container
            header={
              <Header
                variant="h2"
                description={`Read each line aloud, in your normal voice. ${MIN_CLIPS} recordings and about
                  ${MIN_SECONDS} seconds in total is enough.`}
              >
                Record your voice
              </Header>
            }
          >
            <SpaceBetween direction="vertical" size="m">
              <FormField label="The name to label you with" description="This is what appears in meeting transcripts.">
                <Input
                  value={displayName}
                  onChange={({ detail }) => setDisplayName(detail.value)}
                  placeholder="e.g. Sarah Lee"
                  disabled={busy}
                />
              </FormField>

              <TextContent>
                <ol>
                  {SCRIPT.map((line, index) => (
                    <li key={line}>
                      {line}
                      {clips[index] ? ' ✓' : ''}
                    </li>
                  ))}
                </ol>
              </TextContent>

              <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                {recording ? (
                  <Button variant="primary" iconName="microphone" onClick={stopRecording}>
                    Stop
                  </Button>
                ) : (
                  <Button iconName="microphone" onClick={startRecording} disabled={busy}>
                    Record a line
                  </Button>
                )}
                <Box variant="small" color="text-body-secondary">
                  {`${clips.length} ${clips.length === 1 ? 'recording' : 'recordings'}, `}
                  {`${totalSeconds.toFixed(0)}s of ${MIN_SECONDS}s`}
                </Box>
                {clips.length > 0 && (
                  <Button variant="inline-link" onClick={() => setClips([])} disabled={busy || recording}>
                    Start again
                  </Button>
                )}
              </SpaceBetween>

              <Button
                variant="primary"
                disabled={!enough || !displayName.trim() || busy || recording}
                loading={busy}
                onClick={submit}
              >
                Enrol my voice
              </Button>

              <Box variant="small" color="text-body-secondary">
                Your recordings are used to compute the voiceprint and are then discarded. They are never saved.
              </Box>
            </SpaceBetween>
          </Container>
        )}

        {status === 'revoked' && (
          <Alert type="info">
            Your voiceprint was deleted. You can enrol again at any time by agreeing to the terms above.
          </Alert>
        )}
      </SpaceBetween>
    </ContentLayout>
  );
};

export default VoiceEnrollmentPage;
