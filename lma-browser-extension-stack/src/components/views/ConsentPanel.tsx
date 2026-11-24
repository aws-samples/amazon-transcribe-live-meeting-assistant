/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { Alert, Box, Button, Checkbox, Container, CopyToClipboard, FormField, Header, Input, RadioGroup, SpaceBetween } from '@cloudscape-design/components';
import { CONSENT_METHODS, CONSENT_MODE_OPT_IN, ConsentMethod, ConsentSettings, ConsentStep } from '../../lib/consent';

type Props = {
  settings: ConsentSettings,
  noticeText: string,
  step: ConsentStep,
  onStepChange: (step: ConsentStep) => void,
  /** Posts the notice in the meeting chat of the captured tab; resolves false when it could not be sent. */
  onPostInChat: () => Promise<boolean>,
  capturing: boolean,
  status: { type: 'success' | 'info' | 'warning' | 'error', text: string } | null,
  onObjection: (participantName: string) => void,
};

/**
 * The consent step in the extension's side panel: the notice, Copy and Post in
 * meeting chat (counted as posted only once the user says it appeared), the
 * confirmation and how it was given, and, while capturing, Participant
 * objected.
 */
function ConsentPanel({ settings, noticeText, step, onStepChange, onPostInChat, capturing, status, onObjection }: Props) {
  const [objector, setObjector] = React.useState('');
  const [posting, setPosting] = React.useState(false);
  const optIn = settings.mode === CONSENT_MODE_OPT_IN;
  const update = (change: Partial<ConsentStep>) => onStepChange({ ...step, ...change });

  const post = async () => {
    setPosting(true);
    const sent = await onPostInChat();
    setPosting(false);
    update({ chatPost: sent ? 'sent' : 'failed' });
  };

  return (
    <Container
      header={
        <Header
          variant="h3"
          description={optIn
            ? 'This deployment records a meeting only after everyone in it has agreed. Share the request, then confirm that everyone agreed.'
            : 'This deployment tells everyone in a meeting that it is recorded and transcribed. Share the notice; if anybody objects, use Participant objected.'}
        >
          Meeting consent
        </Header>
      }
    >
      <SpaceBetween size="s">
        {settings.supported === false && (
          <Alert type="error" header="This meeting cannot be captured here">{settings.reason}</Alert>
        )}
        <FormField label={optIn ? 'Consent request' : 'Notice'} description="Read it out, or post it in the meeting chat." stretch>
          <SpaceBetween size="xs">
            <Box variant="p"><span data-testid="consent-notice" style={{ whiteSpace: 'pre-wrap' }}>{noticeText}</span></Box>
            <SpaceBetween direction="horizontal" size="xs">
              <CopyToClipboard
                variant="button"
                textToCopy={noticeText}
                copyButtonText="Copy"
                copySuccessText="Notice copied"
                copyErrorText="The notice could not be copied"
              />
              <Button onClick={post} loading={posting} disabled={capturing && step.chatPost === 'confirmed'}>Post in meeting chat</Button>
            </SpaceBetween>
            {step.chatPost === 'sent' && (
              <Alert
                type="info"
                header="Did the notice appear in the meeting chat?"
                action={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button onClick={() => update({ chatPost: 'confirmed', method: step.method || 'chat', ...(optIn ? {} : { confirmed: true }) })}>Yes, it appeared</Button>
                    <Button onClick={() => update({ chatPost: 'failed', method: step.method === 'chat' ? undefined : step.method })}>No</Button>
                  </SpaceBetween>
                }
              >
                It counts as posted only once you have seen it in the chat.
              </Alert>
            )}
            {step.chatPost === 'confirmed' && <Box color="text-status-success">Posted in the meeting chat.</Box>}
            {step.chatPost === 'failed' && (
              <Box color="text-status-warning">The notice was not posted. Copy it and paste it into the meeting chat, or read it out.</Box>
            )}
          </SpaceBetween>
        </FormField>
        {!capturing && (
          <>
            <Checkbox checked={step.confirmed} onChange={({ detail }) => update({ confirmed: detail.checked })}>
              {optIn ? 'Everyone in the meeting agreed to it being recorded and transcribed' : 'I have told everyone in the meeting, with the notice above'}
            </Checkbox>
            {step.confirmed && (
              <FormField label={optIn ? 'How everyone agreed' : 'How everyone was told'} stretch>
                <RadioGroup
                  value={step.method || null}
                  onChange={({ detail }) => update({ method: detail.value as ConsentMethod })}
                  items={CONSENT_METHODS.map((m) => ({ value: m.value, label: m.label, description: m.description }))}
                />
              </FormField>
            )}
            {step.confirmed && optIn && (
              <FormField label="Note (optional)" description="In your own words, how everyone agreed." stretch>
                <Input value={step.statement} onChange={({ detail }) => update({ statement: detail.value })} />
              </FormField>
            )}
          </>
        )}
        {status && <Alert type={status.type}>{status.text}</Alert>}
        {capturing && (
          <SpaceBetween size="xs">
            <FormField label="Name of the participant who objected (optional)" stretch>
              <Input value={objector} onChange={({ detail }) => setObjector(detail.value)} ariaLabel="Name of the participant who objected" />
            </FormField>
            <Button fullWidth onClick={() => onObjection(objector.trim())}>Participant objected</Button>
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Container>
  );
}

export default ConsentPanel;
