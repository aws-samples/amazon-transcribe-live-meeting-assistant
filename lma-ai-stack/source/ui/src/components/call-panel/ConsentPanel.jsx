/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * The meeting's consent record: its mode and status, whether it may be recorded
 * and transcribed now, who replied what, the owner's attestation, the evidence
 * images with the vision model's check of each, and a timeline of all of it.
 *
 * Shown only for a meeting whose consent was started. Everything shown is what
 * the backend stored and decided; the panel's two actions -- record an
 * objection, attest -- and the evidence check are requests the backend applies
 * its own access rule to. See docs/meeting-consent.md.
 */
import React, { useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  Container,
  ExpandableSection,
  FormField,
  Header,
  Input,
  Link,
  Modal,
  Select,
  SpaceBetween,
  StatusIndicator,
  Textarea,
} from '@cloudscape-design/components';

import {
  ATTESTATION_METHOD_LABELS,
  MODE_LABELS,
  RULE_LABELS,
  STATUS_LABELS,
  VERDICT_LABELS,
  buildTimeline,
  participantRows,
  statusIndicatorType,
  unidentifiedLabel,
  unidentifiedPresentCount,
  verdictColor,
} from '../common/meeting-consent';

/* eslint-disable react/prop-types */

const DOCS_URL =
  'https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/main/docs/meeting-consent.md';

const DESCRIPTION =
  'Supporting evidence of what was posted and replied, by display name. Not a legal determination of consent.';

const formatTime = (iso) => {
  if (!iso) return '';
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString();
};

const decisionBadge = (decision) => {
  if (decision === 'consent') return <Badge color="green">Consented</Badge>;
  if (decision === 'objection') return <Badge color="red">Objected</Badge>;
  return <Badge color="grey">No reply</Badge>;
};

const METHOD_OPTIONS = Object.entries(ATTESTATION_METHOD_LABELS).map(([value, label]) => ({ value, label }));

const EvidenceItem = ({ item, onVerify, canVerify }) => {
  const [checking, setChecking] = useState(false);
  const [failure, setFailure] = useState(null);
  // An image can expire before the record that lists it (the evidence
  // folder's lifecycle rule follows the transcript's retention).
  const [unavailable, setUnavailable] = useState(false);
  // A fresh link (each read signs the image again) deserves another try.
  useEffect(() => setUnavailable(false), [item.SignedUrl]);
  const check = async () => {
    setChecking(true);
    setFailure(null);
    try {
      setFailure(await onVerify(item.EvidenceId));
    } finally {
      setChecking(false);
    }
  };
  const verification = item.Verification;
  return (
    <Box padding="xs">
      <SpaceBetween size="xs">
        {item.SignedUrl && !unavailable ? (
          <a href={item.SignedUrl} target="_blank" rel="noopener noreferrer">
            <img
              src={item.SignedUrl}
              alt={`Consent evidence captured ${formatTime(item.CapturedAt)}`}
              onError={() => setUnavailable(true)}
              style={{ maxWidth: '100%', maxHeight: 180, border: '1px solid #d5dbdb', borderRadius: 4 }}
            />
          </a>
        ) : (
          <Box color="text-status-inactive">
            {(item.ImageDeletedAt && 'Image deleted after its check') ||
              (unavailable && 'Image no longer available') ||
              'No image'}
          </Box>
        )}
        <Box fontSize="body-s" color="text-body-secondary">
          {`${item.Kind === 'invitation' ? 'Invitation' : 'Screenshot'} captured ${formatTime(item.CapturedAt)}`}
          {item.Note ? ` — ${item.Note}` : ''}
        </Box>
        {verification ? (
          <SpaceBetween size="xxs">
            <SpaceBetween size="xxs" direction="horizontal" alignItems="center">
              <Box fontSize="body-s">Image check:</Box>
              <Badge color={verdictColor(verification.Verdict)}>
                {VERDICT_LABELS[verification.Verdict] || verification.Verdict}
              </Badge>
              <Box fontSize="body-s" color="text-body-secondary">
                {`confidence ${verification.Confidence ?? '?'}, ${verification.ModelId || 'model'}`}
              </Box>
            </SpaceBetween>
            {verification.Summary && <Box fontSize="body-s">{verification.Summary}</Box>}
            {(verification.MissingParticipants || []).length > 0 && (
              <Box fontSize="body-s">{`No visible consent from: ${verification.MissingParticipants.join(', ')}`}</Box>
            )}
          </SpaceBetween>
        ) : (
          <Box fontSize="body-s" color="text-status-inactive">
            Image check: not run
          </Box>
        )}
        {canVerify && !item.ImageDeletedAt && (
          <Button onClick={check} loading={checking} iconName="search">
            {verification ? 'Check again' : 'Check with model'}
          </Button>
        )}
        {/* A check that raced the automatic one, which then deleted the image,
            fails; the entry now shows the image as deleted after its check. */}
        {failure && !item.ImageDeletedAt && <StatusIndicator type="error">{failure}</StatusIndicator>}
      </SpaceBetween>
    </Box>
  );
};

const ObjectionModal = ({ onDismiss, onSubmit }) => {
  const [name, setName] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const submit = async () => {
    setBusy(true);
    setFailure(null);
    const problem = await onSubmit(name.trim(), text.trim());
    setBusy(false);
    if (problem) setFailure(problem);
    else onDismiss();
  };
  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header="Record an objection"
      footer={
        <Box float="right">
          <SpaceBetween size="xs" direction="horizontal">
            <Button onClick={onDismiss} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={busy} disabled={!name.trim()}>
              Record objection
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>
          An objection stops recording and transcription in a notify or opt-in meeting until that participant consents.
          It is recorded under your username.
        </Box>
        <FormField label="Participant" description="Their display name in the meeting">
          <Input value={name} onChange={({ detail }) => setName(detail.value)} ariaLabel="Participant name" />
        </FormField>
        <FormField label="What they said (optional)">
          <Textarea value={text} onChange={({ detail }) => setText(detail.value)} rows={2} ariaLabel="Objection" />
        </FormField>
        {failure && <Alert type="error">{failure}</Alert>}
      </SpaceBetween>
    </Modal>
  );
};

const AttestModal = ({ onDismiss, onSubmit }) => {
  const [method, setMethod] = useState(METHOD_OPTIONS[0]);
  const [statement, setStatement] = useState('');
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const submit = async () => {
    setBusy(true);
    setFailure(null);
    const problem = await onSubmit(method.value, statement.trim());
    setBusy(false);
    if (problem) setFailure(problem);
    else onDismiss();
  };
  return (
    <Modal
      visible
      onDismiss={onDismiss}
      header="Attest that everyone agreed"
      footer={
        <Box float="right">
          <SpaceBetween size="xs" direction="horizontal">
            <Button onClick={onDismiss} disabled={busy}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={busy}>
              Attest
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>
          You confirm, as the meeting&apos;s owner, that every participant agreed to the meeting being recorded and
          transcribed. An objection from any participant still stops it.
        </Box>
        <FormField label="How they agreed">
          <Select
            selectedOption={method}
            onChange={({ detail }) => setMethod(detail.selectedOption)}
            options={METHOD_OPTIONS}
            ariaLabel="How they agreed"
          />
        </FormField>
        <FormField label="Statement (optional)" description="For example, when and how they agreed">
          <Textarea
            value={statement}
            onChange={({ detail }) => setStatement(detail.value)}
            rows={2}
            ariaLabel="Statement"
          />
        </FormField>
        {failure && <Alert type="error">{failure}</Alert>}
      </SpaceBetween>
    </Modal>
  );
};

/**
 * @param {object} props
 * @param {object} props.state what useMeetingConsent returns
 * @param {boolean} props.canEdit the viewer may change the meeting (owner, shared-with or Admin)
 * @param {boolean} props.isOwner the viewer owns the meeting
 * @param {boolean} props.isAdmin the viewer is in the Admin group
 */
const ConsentPanel = ({ state, canEdit = false, isOwner = false, isAdmin = false }) => {
  const [dialog, setDialog] = useState(null);
  const consent = state?.consent;
  if (!consent) {
    // A failed read is said, not hidden: the record is evidence, and the
    // summary download has no record to include until one is read.
    if (!state?.error) return null;
    return (
      <Container header={<Header variant="h4">Consent</Header>}>
        <Alert type="error">{`The consent record could not be read: ${state.error}`}</Alert>
      </Container>
    );
  }

  const rows = participantRows(consent);
  const unidentified = unidentifiedPresentCount(consent);
  const timeline = buildTimeline(consent);
  const evidence = consent.Evidence || [];
  const attestation = consent.Attestation;
  // Only where an attestation is what grants the meeting; elsewhere it would
  // add an entry that changes nothing.
  const offerAttestation = isOwner && consent.Mode === 'opt_in' && consent.Rule === 'owner_attests';

  return (
    <Container
      header={
        <Header
          variant="h4"
          info={
            <Link variant="info" href={DOCS_URL} target="_blank">
              Info
            </Link>
          }
          description={DESCRIPTION}
          actions={
            <SpaceBetween size="xxs" direction="horizontal" alignItems="center">
              {canEdit && consent.Mode !== 'off' && (
                <Button onClick={() => setDialog('objection')}>Record an objection</Button>
              )}
              {offerAttestation && (
                <Button onClick={() => setDialog('attest')} disabled={!!attestation}>
                  {attestation ? 'Attested' : 'Attest that everyone agreed'}
                </Button>
              )}
            </SpaceBetween>
          }
        >
          Consent
        </Header>
      }
    >
      <SpaceBetween size="l">
        {state.error && <Alert type="error">{`The consent record could not be read: ${state.error}`}</Alert>}
        <ColumnLayout columns={3} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Mode</Box>
            <div>{MODE_LABELS[consent.Mode] || consent.Mode}</div>
            {consent.Rule && <Box fontSize="body-s">{RULE_LABELS[consent.Rule] || consent.Rule}</Box>}
          </div>
          <div>
            <Box variant="awsui-key-label">Status</Box>
            <StatusIndicator type={statusIndicatorType(consent.Status)}>
              {STATUS_LABELS[consent.Status] || consent.Status}
            </StatusIndicator>
          </div>
          <div>
            <Box variant="awsui-key-label">Recording and transcription</Box>
            <StatusIndicator type={consent.CaptureAllowed ? 'success' : 'stopped'}>
              {consent.CaptureAllowed ? 'Allowed' : 'Withheld'}
            </StatusIndicator>
          </div>
        </ColumnLayout>

        {consent.NoticeText && (
          <div>
            <Box variant="awsui-key-label">Notice</Box>
            <Box>{consent.NoticeText}</Box>
            <Box fontSize="body-s" color="text-body-secondary">
              {consent.NoticePostedAt ? `First posted ${formatTime(consent.NoticePostedAt)}` : 'Not posted yet'}
            </Box>
          </div>
        )}

        {(rows.length > 0 || unidentified > 0) && (
          <div>
            <Box variant="awsui-key-label">Participants</Box>
            <SpaceBetween size="xxs">
              {rows.map((row) => (
                <SpaceBetween key={row.name} size="xs" direction="horizontal" alignItems="center">
                  <span>{row.name}</span>
                  {decisionBadge(row.decision)}
                  {!row.present && (
                    <Box fontSize="body-s" color="text-status-inactive">
                      not present now
                    </Box>
                  )}
                </SpaceBetween>
              ))}
              {/* Seats counted on a platform that gives only a count, not people. */}
              {unidentified > 0 && <Box color="text-body-secondary">{unidentifiedLabel(unidentified)}</Box>}
            </SpaceBetween>
          </div>
        )}

        {attestation && (
          <div>
            <Box variant="awsui-key-label">Attestation</Box>
            <Box>
              {`${attestation.AttestedBy}: ${
                ATTESTATION_METHOD_LABELS[attestation.Method] || attestation.Method
              }, ${formatTime(attestation.AttestedAt)}`}
            </Box>
            {attestation.Statement && <Box fontSize="body-s">{`"${attestation.Statement}"`}</Box>}
          </div>
        )}

        {evidence.length > 0 && (
          <div>
            <Box variant="awsui-key-label">Evidence</Box>
            <ColumnLayout columns={3}>
              {evidence.map((item) => (
                <EvidenceItem
                  key={item.EvidenceId}
                  item={item}
                  onVerify={state.verifyEvidence}
                  canVerify={isOwner || isAdmin}
                />
              ))}
            </ColumnLayout>
            <Box fontSize="body-s" color="text-body-secondary">
              An image check reports what a model reads in that image alone, so it can differ from the status above. It
              is supporting evidence only and never changes the status.
            </Box>
          </div>
        )}

        {timeline.length > 0 && (
          <ExpandableSection headerText={`Timeline (${timeline.length})`} variant="footer">
            <SpaceBetween size="xxs">
              {timeline.map((entry) => (
                <Box key={`${entry.kind}-${entry.index}`} fontSize="body-s">
                  <Box variant="span" color="text-body-secondary">
                    {`${formatTime(entry.at) || '—'}  `}
                  </Box>
                  {entry.text}
                </Box>
              ))}
            </SpaceBetween>
          </ExpandableSection>
        )}
      </SpaceBetween>
      {dialog === 'objection' && (
        <ObjectionModal
          onDismiss={() => setDialog(null)}
          onSubmit={(name, text) => state.recordObjection(name, text)}
        />
      )}
      {dialog === 'attest' && (
        <AttestModal
          onDismiss={() => setDialog(null)}
          onSubmit={(method, statement) => state.attest(method, statement)}
        />
      )}
    </Container>
  );
};

export default ConsentPanel;
