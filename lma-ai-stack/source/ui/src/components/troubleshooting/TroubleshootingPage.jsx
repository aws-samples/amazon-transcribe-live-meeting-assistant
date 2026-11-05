/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Troubleshooting page: questions about the whole deployment, answered by
 * the troubleshooting agent from this stack's own logs, Virtual Participant
 * records and stack parameters. See docs/troubleshooting-agent.md.
 *
 * For the Admin group. The startTroubleshooting field and its resolver accept
 * a diagnosis from the Admin group only; the group check here makes that
 * legible rather than arriving as a refused request.
 *
 * Each question is one diagnosis, and one runs at a time: a diagnosis takes
 * minutes and every run is paid for, so the next question waits for the answer
 * to the last. A meeting ID or Virtual Participant ID, under Scope, points the
 * agent at one resource.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Button,
  Container,
  ExpandableSection,
  FormField,
  Grid,
  Header,
  Input,
  SpaceBetween,
  Spinner,
  Textarea,
} from '@cloudscape-design/components';

import useTroubleshootingJob from '../../hooks/use-troubleshooting-job';
import useUserGroups from '../../hooks/use-user-groups';
import TroubleshootingAnswer, { JOB_SHAPE } from './TroubleshootingAnswer';

export const STARTER_QUESTIONS = [
  "Why didn't my last Virtual Participant join its meeting?",
  'Are there errors in the transcriber in the last hour?',
  'Did any meeting in the last 24 hours stop receiving transcript segments?',
  'Is the meeting assistant failing to answer questions?',
];

const PAGE_DESCRIPTION =
  "Ask why something failed. The troubleshooting agent reads this deployment's logs, Virtual Participant " +
  'records and stack parameters, and answers with a root cause and the evidence it rests on. It reads only; ' +
  'it changes nothing.';

const Turn = ({ turn, job = null, error = null, starting = false }) => (
  <Container
    header={
      <Header
        variant="h3"
        description={
          [turn.callId && `Meeting ${turn.callId}`, turn.vpId && `Virtual Participant ${turn.vpId}`]
            .filter(Boolean)
            .join(' · ') || undefined
        }
      >
        <span className="troubleshooting-question">{turn.question}</span>
      </Header>
    }
  >
    <TroubleshootingAnswer job={job} error={error} starting={starting} />
  </Container>
);

const TURN_SHAPE = PropTypes.shape({
  key: PropTypes.number.isRequired,
  question: PropTypes.string.isRequired,
  callId: PropTypes.string,
  vpId: PropTypes.string,
});

Turn.propTypes = {
  turn: TURN_SHAPE.isRequired,
  job: JOB_SHAPE,
  error: PropTypes.string,
  starting: PropTypes.bool,
};

export const TroubleshootingContent = ({ pollIntervalMs = undefined }) => {
  const { job, error, starting, running, start } = useTroubleshootingJob(
    pollIntervalMs ? { pollIntervalMs } : undefined,
  );
  const [history, setHistory] = useState([]);
  const [current, setCurrent] = useState(null);
  const [question, setQuestion] = useState('');
  const [callId, setCallId] = useState('');
  const [vpId, setVpId] = useState('');

  const ask = (text) => {
    const asked = (text || '').trim();
    if (!asked || running) return;
    if (current) setHistory((previous) => [...previous, { turn: current, job, error }]);
    const turn = { key: Date.now(), question: asked, callId: callId.trim(), vpId: vpId.trim() };
    setCurrent(turn);
    setQuestion('');
    start({ question: asked, callId: turn.callId, vpId: turn.vpId });
  };

  const onSubmit = (event) => {
    event.preventDefault();
    ask(question);
  };

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header variant="h1" description={PAGE_DESCRIPTION}>
            Troubleshooting
          </Header>
        }
      >
        <form onSubmit={onSubmit}>
          <SpaceBetween size="m">
            <FormField label="Question" stretch>
              <Textarea
                value={question}
                onChange={({ detail }) => setQuestion(detail.value)}
                placeholder="For example: why did the Virtual Participant for my 10:00 meeting leave early?"
                rows={3}
                ariaLabel="Question"
              />
            </FormField>
            <ExpandableSection headerText="Scope (optional)">
              <Grid gridDefinition={[{ colspan: { default: 12, s: 6 } }, { colspan: { default: 12, s: 6 } }]}>
                <FormField label="Meeting ID">
                  <Input value={callId} onChange={({ detail }) => setCallId(detail.value)} ariaLabel="Meeting ID" />
                </FormField>
                <FormField label="Virtual Participant ID">
                  <Input
                    value={vpId}
                    onChange={({ detail }) => setVpId(detail.value)}
                    ariaLabel="Virtual Participant ID"
                  />
                </FormField>
              </Grid>
            </ExpandableSection>
            <Box float="right">
              <Button variant="primary" formAction="submit" loading={running} disabled={!question.trim()}>
                Ask
              </Button>
            </Box>
          </SpaceBetween>
        </form>
      </Container>

      {!current && (
        <Container header={<Header variant="h3">Suggested questions</Header>}>
          <SpaceBetween size="xs" direction="horizontal">
            {STARTER_QUESTIONS.map((starter) => (
              <Button key={starter} onClick={() => ask(starter)}>
                {starter}
              </Button>
            ))}
          </SpaceBetween>
        </Container>
      )}

      {current && <Turn turn={current} job={job} error={error} starting={starting} />}
      {[...history].reverse().map((entry) => (
        <Turn key={entry.turn.key} turn={entry.turn} job={entry.job} error={entry.error} />
      ))}
    </SpaceBetween>
  );
};

TroubleshootingContent.propTypes = {
  pollIntervalMs: PropTypes.number,
};

/**
 * What a user outside the Admin group sees. AdminOnly's own message is about
 * changing a setting, which is not what this page does.
 */
export const TroubleshootingAccessDenied = () => (
  <Box padding="xl">
    <Alert type="error" header="Access denied">
      Only <strong>Admin</strong> users can use troubleshooting. Ask an administrator if something in this deployment
      needs diagnosing.
    </Alert>
  </Box>
);

const TroubleshootingPage = () => {
  const { isAdmin, isLoading } = useUserGroups();
  if (isLoading) {
    return (
      <Box padding="xl" textAlign="center">
        <Spinner size="large" /> Loading...
      </Box>
    );
  }
  return isAdmin ? <TroubleshootingContent /> : <TroubleshootingAccessDenied />;
};

export default TroubleshootingPage;
