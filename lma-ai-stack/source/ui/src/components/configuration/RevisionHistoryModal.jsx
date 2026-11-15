/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Badge,
  Box,
  Button,
  FormField,
  Header,
  Input,
  Modal,
  SpaceBetween,
  Table,
  Textarea,
} from '@cloudscape-design/components';

import { isConflict } from '../common/config-conflict';
import {
  MAX_LABEL_LENGTH,
  comparisonPair,
  diffRevisionsQuery,
  getRevisionQuery,
  labelRevisionMutation,
  listRevisionsQuery,
  parseChanges,
  pathText,
  restoreRevisionMutation,
  sectionsText,
  sourceText,
  valueText,
} from '../configuration-history/history';
import { errorText } from './configuration-api';

const client = generateClient();

const CONFLICT_MESSAGE =
  'Someone saved the configuration after this history was loaded. Refresh the history to see their change, ' +
  'then restore again if you still want to.';

const whenText = (iso) => {
  const parsed = iso ? new Date(iso) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString() : iso || '—';
};

const COLUMNS = [
  {
    id: 'revision',
    header: 'Revision',
    cell: (entry) => (
      <SpaceBetween direction="horizontal" size="xxs">
        <span>{entry.Revision}</span>
        {entry.Published && <Badge color="green">Current</Badge>}
      </SpaceBetween>
    ),
    width: 130,
  },
  { id: 'when', header: 'Saved', cell: (entry) => whenText(entry.CreatedAt) },
  { id: 'who', header: 'By', cell: (entry) => entry.CreatedBy || '—' },
  { id: 'sections', header: 'Changed', cell: (entry) => sectionsText(entry.Sections) },
  { id: 'source', header: 'How', cell: (entry) => sourceText(entry) },
  { id: 'comment', header: 'Comment', cell: (entry) => entry.Comment || '—' },
  { id: 'label', header: 'Label', cell: (entry) => entry.Label || '—' },
];

const CHANGE_COLUMNS = [
  { id: 'path', header: 'Setting', cell: (change) => pathText(change.Path) },
  { id: 'change', header: 'Change', cell: (change) => change.Change },
  { id: 'before', header: 'Before', cell: (change) => <Box variant="code">{valueText(change.Before)}</Box> },
  { id: 'after', header: 'After', cell: (change) => <Box variant="code">{valueText(change.After)}</Box> },
];

/**
 * The revision history of one configuration profile, in a dialog over the
 * editor: every save, who made it and why, with View, Compare, Label and
 * Restore. Restoring saves the chosen revision as a new one, so `onRestored`
 * is told to reload the editor.
 *
 * One dialog: choosing an action shows it in place of the list, with Back to
 * return, rather than opening a dialog over a dialog.
 */
const RevisionHistoryModal = ({ profile, visible, onDismiss, onRestored = () => {}, unsavedChanges = false }) => {
  const [listing, setListing] = useState(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null);
  const [selected, setSelected] = useState([]);
  // The view shown in place of the list: { kind: 'view' | 'compare' | 'restore' | 'label', ... }.
  const [dialog, setDialog] = useState(null);
  const [comment, setComment] = useState('');
  const [label, setLabel] = useState('');
  // A restore or label failure other than a conflict is shown in its view,
  // which stays open so the admin can retry without typing again.
  const [dialogError, setDialogError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await client.graphql({ query: listRevisionsQuery, variables: { Profile: profile } });
      setListing(result.data?.listConfigurationRevisions || null);
      setSelected([]);
      // A load that worked supersedes the error of one that did not.
      setStatus((previous) => (previous?.type === 'error' ? null : previous));
    } catch (err) {
      setStatus({ type: 'error', text: `Could not load the revision history: ${errorText(err)}` });
    } finally {
      setLoading(false);
    }
  }, [profile]);

  useEffect(() => {
    if (!visible) return;
    setDialog(null);
    setStatus(null);
    load();
  }, [visible, load]);

  const view = async () => {
    const [entry] = selected;
    setBusy(true);
    try {
      const result = await client.graphql({
        query: getRevisionQuery,
        variables: { Profile: profile, Revision: entry.Revision },
      });
      const body = JSON.parse(result.data?.getConfigurationRevision?.Body || '{}');
      setDialog({ kind: 'view', revision: entry.Revision, text: JSON.stringify(body, null, 2) });
    } catch (err) {
      setStatus({ type: 'error', text: `Could not read revision ${entry.Revision}: ${errorText(err)}` });
    } finally {
      setBusy(false);
    }
  };

  const compare = async () => {
    const pair = comparisonPair(selected);
    setBusy(true);
    try {
      const result = await client.graphql({ query: diffRevisionsQuery, variables: { Profile: profile, ...pair } });
      setDialog({ kind: 'compare', ...pair, changes: parseChanges(result.data?.diffConfigurationRevisions?.Changes) });
    } catch (err) {
      setStatus({ type: 'error', text: `Could not compare the revisions: ${errorText(err)}` });
    } finally {
      setBusy(false);
    }
  };

  const openRestore = () => {
    setComment('');
    setDialogError(null);
    setDialog({ kind: 'restore', revision: selected[0].Revision });
  };

  const restore = async () => {
    const { revision } = dialog;
    setBusy(true);
    try {
      const result = await client.graphql({
        query: restoreRevisionMutation,
        variables: {
          input: {
            Profile: profile,
            Revision: revision,
            ExpectedVersion: listing?.Version ?? 0,
            Comment: comment.trim() || null,
          },
        },
      });
      const outcome = result.data?.restoreConfigurationRevision || {};
      setStatus({
        type: 'success',
        text: outcome.Unchanged
          ? `The configuration already holds revision ${revision}; nothing was changed.`
          : `Restored revision ${revision} as revision ${outcome.Revision}. ` +
            'It applies as each setting is next read: at once for most, within 30 seconds for the ' +
            'Transcription Engine switches, and within five minutes for summary prompts and meeting cost prices.',
      });
      setDialog(null);
      if (!outcome.Unchanged) onRestored(outcome.Revision);
      await load();
    } catch (err) {
      if (isConflict(err)) {
        // Retrying cannot help until the history has the newer version, so
        // the list comes back and says to refresh.
        setDialog(null);
        setStatus({ type: 'error', text: CONFLICT_MESSAGE });
      } else {
        setDialogError(`Could not restore revision ${revision}: ${errorText(err)}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const openLabel = () => {
    setLabel(selected[0].Label || '');
    setDialogError(null);
    setDialog({ kind: 'label', revision: selected[0].Revision });
  };

  // Labelling changes nothing in the configuration, so it needs no version: it
  // only marks a revision, and a labelled one is kept however many follow it.
  const saveLabel = async () => {
    const { revision } = dialog;
    const text = label.trim();
    setBusy(true);
    try {
      await client.graphql({
        query: labelRevisionMutation,
        variables: { input: { Profile: profile, Revision: revision, Label: text || null } },
      });
      setStatus({
        type: 'success',
        text: text ? `Labelled revision ${revision} "${text}".` : `Removed the label from revision ${revision}.`,
      });
      setDialog(null);
      await load();
    } catch (err) {
      setDialogError(`Could not label revision ${revision}: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const entries = listing?.Revisions || [];
  const one = selected.length === 1 ? selected[0] : null;
  const back = () => !busy && setDialog(null);
  const dismiss = () => !busy && onDismiss();

  let header = `Revision history of ${profile}`;
  let footer = (
    <Box float="right">
      <Button variant="primary" onClick={dismiss}>
        Close
      </Button>
    </Box>
  );
  let body;

  if (dialog?.kind === 'view') {
    header = `Revision ${dialog.revision} of ${profile}`;
    footer = (
      <Box float="right">
        <Button onClick={back}>Back to the history</Button>
      </Box>
    );
    body = (
      <Box variant="code">
        <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{dialog.text}</pre>
      </Box>
    );
  } else if (dialog?.kind === 'compare') {
    header = `Revision ${dialog.FromRevision} compared with revision ${dialog.ToRevision}`;
    footer = (
      <Box float="right">
        <Button onClick={back}>Back to the history</Button>
      </Box>
    );
    body = (
      <Table
        items={dialog.changes}
        columnDefinitions={CHANGE_COLUMNS}
        wrapLines
        variant="embedded"
        empty={<Box textAlign="center">The two revisions hold the same configuration.</Box>}
      />
    );
  } else if (dialog?.kind === 'restore') {
    header = `Restore revision ${dialog.revision}?`;
    footer = (
      <Box float="right">
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={back} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={restore} loading={busy}>
            Restore
          </Button>
        </SpaceBetween>
      </Box>
    );
    body = (
      <SpaceBetween size="m">
        {dialogError && <Alert type="error">{dialogError}</Alert>}
        <Box>
          {`The whole of the ${profile} profile is replaced by the configuration this revision holds, every ` +
            'section of it. The restore is recorded as a new revision, so the configuration it replaces stays in ' +
            'the history and can be restored in turn.'}
        </Box>
        {unsavedChanges && (
          <Alert type="warning">
            The editor reloads the profile after the restore, so the unsaved changes in it are discarded.
          </Alert>
        )}
        <FormField label="Comment" description="Optional. Recorded with the new revision.">
          <Textarea
            value={comment}
            ariaLabel="Restore comment"
            onChange={({ detail }) => setComment(detail.value)}
            rows={2}
          />
        </FormField>
      </SpaceBetween>
    );
  } else if (dialog?.kind === 'label') {
    header = `Label revision ${dialog.revision}`;
    footer = (
      <Box float="right">
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="link" onClick={back} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" onClick={saveLabel} loading={busy}>
            Save label
          </Button>
        </SpaceBetween>
      </Box>
    );
    body = (
      <SpaceBetween size="m">
        {dialogError && <Alert type="error">{dialogError}</Alert>}
        <FormField
          label="Label"
          description={
            'A labelled revision is kept however many revisions follow it. Leave the label empty to remove it, ' +
            'and the revision is then kept only while it is one of the 20 most recent.'
          }
          constraintText={`${label.length}/${MAX_LABEL_LENGTH} characters`}
        >
          <Input
            value={label}
            ariaLabel="Revision label"
            onChange={({ detail }) => setLabel(detail.value.slice(0, MAX_LABEL_LENGTH))}
          />
        </FormField>
      </SpaceBetween>
    );
  } else {
    body = (
      <SpaceBetween size="m">
        {status && (
          <Alert type={status.type} dismissible onDismiss={() => setStatus(null)}>
            {status.text}
          </Alert>
        )}
        <Table
          items={entries}
          columnDefinitions={COLUMNS}
          trackBy="Revision"
          selectionType="multi"
          selectedItems={selected}
          onSelectionChange={({ detail }) => setSelected(detail.selectedItems.slice(-2))}
          ariaLabels={{
            selectionGroupLabel: 'Revision selection',
            allItemsSelectionLabel: () => 'Select all revisions',
            itemSelectionLabel: (_selection, entry) => `Select revision ${entry.Revision}`,
          }}
          loading={loading}
          loadingText="Loading the revision history"
          variant="embedded"
          wrapLines
          empty={
            <Box textAlign="center" color="inherit">
              No revisions are recorded yet.
            </Box>
          }
          header={
            <Header
              counter={loading ? undefined : `(${entries.length})`}
              description={
                'Every save of this profile records a revision of the whole of it. Select one to view, label or ' +
                'restore it, or two to compare them.'
              }
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    iconName="refresh"
                    onClick={load}
                    disabled={loading || busy}
                    ariaLabel="Refresh the history"
                  />
                  <Button onClick={view} disabled={!one || busy}>
                    View
                  </Button>
                  <Button onClick={compare} disabled={!comparisonPair(selected) || busy}>
                    Compare revisions
                  </Button>
                  <Button onClick={openLabel} disabled={!one || busy}>
                    Label
                  </Button>
                  <Button variant="primary" onClick={openRestore} disabled={!one || one.Published || busy}>
                    Restore
                  </Button>
                </SpaceBetween>
              }
            >
              Revisions
            </Header>
          }
        />
      </SpaceBetween>
    );
  }

  // The dialog is labelled by its header, so each view can be found by it.
  return (
    <Modal visible={visible} onDismiss={dismiss} header={header} footer={footer} size="max">
      {body}
    </Modal>
  );
};

RevisionHistoryModal.propTypes = {
  profile: PropTypes.string.isRequired,
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  onRestored: PropTypes.func,
  // Whether the editor behind the dialog holds unsaved changes a restore would discard.
  unsavedChanges: PropTypes.bool,
};

export default RevisionHistoryModal;
