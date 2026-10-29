/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting's estimated cost, itemised.
 *
 * A collapsed section on the meeting page, like the IDP Accelerator's
 * "Estimated cost" section on a document. The figure is computed by the backend
 * on each read (getCallCost) from the meeting's metering records and the price
 * table, so the panel only asks and renders; it never prices anything itself.
 *
 * It says plainly what kind of number this is: an estimate at dated list
 * prices, with the lines it could not price, the regions it priced at another
 * region's rates, and what it leaves out. See docs/meeting-cost-estimate.md.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  ExpandableSection,
  Popover,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';

import getCallCost from '../../graphql/queries/getCallCost';
import {
  estimateCaveats,
  formatCost,
  formatQuantity,
  formatUnitPrice,
  serviceLabel,
  tableRows,
  unitLabel,
} from '../common/call-cost';

const logger = new ConsoleLogger('CallCostPanel');

const defaultLoadCost = async (callId) => {
  const client = generateClient();
  const response = await client.graphql({ query: getCallCost, variables: { CallId: callId } });
  return response?.data?.getCallCost || null;
};

const DOCS_URL =
  'https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/main/docs/meeting-cost-estimate.md';

// Cell renderers are plain functions of a row, defined at module scope so the
// table's column definitions are not rebuilt as new component types each render.
const renderCost = (row, currency) => {
  if (row.kind === 'subtotal') {
    return <Box fontWeight="bold">{formatCost(row.Cost, currency)}</Box>;
  }
  if (!row.Chargeable) {
    return <Box color="text-status-inactive">not billed separately</Box>;
  }
  if (!row.Priced) {
    return <StatusIndicator type="warning">No price</StatusIndicator>;
  }
  return formatCost(row.Cost, currency);
};

const renderService = (row) => {
  if (row.kind === 'subtotal') {
    return (
      <Box fontWeight="bold">
        {`${row.label} subtotal`}
        {row.unpriced ? ` (${row.unpriced} unpriced)` : ''}
      </Box>
    );
  }
  const notes = [row.Note, row.Details].filter(Boolean);
  const label = serviceLabel(row.Service);
  if (!notes.length) return label;
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="large"
      triggerType="text"
      content={
        <SpaceBetween size="xs">
          {notes.map((text) => (
            <Box key={text}>{text}</Box>
          ))}
        </SpaceBetween>
      }
    >
      {label}
    </Popover>
  );
};

const buildColumns = (currency) => [
  { id: 'service', header: 'Service', cell: renderService },
  { id: 'unit', header: 'Unit', cell: (row) => (row.kind === 'line' ? unitLabel(row.Unit) : '') },
  { id: 'quantity', header: 'Quantity', cell: (row) => (row.kind === 'line' ? formatQuantity(row.Quantity) : '') },
  {
    id: 'unitPrice',
    header: 'Unit price',
    cell: (row) => (row.kind === 'line' && row.Chargeable ? formatUnitPrice(row.UnitPrice, currency) : ''),
  },
  { id: 'cost', header: 'Estimated cost', cell: (row) => renderCost(row, currency) },
];

const CallCostPanel = ({ callId, refreshKey = '', loadCost = defaultLoadCost }) => {
  const [estimate, setEstimate] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [expanded, setExpanded] = useState(false);

  // Only the latest request may update the panel: a refresh pressed while a
  // reload is in flight, or a different meeting opened mid-request, must not be
  // overwritten by the older answer arriving last.
  const latestRequest = useRef(0);

  const refresh = useCallback(async () => {
    if (!callId) return;
    latestRequest.current += 1;
    const request = latestRequest.current;
    setLoading(true);
    setError(null);
    try {
      const result = await loadCost(callId);
      if (request === latestRequest.current) setEstimate(result);
    } catch (err) {
      logger.error('could not load the cost estimate', err);
      if (request === latestRequest.current) {
        // Cleared, so a failed refresh is not shown above an older table as if
        // that table were current.
        setEstimate(null);
        setError(err?.errors?.[0]?.message || err?.message || 'The estimate could not be loaded.');
      }
    } finally {
      if (request === latestRequest.current) setLoading(false);
    }
  }, [callId, loadCost]);

  // Loaded when the meeting changes and whenever refreshKey does. The page keys
  // it on the meeting's status and on its summary, because most of a meeting's
  // usage is recorded as it ends and the summary's own usage is written just
  // before the summary appears. The Virtual Participant's runtime is written as
  // it leaves, which can be after both; the refresh button picks that up.
  useEffect(() => {
    refresh();
  }, [refresh, refreshKey]);

  const currency = estimate?.Currency || 'USD';
  const unmetered = estimate && !estimate.RecordCount;
  let headerDescription = 'Loading…';
  if (error) {
    headerDescription = 'Unavailable';
  } else if (unmetered) {
    headerDescription = 'No metering data';
  } else if (estimate) {
    const incomplete = estimate.UnpricedCount > 0 ? ' (incomplete)' : '';
    headerDescription = `${formatCost(estimate.TotalCost, currency)} estimated${incomplete}`;
  }

  return (
    <ExpandableSection
      variant="container"
      expanded={expanded}
      onChange={({ detail }) => setExpanded(detail.expanded)}
      headerText="Estimated cost"
      headerDescription={headerDescription}
      headerActions={
        <Button iconName="refresh" variant="icon" ariaLabel="Refresh estimate" loading={loading} onClick={refresh} />
      }
    >
      <SpaceBetween size="m">
        {error && <Alert type="error">{error}</Alert>}
        {unmetered && (
          <Box>
            No usage has been recorded for this meeting. Meetings held before cost metering was deployed have none, and
            a meeting that has only just started may not have written any yet.
          </Box>
        )}
        {estimate && !unmetered && (
          <>
            <Alert type="info" header="This is an estimate">
              {`Computed from usage recorded for this meeting, at public on-demand list prices as of ` +
                `${estimate.PricesAsOf || 'an unrecorded date'}, first volume tier. Your bill can differ: free ` +
                'tier, Savings Plans, volume discounts and private pricing are not applied, and resources that ' +
                'run whether or not a meeting is in progress are not included.'}
            </Alert>
            {estimateCaveats(estimate).map((text) => (
              <Alert key={text} type="warning">
                {text}
              </Alert>
            ))}
            <Table
              variant="embedded"
              wrapLines
              items={tableRows(estimate)}
              trackBy="id"
              columnDefinitions={buildColumns(currency)}
              footer={
                <Box textAlign="right" fontWeight="bold" fontSize="heading-s">
                  {`Total: ${formatCost(estimate.TotalCost, currency)}`}
                </Box>
              }
            />
            <ExpandableSection headerText="What this estimate does not include" variant="footer">
              <ul>
                {(estimate.NotIncluded || []).map((text) => (
                  <li key={text}>{text}</li>
                ))}
              </ul>
            </ExpandableSection>
            <ColumnLayout columns={3} variant="text-grid">
              <div>
                <Box variant="awsui-key-label">Prices as of</Box>
                <div>{estimate.PricesAsOf || '—'}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Region</Box>
                <div>{estimate.Region || '—'}</div>
              </div>
              <div>
                <Box variant="awsui-key-label">Computed</Box>
                <div>{estimate.ComputedAt ? new Date(estimate.ComputedAt).toLocaleString() : '—'}</div>
              </div>
            </ColumnLayout>
            <Box variant="small" color="text-body-secondary">
              {estimate.PriceSource}{' '}
              <a href={DOCS_URL} target="_blank" rel="noopener noreferrer">
                How this is estimated
              </a>
            </Box>
          </>
        )}
      </SpaceBetween>
    </ExpandableSection>
  );
};

CallCostPanel.propTypes = {
  callId: PropTypes.string.isRequired,
  // Changes to this re-load the estimate; the page passes the meeting status.
  refreshKey: PropTypes.string,
  // Replaceable for tests.
  loadCost: PropTypes.func,
};

export default CallCostPanel;
