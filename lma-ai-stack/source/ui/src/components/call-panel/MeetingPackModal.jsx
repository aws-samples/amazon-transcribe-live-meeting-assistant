/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The two dialogs of the Download menu that put a meeting's PDF files
 * together (meeting-pack.js):
 *
 * - Meeting pack (`mode` 'pack'): the Summary, Slides and Whiteboards PDFs,
 *   each ticked when the meeting has it, built in the browser as soon as the
 *   dialog opens, with each one's progress and size; saved as one .zip or as
 *   separate files.
 * - Email participants (`mode` 'email'): the meeting's participants as the
 *   recipients, the same files to attach, the total size with a warning when
 *   it is more than mail services commonly take, and two ways to send: an
 *   email draft (.eml) with the files attached, which the mail app opens to
 *   edit and send, or the mail app opened with the summary and no attachments.
 *
 * A part the meeting does not have is not ticked and cannot be, and says why.
 * Nothing is sent from LMA; every file is saved to the viewer's computer.
 */
import React, { useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  FormField,
  Modal,
  RadioGroup,
  SpaceBetween,
  StatusIndicator,
  TokenGroup,
} from '@cloudscape-design/components';

import { saveBlob } from '../common/download-func';
import { emlBlob, encodedSize } from '../common/eml';
import {
  LIVE_NOTE,
  buildEmailDraft,
  defaultSelection,
  emailRecipients,
  emailTooLarge,
  emlFileName,
  formatBytes,
  isLive,
  mailtoUrl,
  packPartStates,
  savePack,
} from './meeting-pack';

/** What a part's line says under its name: why it is not there, how far it has got, or its size. */
export const partStatus = (part, state) => {
  if (!part.available) return { type: 'stopped', text: part.reason };
  if (!state || state.status === 'building') {
    const progress = state?.total ? ` (${state.done} of ${state.total})` : '';
    return { type: 'loading', text: `Preparing…${progress}` };
  }
  if (state.status === 'ready') return { type: 'success', text: formatBytes(state.size) };
  if (state.status === 'unavailable') return { type: 'stopped', text: state.message };
  return { type: 'error', text: state.message || 'This file could not be prepared.' };
};

const MeetingPackModal = ({
  mode,
  visible,
  onDismiss,
  item,
  pack,
  participants = [],
  whiteboardCount = 0,
  whiteboardsDeleted = false,
  keyframeCount = 0,
  contentKey = '',
  save = saveBlob,
  openMail = (url) => window.open(url),
}) => {
  const hasSummary = Boolean(item.callSummaryText);
  const { ensure, parts } = pack;
  const meetingStates = useMemo(
    () => packPartStates({ hasSummary, keyframeCount, whiteboardCount, whiteboardsDeleted }),
    [hasSummary, keyframeCount, whiteboardCount, whiteboardsDeleted],
  );
  // A part found to have nothing in it once built (key frames all of the
  // camera, say) is left out as one the meeting does not have, with the reason.
  const states = meetingStates.map((part) =>
    parts[part.id]?.status === 'unavailable' ? { ...part, available: false, reason: parts[part.id].message } : part,
  );
  const [selected, setSelected] = useState(() => defaultSelection(states));
  const [asZip, setAsZip] = useState('zip');
  const [recipients, setRecipients] = useState(() => emailRecipients(item.participants));
  const [outcome, setOutcome] = useState(null);
  const [working, setWorking] = useState(false);

  // Each time the dialog opens: every part the meeting has is ticked, the
  // recipients are the meeting's participants, and nothing has been saved yet.
  useEffect(() => {
    if (!visible) return;
    setSelected(defaultSelection(meetingStates));
    setRecipients(emailRecipients(item.participants));
    setOutcome(null);
  }, [visible]);

  // A ticked part that is not built, or no longer current (the summary, the
  // key frames or a whiteboard changed while the dialog is open), is built.
  useEffect(() => {
    if (!visible) return;
    meetingStates.forEach((part) => {
      if (part.available && selected[part.id]) ensure(part.id);
    });
  }, [visible, meetingStates, selected, ensure, item.callSummaryText, contentKey]);

  const chosen = states.filter((part) => part.available && selected[part.id]);
  const chosenStates = chosen.map((part) => parts[part.id]);
  const ready = chosenStates.every((state) => state?.status === 'ready');
  const failed = chosen.filter((part) => parts[part.id]?.status === 'failed');
  const files = ready ? chosenStates.map((state) => ({ filename: state.filename, blob: state.blob })) : [];
  const total = chosenStates.reduce((sum, state) => sum + (state?.status === 'ready' ? state.size : 0), 0);
  const tooLarge = mode === 'email' && emailTooLarge(total);

  // Saves the chosen files; `forMail` keeps the note about attaching them by hand.
  const downloadPack = async ({ zip = asZip === 'zip', forMail = false } = {}) => {
    setWorking(true);
    try {
      await savePack(files, { callId: item.callId, asZip: zip, save });
      if (!forMail)
        setOutcome({ type: 'success', text: zip ? 'The meeting pack was saved.' : 'The files were saved.' });
    } catch (error) {
      setOutcome({ type: 'error', text: 'The files could not be saved.' });
    } finally {
      setWorking(false);
    }
  };

  const createDraft = async () => {
    setWorking(true);
    try {
      const message = await buildEmailDraft({ item, recipients, participants, files });
      const name = emlFileName(item.callId);
      save(emlBlob(message), name);
      setOutcome({
        type: 'success',
        text: `Saved ${name}. Open it in your mail app: Outlook opens it as a draft, with the files attached, to edit and send.`,
      });
    } catch (error) {
      setOutcome({ type: 'error', text: 'The email draft could not be prepared.' });
    } finally {
      setWorking(false);
    }
  };

  const openInMailApp = () => {
    openMail(mailtoUrl(item, recipients));
    setOutcome({ type: 'mailto', text: 'Your mail app was asked to open a message with the summary.' });
  };

  const partList = (
    <FormField label={mode === 'email' ? 'Attach' : 'Files'}>
      <SpaceBetween size="xs">
        {states.map((part) => {
          const status = partStatus(part, parts[part.id]);
          const ticked = part.available && Boolean(selected[part.id]);
          return (
            <div key={part.id} data-testid={`pack-part-${part.id}`}>
              <Checkbox
                checked={ticked}
                disabled={!part.available}
                onChange={({ detail }) => setSelected((previous) => ({ ...previous, [part.id]: detail.checked }))}
                description={
                  ticked || !part.available ? (
                    <StatusIndicator type={status.type}>{status.text}</StatusIndicator>
                  ) : (
                    'Not included.'
                  )
                }
              >
                {part.label}
              </Checkbox>
              {ticked && parts[part.id]?.status === 'failed' && (
                <Button variant="inline-link" onClick={() => ensure(part.id, { force: true })}>
                  Try again
                </Button>
              )}
            </div>
          );
        })}
      </SpaceBetween>
    </FormField>
  );

  const sizeLine = chosen.length ? (
    <Box variant="small" data-testid="pack-total">
      {ready
        ? `Total: ${formatBytes(total)}${
            mode === 'email' ? ` (about ${formatBytes(encodedSize(total))} as an email)` : ''
          }`
        : `Total so far: ${formatBytes(total)}`}
    </Box>
  ) : (
    <Box variant="small">Nothing chosen.</Box>
  );

  const waiting = chosen.length > 0 && !ready && !failed.length;
  const blocked = failed.length > 0;
  const blockedText = blocked ? `Untick ${failed.map((part) => part.label).join(' and ')}, or try again.` : '';

  const footer =
    mode === 'email' ? (
      <Box float="right">
        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={onDismiss}>Close</Button>
          <Button onClick={openInMailApp} disabled={!hasSummary}>
            Open in mail app (no attachments)
          </Button>
          <Button variant="primary" onClick={createDraft} loading={working || waiting} disabled={blocked}>
            Create email draft (.eml)
          </Button>
        </SpaceBetween>
      </Box>
    ) : (
      <Box float="right">
        <SpaceBetween direction="horizontal" size="xs">
          <Button onClick={onDismiss}>Close</Button>
          <Button
            variant="primary"
            onClick={() => downloadPack()}
            loading={working || waiting}
            disabled={!chosen.length || blocked}
          >
            Download
          </Button>
        </SpaceBetween>
      </Box>
    );

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={mode === 'email' ? 'Email participants' : 'Meeting pack'}
      footer={footer}
      size="medium"
    >
      <SpaceBetween size="m">
        {mode === 'email' ? (
          <FormField
            label="To"
            description="The meeting's participants with an email address. Change them here or in your mail app."
          >
            {recipients.length ? (
              <TokenGroup
                items={recipients.map(({ name, email }) => ({
                  label: name || email,
                  description: name ? email : undefined,
                  dismissLabel: `Remove ${email}`,
                }))}
                onDismiss={({ detail }) =>
                  setRecipients((previous) => previous.filter((_, index) => index !== detail.itemIndex))
                }
                limit={8}
                i18nStrings={{ limitShowMore: 'Show all', limitShowFewer: 'Show fewer' }}
              />
            ) : (
              <Box color="text-status-inactive">
                No participant of this meeting has an email address, so the draft has no recipients. Add them in your
                mail app.
              </Box>
            )}
          </FormField>
        ) : (
          <Box variant="p">The meeting&apos;s PDF files, built in your browser. Choose which to include.</Box>
        )}
        {isLive(item) && <Alert type="info">{LIVE_NOTE}</Alert>}
        {partList}
        {sizeLine}
        {blocked && <Box color="text-status-error">{blockedText}</Box>}
        {tooLarge && (
          <Alert type="warning" header="The email may be too large to send">
            Many mail services refuse a message above 20 to 25 MB, and attaching files adds about a third to their size.
            Untick Slides (PDF) to make it smaller, and share the slides another way.
          </Alert>
        )}
        {mode === 'pack' && (
          <RadioGroup
            value={asZip}
            onChange={({ detail }) => setAsZip(detail.value)}
            items={[
              { value: 'zip', label: 'One .zip file' },
              {
                value: 'separate',
                label: 'Separate files',
                description: 'Your browser may ask to allow several downloads.',
              },
            ]}
            ariaLabel="Save as"
          />
        )}
        {mode === 'email' && (
          <Box variant="small" color="text-status-inactive">
            The draft is a .eml file saved to your computer. Nothing is sent from LMA.
          </Box>
        )}
        {outcome && outcome.type !== 'mailto' && (
          <Alert type={outcome.type} statusIconAriaLabel={outcome.type}>
            {outcome.text}
          </Alert>
        )}
        {outcome?.type === 'mailto' && (
          <Alert
            type="info"
            action={
              <Button onClick={() => downloadPack({ zip: false, forMail: true })} disabled={!ready || !files.length}>
                Download the files
              </Button>
            }
          >
            {outcome.text} A message opened this way cannot carry attachments: download the files and attach them by
            hand.
          </Alert>
        )}
      </SpaceBetween>
    </Modal>
  );
};

MeetingPackModal.propTypes = {
  mode: PropTypes.oneOf(['pack', 'email']).isRequired,
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  // eslint-disable-next-line react/forbid-prop-types
  item: PropTypes.object.isRequired,
  pack: PropTypes.shape({ parts: PropTypes.objectOf(PropTypes.shape({})), ensure: PropTypes.func }).isRequired,
  participants: PropTypes.arrayOf(PropTypes.string),
  whiteboardCount: PropTypes.number,
  whiteboardsDeleted: PropTypes.bool,
  keyframeCount: PropTypes.number,
  /** Changes when a whiteboard changes, so an open dialog builds the Whiteboards PDF again. */
  contentKey: PropTypes.string,
  save: PropTypes.func,
  openMail: PropTypes.func,
};

export default MeetingPackModal;
