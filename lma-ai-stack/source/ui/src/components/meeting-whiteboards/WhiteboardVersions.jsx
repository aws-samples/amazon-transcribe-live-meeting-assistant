/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/prop-types */
import React, { useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  ExpandableSection,
  Modal,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';
import { ConsoleLogger } from 'aws-amplify/utils';

import { compareVersions, errorMessage } from './whiteboard-model';

const logger = new ConsoleLogger('WhiteboardVersions');

const DIFF_STYLE = {
  added: { background: 'rgba(3, 116, 34, 0.12)' },
  removed: { background: 'rgba(209, 50, 18, 0.12)', textDecoration: 'line-through' },
  same: {},
};
const DIFF_MARK = { added: '+ ', removed: '- ', same: '  ' };

const Diff = ({ lines }) => (
  <pre className="whiteboard-diff" style={{ whiteSpace: 'pre-wrap', margin: 0 }}>
    {lines.map((line, index) => (
      // eslint-disable-next-line react/no-array-index-key
      <div key={index} style={DIFF_STYLE[line.type]}>
        {DIFF_MARK[line.type]}
        {line.text}
      </div>
    ))}
  </pre>
);

/**
 * A whiteboard's versions, newest first. Choosing one shows it, read-only,
 * panel by panel against the whiteboard as it is now; Restore brings its
 * title and panels back as a new version, made by the user.
 */
const WhiteboardVersions = ({ board, visible, readOnly, onDismiss, listVersions, readVersion, onRestore }) => {
  const [items, setItems] = useState([]);
  const [nextToken, setNextToken] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [snapshot, setSnapshot] = useState(null);
  const [restoring, setRestoring] = useState(false);

  const loadPage = async (token) => {
    setLoading(true);
    try {
      const page = await listVersions(board.WhiteboardId, token);
      setItems((previous) => (token ? [...previous, ...page.items] : page.items));
      setNextToken(page.nextToken);
      setError(null);
    } catch (err) {
      logger.error('could not list the versions', err);
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!visible) return;
    setSelected(null);
    setSnapshot(null);
    loadPage(null);
  }, [visible, board?.WhiteboardId]);

  const choose = async (version) => {
    setSelected(version);
    setSnapshot(null);
    try {
      setSnapshot(await readVersion(board.WhiteboardId, version.Version));
    } catch (err) {
      logger.error('could not read the version', err);
      setError(errorMessage(err));
    }
  };

  const restore = async () => {
    setRestoring(true);
    const result = await onRestore(selected.Version);
    setRestoring(false);
    if (result?.error) setError(result.error);
    else onDismiss();
  };

  const isCurrent = selected && Number(selected.Version) === Number(board.Version);

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header={`Versions of ${board.Title}`}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onDismiss}>Close</Button>
            {!readOnly && (
              <Button variant="primary" disabled={!snapshot || isCurrent} loading={restoring} onClick={restore}>
                Restore this version
              </Button>
            )}
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        {error && <Alert type="error">{error}</Alert>}
        <Table
          variant="embedded"
          loading={loading && items.length === 0}
          loadingText="Loading versions"
          selectionType="single"
          selectedItems={selected ? [selected] : []}
          onSelectionChange={({ detail }) => choose(detail.selectedItems[0])}
          trackBy="Version"
          items={items}
          empty={<Box color="text-status-inactive">No versions.</Box>}
          columnDefinitions={[
            { id: 'version', header: 'Version', cell: (v) => v.Version },
            { id: 'time', header: 'When', cell: (v) => (v.UpdatedAt ? new Date(v.UpdatedAt).toLocaleString() : '') },
            {
              id: 'source',
              header: 'By',
              cell: (v) => (v.Source === 'assistant' ? 'Assistant' : v.UpdatedBy || 'A user'),
            },
            { id: 'changed', header: 'Panels changed', cell: (v) => (v.ChangedPanels || []).length },
          ]}
        />
        {nextToken && (
          <Button onClick={() => loadPage(nextToken)} loading={loading}>
            Show older versions
          </Button>
        )}
        {selected && !snapshot && <StatusIndicator type="loading">Reading version {selected.Version}</StatusIndicator>}
        {snapshot && (
          <SpaceBetween size="xs">
            <Box variant="h4">
              Version {snapshot.Version} against the whiteboard now{isCurrent ? ' (this is the current version)' : ''}
            </Box>
            {compareVersions(snapshot, board).map((row) => (
              <ExpandableSection
                key={row.panelId}
                headerText={`${row.title} — ${row.status}`}
                defaultExpanded={row.status !== 'same'}
              >
                <Diff lines={row.diff} />
              </ExpandableSection>
            ))}
          </SpaceBetween>
        )}
      </SpaceBetween>
    </Modal>
  );
};

export default WhiteboardVersions;
