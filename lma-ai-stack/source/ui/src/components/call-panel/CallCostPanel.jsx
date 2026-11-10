/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The meeting's estimated cost, itemised.
 *
 * A collapsed section on the meeting page's Details tab, like the IDP
 * Accelerator's "Estimated cost" section on a document; the cost chip in the
 * page header opens the same breakdown in a popover. The figure is computed by the backend
 * on each read (getCallCost) from the meeting's metering records and the price
 * table, so the panel only asks and renders; it never prices anything itself.
 *
 * It says plainly what kind of number this is: an estimate at dated list
 * prices, with the lines it could not price, the regions it priced at another
 * region's rates, and what it leaves out. See docs/meeting-cost-estimate.md.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Badge,
  Box,
  Button,
  ColumnLayout,
  ExpandableSection,
  Link,
  Popover,
  SpaceBetween,
  StatusIndicator,
  Table,
} from '@cloudscape-design/components';

import useCallCost, { defaultLoadCost } from '../../hooks/use-call-cost';
import {
  estimateCaveats,
  formatCost,
  formatQuantity,
  formatUnitPrice,
  lineCalculation,
  priceSourceLabel,
  serviceLabel,
  tableRows,
  unitLabel,
} from '../common/call-cost';

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
  // The whole calculation as the cell's title, so it can be read as one statement.
  return <span title={lineCalculation(row, currency)}>{formatCost(row.Cost, currency)}</span>;
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

// Where the line's price came from -- the shipped table's source, or a price
// set in the active configuration profile -- with the full sentence on selection.
const renderPriceSource = (row, currency) => {
  if (row.kind !== 'line' || !row.Chargeable) return '';
  const label = priceSourceLabel(row);
  const shipped = row.ShippedUnitPrice;
  const trigger =
    row.PriceOrigin === 'custom' ? (
      <SpaceBetween direction="horizontal" size="xxs">
        <Badge color="blue">{label}</Badge>
        {shipped !== null && shipped !== undefined && (
          <Box variant="small">{`shipped ${formatUnitPrice(shipped, currency)}`}</Box>
        )}
      </SpaceBetween>
    ) : (
      label
    );
  if (!row.PriceSource) return trigger;
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="large"
      triggerType="text"
      header="Where this price comes from"
      content={
        <SpaceBetween size="xs">
          <Box>{row.PriceSource}</Box>
          {row.PriceSourceUrl && (
            <Link external href={row.PriceSourceUrl}>
              Pricing page
            </Link>
          )}
        </SpaceBetween>
      }
    >
      {trigger}
    </Popover>
  );
};

// The columns read as the calculation they show: usage, times the unit price,
// equals the cost.
const buildColumns = (currency) => [
  { id: 'service', header: 'Service', cell: renderService },
  {
    id: 'usage',
    header: 'Usage',
    cell: (row) => (row.kind === 'line' ? `${formatQuantity(row.Quantity)} ${unitLabel(row.Unit)}` : ''),
  },
  {
    id: 'unitPrice',
    header: '× Unit price',
    cell: (row) => (row.kind === 'line' && row.Chargeable ? formatUnitPrice(row.UnitPrice, currency) : ''),
  },
  { id: 'cost', header: '= Estimated cost', cell: (row) => renderCost(row, currency) },
  { id: 'source', header: 'Price source', cell: (row) => renderPriceSource(row, currency) },
];

/**
 * The estimate in a few words: "$0.1234 estimated", "No metering data", and so
 * on. `short` leaves out the word "estimated", for the header's cost chip.
 */
export const costSummaryText = ({ estimate, error }, { short = false } = {}) => {
  if (error) return 'Unavailable';
  if (!estimate) return 'Loading…';
  if (!estimate.RecordCount) return 'No metering data';
  const incomplete = estimate.UnpricedCount > 0 ? ' (incomplete)' : '';
  return `${formatCost(estimate.TotalCost, estimate.Currency || 'USD')}${short ? '' : ' estimated'}${incomplete}`;
};

/** The itemised estimate, its caveats and where its prices come from. */
export const CallCostBreakdown = ({ estimate = null, error = null }) => {
  const currency = estimate?.Currency || 'USD';
  const unmetered = estimate && !estimate.RecordCount;
  return (
    <SpaceBetween size="m">
      {error && <Alert type="error">{error}</Alert>}
      {unmetered && (
        <Box>
          No usage has been recorded for this meeting. Meetings held before cost metering was deployed have none, and a
          meeting that has only just started may not have written any yet.
        </Box>
      )}
      {estimate && !unmetered && (
        <>
          <Alert type="info" header="This is an estimate">
            {`Each line is the usage recorded for this meeting times a unit price: a public on-demand list ` +
              `price as of ${estimate.PricesAsOf || 'an unrecorded date'} (first volume tier), unless an ` +
              'administrator set a price of their own in the Meeting Cost Prices configuration. Your bill can ' +
              'differ: free tier, Savings Plans and volume discounts are not applied, and resources that run ' +
              'whether or not a meeting is in progress are not included. Select a price source to see where ' +
              'it comes from.'}
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
  );
};

CallCostBreakdown.propTypes = {
  // eslint-disable-next-line react/forbid-prop-types
  estimate: PropTypes.object,
  error: PropTypes.string,
};

/**
 * The estimate as a section of the page. Reads it itself, or shows `state`
 * from a useCallCost the page already holds (the meeting page does, so the
 * header's cost chip and this section share one read).
 */
const CallCostPanel = ({
  callId,
  refreshKey = '',
  loadCost = defaultLoadCost,
  state = null,
  defaultExpanded = false,
}) => {
  const own = useCallCost(state ? null : callId, refreshKey, loadCost);
  const { estimate, loading, error, refresh } = state || own;
  const [expanded, setExpanded] = useState(defaultExpanded);

  return (
    <ExpandableSection
      variant="container"
      expanded={expanded}
      onChange={({ detail }) => setExpanded(detail.expanded)}
      headerText="Estimated cost"
      headerDescription={costSummaryText({ estimate, error })}
      headerActions={
        <Button iconName="refresh" variant="icon" ariaLabel="Refresh estimate" loading={loading} onClick={refresh} />
      }
    >
      <CallCostBreakdown estimate={estimate} error={error} />
    </ExpandableSection>
  );
};

CallCostPanel.propTypes = {
  callId: PropTypes.string.isRequired,
  // Changes to this re-load the estimate; the page passes the meeting status.
  refreshKey: PropTypes.string,
  // Replaceable for tests.
  loadCost: PropTypes.func,
  // A useCallCost result to show instead of reading the estimate here.
  state: PropTypes.shape({
    // eslint-disable-next-line react/forbid-prop-types
    estimate: PropTypes.object,
    loading: PropTypes.bool,
    error: PropTypes.string,
    refresh: PropTypes.func,
  }),
  defaultExpanded: PropTypes.bool,
};

export default CallCostPanel;
