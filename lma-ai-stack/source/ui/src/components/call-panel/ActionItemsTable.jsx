/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The action items listed with the meeting's summary (Call.ActionItems), when
 * its summary profile lists structured action items. An owner is a name as
 * said in the meeting; an item with none, or with no due date, says so plainly.
 * See docs/transcript-summarization.md#structured-action-items.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Badge, Box, Header, Table } from '@cloudscape-design/components';

import { formatElapsed } from '../common/meeting-preparation';

/** The stored items that can be shown: each with its text. */
export const shownActionItems = (items) =>
  (Array.isArray(items) ? items : []).filter((item) => item && typeof item.Text === 'string' && item.Text.trim());

const ownerCell = (item) => (item.Owner ? item.Owner : <Badge color="red">No owner</Badge>);

const dueCell = (item) => {
  if (item.DueDate) return item.DueText ? `${item.DueDate} (${item.DueText})` : item.DueDate;
  if (item.DueText) return item.DueText;
  return <Badge color="grey">No due date</Badge>;
};

const timeCell = (item) =>
  item.StartTime === null || item.StartTime === undefined ? (
    <Box color="text-status-inactive">-</Box>
  ) : (
    <Box variant="code">{formatElapsed(item.StartTime)}</Box>
  );

const COLUMNS = [
  { id: 'text', header: 'Action', cell: (item) => item.Text, isRowHeader: true },
  { id: 'owner', header: 'Owner', cell: ownerCell },
  { id: 'due', header: 'Due', cell: dueCell },
  { id: 'time', header: 'Time', cell: timeCell },
];

const ActionItemsTable = ({ items = undefined }) => {
  const shown = shownActionItems(items);
  if (!shown.length) return null;
  return (
    <Table
      variant="embedded"
      header={
        <Header variant="h3" counter={`(${shown.length})`}>
          Action items
        </Header>
      }
      items={shown}
      trackBy={(item) => `${item.SegmentId || ''}|${item.Text}`}
      columnDefinitions={COLUMNS}
    />
  );
};

ActionItemsTable.propTypes = {
  items: PropTypes.arrayOf(
    PropTypes.shape({
      Text: PropTypes.string,
      Owner: PropTypes.string,
      DueDate: PropTypes.string,
      DueText: PropTypes.string,
      StartTime: PropTypes.number,
      SegmentId: PropTypes.string,
    }),
  ),
};

export default ActionItemsTable;
