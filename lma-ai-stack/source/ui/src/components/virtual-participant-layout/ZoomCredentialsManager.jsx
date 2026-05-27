/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/jsx-props-no-spreading */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import {
  Alert,
  Box,
  Button,
  Form,
  FormField,
  Input,
  Modal,
  SpaceBetween,
  StatusIndicator,
} from '@cloudscape-design/components';

const client = generateClient();

const Q_GET_STATUS = /* GraphQL */ `
  query GetMyZoomCredentialsStatus {
    getMyZoomCredentialsStatus {
      present
      username
      lastUpdatedAt
    }
  }
`;

const M_SET = /* GraphQL */ `
  mutation SetMyZoomCredentials($input: SetZoomCredentialsInput!) {
    setMyZoomCredentials(input: $input) {
      present
      username
      lastUpdatedAt
    }
  }
`;

const M_DELETE = /* GraphQL */ `
  mutation DeleteMyZoomCredentials {
    deleteMyZoomCredentials
  }
`;

const ZoomCredentialsManager = ({ onChange }) => {
  const [status, setStatus] = useState({ present: false, username: null, lastUpdatedAt: null });
  const [loading, setLoading] = useState(true);
  const [editVisible, setEditVisible] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [info, setInfo] = useState(null);
  const [form, setForm] = useState({ username: '', password: '' });

  // Hold onChange in a ref so refresh() doesn't change identity when the
  // parent re-renders with a new callback reference. Without this, the
  // useEffect below re-runs on every parent render and we flicker.
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await client.graphql({ query: Q_GET_STATUS });
      const s = r?.data?.getMyZoomCredentialsStatus || { present: false, username: null, lastUpdatedAt: null };
      setStatus(s);
      onChangeRef.current?.(s);
    } catch (e) {
      setError(e?.errors?.[0]?.message || e?.message || 'Failed to load credentials status');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    // refresh is stable (empty dep list); only fire on mount.
  }, [refresh]);

  const openEdit = () => {
    setForm({ username: status.username || '', password: '' });
    setEditVisible(true);
    setError(null);
    setInfo(null);
  };

  const submit = async () => {
    if (!form.username || !form.password) {
      setError('Both username and password are required');
      return;
    }
    if (form.password.length < 8) {
      setError('Password must be at least 8 characters');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const r = await client.graphql({
        query: M_SET,
        variables: { input: { username: form.username.trim(), password: form.password } },
      });
      const s = r?.data?.setMyZoomCredentials || { present: true, username: form.username, lastUpdatedAt: null };
      setStatus(s);
      if (onChange) onChange(s);
      setEditVisible(false);
      setInfo(
        'Saved. Tip — sign in to Zoom on your laptop with this account at least once before ' +
          'relying on LMA. Brand-new accounts that only ever sign in from cloud IPs can still ' +
          "trigger Zoom's bot detection.",
      );
    } catch (e) {
      setError(e?.errors?.[0]?.message || e?.message || 'Failed to save credentials');
    } finally {
      setSubmitting(false);
    }
  };

  const remove = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await client.graphql({ query: M_DELETE });
      const s = { present: false, username: null, lastUpdatedAt: null };
      setStatus(s);
      if (onChange) onChange(s);
      setInfo('Zoom credentials removed.');
    } catch (e) {
      setError(e?.errors?.[0]?.message || e?.message || 'Failed to remove credentials');
    } finally {
      setSubmitting(false);
    }
  };

  let statusContent;
  if (loading) {
    statusContent = <StatusIndicator type="loading">Loading Zoom account status...</StatusIndicator>;
  } else if (status.present) {
    statusContent = (
      <SpaceBetween direction="horizontal" size="s">
        <StatusIndicator type="success">Zoom account: signed in as {status.username || '(unknown)'}</StatusIndicator>
        <Button onClick={openEdit} disabled={submitting}>
          Update
        </Button>
        <Button onClick={remove} disabled={submitting}>
          Remove
        </Button>
      </SpaceBetween>
    );
  } else {
    statusContent = (
      <SpaceBetween direction="horizontal" size="s">
        <StatusIndicator type="warning">
          No Zoom account configured — VP will join as a guest (some meetings may block this).
        </StatusIndicator>
        <Button onClick={openEdit} disabled={submitting}>
          Add Zoom credentials
        </Button>
        <Button variant="link" href="https://zoom.us/signup" target="_blank" external>
          Create a Zoom account
        </Button>
      </SpaceBetween>
    );
  }

  return (
    <Box>
      <SpaceBetween direction="vertical" size="xs">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}
        {info && (
          <Alert type="info" dismissible onDismiss={() => setInfo(null)}>
            {info}
          </Alert>
        )}
        {statusContent}
      </SpaceBetween>
      <Modal
        visible={editVisible}
        onDismiss={() => setEditVisible(false)}
        header="Zoom account for LMA"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setEditVisible(false)} disabled={submitting}>
                Cancel
              </Button>
              <Button variant="primary" onClick={submit} loading={submitting}>
                Save
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Form>
          <SpaceBetween direction="vertical" size="m">
            <Box variant="small">
              Stored securely in AWS Secrets Manager and used only when LMA joins a Zoom meeting on your behalf. The
              password is never returned to your browser. Two-factor or CAPTCHA challenges still require manual action
              via the LMA viewer.
            </Box>
            <FormField label="Zoom email or username" stretch>
              <Input
                value={form.username}
                onChange={({ detail }) => setForm((p) => ({ ...p, username: detail.value }))}
                placeholder="you@example.com"
                autoComplete={false}
              />
            </FormField>
            <FormField label="Zoom password" stretch>
              <Input
                type="password"
                value={form.password}
                onChange={({ detail }) => setForm((p) => ({ ...p, password: detail.value }))}
                placeholder="Zoom password"
                autoComplete={false}
              />
            </FormField>
          </SpaceBetween>
        </Form>
      </Modal>
    </Box>
  );
};

ZoomCredentialsManager.propTypes = {
  onChange: PropTypes.func,
};

ZoomCredentialsManager.defaultProps = {
  onChange: undefined,
};

export default ZoomCredentialsManager;
