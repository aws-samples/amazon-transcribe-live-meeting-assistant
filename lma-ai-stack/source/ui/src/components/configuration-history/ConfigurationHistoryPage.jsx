/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  FormField,
  Header,
  Modal,
  SpaceBetween,
  Spinner,
  Table,
  Textarea,
} from '@cloudscape-design/components';

import AdminOnly from '../common/AdminOnly';
import { isConflict } from '../common/config-conflict';
import {
  PROFILE,
  comparisonPair,
  diffRevisionsQuery,
  getRevisionQuery,
  listRevisionsQuery,
  parseChanges,
  pathText,
  restoreRevisionMutation,
  sectionsText,
  sourceText,
  valueText,
} from './history';

const client = generateClient();

const CONFLICT_MESSAGE =
  'Someone saved the configuration after this page loaded it. Refresh the history to see their change, ' +
  'then restore again if you still want to.';

const errorText = (err) => err?.errors?.[0]?.message || err?.message || String(err);

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

export const ConfigurationHistoryContent = () => {
  const [listing, setListing] = useState(null);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null);
  const [selected, setSelected] = useState([]);
  // One open dialog at a time: { kind: 'view' | 'compare' | 'restore', ... }.
  const [dialog, setDialog] = useState(null);
  const [comment, setComment] = useState('');
  // A restore failure other than a conflict is shown inside the dialog, which
  // stays open so the admin can retry without typing the comment again.
  const [dialogError, setDialogError] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await client.graphql({ query: listRevisionsQuery, variables: { Profile: PROFILE } });
      setListing(result.data?.listConfigurationRevisions || null);
      setSelected([]);
      // A load that worked supersedes the error of one that did not.
      setStatus((previous) => (previous?.type === 'error' ? null : previous));
    } catch (err) {
      setStatus({ type: 'error', text: `Could not load the configuration history: ${errorText(err)}` });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const view = async () => {
    const [entry] = selected;
    setBusy(true);
    try {
      const result = await client.graphql({
        query: getRevisionQuery,
        variables: { Profile: PROFILE, Revision: entry.Revision },
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
      const result = await client.graphql({ query: diffRevisionsQuery, variables: { Profile: PROFILE, ...pair } });
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
            Profile: PROFILE,
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
      await load();
    } catch (err) {
      if (isConflict(err)) {
        // Retrying cannot help until the page has the newer version, so the
        // dialog closes and the page says to refresh.
        setDialog(null);
        setStatus({ type: 'error', text: CONFLICT_MESSAGE });
      } else {
        setDialogError(`Could not restore revision ${revision}: ${errorText(err)}`);
      }
    } finally {
      setBusy(false);
    }
  };

  const entries = listing?.Revisions || [];
  const one = selected.length === 1 ? selected[0] : null;

  return (
    <SpaceBetween size="l">
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
        loading={loading}
        loadingText="Loading the configuration history"
        variant="full-page"
        wrapLines
        empty={
          <Box textAlign="center" color="inherit">
            No revisions are recorded yet.
          </Box>
        }
        header={
          <Header
            variant="h1"
            counter={loading ? undefined : `(${entries.length})`}
            description={
              'Every save from a configuration page records a revision of the whole configuration. ' +
              'Select one to view or restore it, or two to compare them.'
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button iconName="refresh" onClick={load} disabled={loading || busy} ariaLabel="Refresh" />
                <Button onClick={view} disabled={!one || busy}>
                  View
                </Button>
                <Button onClick={compare} disabled={!comparisonPair(selected) || busy}>
                  Compare
                </Button>
                <Button variant="primary" onClick={openRestore} disabled={!one || one.Published || busy}>
                  Restore
                </Button>
              </SpaceBetween>
            }
          >
            Configuration History
          </Header>
        }
      />

      <Modal
        visible={dialog?.kind === 'view'}
        onDismiss={() => setDialog(null)}
        header={`Revision ${dialog?.revision}`}
        size="large"
      >
        <Box variant="code">
          <pre style={{ whiteSpace: 'pre-wrap', margin: 0 }}>{dialog?.text}</pre>
        </Box>
      </Modal>

      <Modal
        visible={dialog?.kind === 'compare'}
        onDismiss={() => setDialog(null)}
        header={`Revision ${dialog?.FromRevision} compared with revision ${dialog?.ToRevision}`}
        size="max"
      >
        <Table
          items={dialog?.changes || []}
          columnDefinitions={CHANGE_COLUMNS}
          wrapLines
          variant="embedded"
          empty={<Box textAlign="center">The two revisions hold the same configuration.</Box>}
        />
      </Modal>

      <Modal
        visible={dialog?.kind === 'restore'}
        onDismiss={() => !busy && setDialog(null)}
        header={`Restore revision ${dialog?.revision}?`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDialog(null)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={restore} loading={busy}>
                Restore
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {dialogError && <Alert type="error">{dialogError}</Alert>}
          <Box>
            The whole configuration is replaced by the one this revision holds: the settings of every configuration page
            — summary prompts, Nova Sonic, chat buttons and transcription engine — and the meeting cost prices set
            through the pricing API. The restore is recorded as a new revision, so the configuration it replaces stays
            in the history and can be restored in turn.
          </Box>
          <FormField label="Comment" description="Optional. Recorded with the new revision.">
            <Textarea value={comment} onChange={({ detail }) => setComment(detail.value)} rows={2} />
          </FormField>
        </SpaceBetween>
      </Modal>

      {busy && !dialog && <Spinner />}
    </SpaceBetween>
  );
};

// The history of a deployment-wide configuration, and putting an earlier state of
// it back, is for Admin users.
const ConfigurationHistoryPage = () => (
  <AdminOnly feature="the configuration">
    <ConfigurationHistoryContent />
  </AdminOnly>
);

export default ConfigurationHistoryPage;
