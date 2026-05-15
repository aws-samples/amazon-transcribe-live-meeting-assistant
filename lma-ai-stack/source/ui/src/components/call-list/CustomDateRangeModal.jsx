/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React, { useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Button,
  DatePicker,
  FormField,
  Modal,
  SpaceBetween,
  TimeInput,
} from '@cloudscape-design/components';

/**
 * Custom absolute-only date/time range picker modal.
 *
 * Modelled on the IDP accelerator's `DateRangeModal`.  This shows explicit
 * Start date / Start time / End date / End time fields (no relative
 * presets — those are already covered by the "Last N hours/days" dropdown
 * in the header).
 *
 * Calls `onApply({ startDateIso, endDateIso })` with UTC ISO 8601 strings.
 */
const CustomDateRangeModal = ({ visible, onDismiss, onApply }) => {
  // Default to the last 7 days.
  const now = new Date();
  const weekAgo = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);

  const [startDate, setStartDate] = useState(weekAgo.toISOString().split('T')[0]);
  const [startTime, setStartTime] = useState('00:00:00');
  const [endDate, setEndDate] = useState(now.toISOString().split('T')[0]);
  const [endTime, setEndTime] = useState('23:59:59');
  const [error, setError] = useState(null);

  const handleApply = () => {
    setError(null);

    if (!startDate || !endDate) {
      setError('Both start and end dates are required.');
      return;
    }

    const startDateIso = `${startDate}T${startTime || '00:00:00'}.000Z`;
    const endDateIso = `${endDate}T${endTime || '23:59:59'}.000Z`;

    if (startDateIso >= endDateIso) {
      setError('Start date/time must be before end date/time.');
      return;
    }

    const startMs = new Date(startDateIso).getTime();
    const endMs = new Date(endDateIso).getTime();
    const daysDiff = (endMs - startMs) / (1000 * 60 * 60 * 24);
    if (daysDiff > 365) {
      setError('Date range cannot exceed 365 days. Please select a shorter range.');
      return;
    }

    onApply({ startDateIso, endDateIso });
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Select custom date range"
      size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>
              Cancel
            </Button>
            <Button variant="primary" onClick={handleApply}>
              Apply
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && <Alert type="error">{error}</Alert>}
        <SpaceBetween size="m" direction="horizontal">
          <FormField label="Start date" constraintText="YYYY/MM/DD">
            <DatePicker
              value={startDate}
              onChange={({ detail }) => setStartDate(detail.value)}
              placeholder="YYYY/MM/DD"
              openCalendarAriaLabel={(selectedDate) =>
                `Choose start date${selectedDate ? `, selected date is ${selectedDate}` : ''}`
              }
            />
          </FormField>
          <FormField label="Start time (UTC)" constraintText="HH:mm:ss">
            <TimeInput
              value={startTime}
              onChange={({ detail }) => setStartTime(detail.value)}
              format="hh:mm:ss"
              placeholder="00:00:00"
            />
          </FormField>
        </SpaceBetween>
        <SpaceBetween size="m" direction="horizontal">
          <FormField label="End date" constraintText="YYYY/MM/DD">
            <DatePicker
              value={endDate}
              onChange={({ detail }) => setEndDate(detail.value)}
              placeholder="YYYY/MM/DD"
              openCalendarAriaLabel={(selectedDate) =>
                `Choose end date${selectedDate ? `, selected date is ${selectedDate}` : ''}`
              }
            />
          </FormField>
          <FormField label="End time (UTC)" constraintText="HH:mm:ss">
            <TimeInput
              value={endTime}
              onChange={({ detail }) => setEndTime(detail.value)}
              format="hh:mm:ss"
              placeholder="23:59:59"
            />
          </FormField>
        </SpaceBetween>
        <Box variant="small" color="text-body-secondary">
          Meetings are queried server-side for custom date ranges. Results are paginated for performance. All times are
          in UTC.
        </Box>
      </SpaceBetween>
    </Modal>
  );
};

CustomDateRangeModal.propTypes = {
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  onApply: PropTypes.func.isRequired,
};

export default CustomDateRangeModal;
