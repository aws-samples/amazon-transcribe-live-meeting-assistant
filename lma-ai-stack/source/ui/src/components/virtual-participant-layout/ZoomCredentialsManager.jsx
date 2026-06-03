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

const Q_GET_CREDS_STATUS = /* GraphQL */ `
  query GetMyZoomCredentialsStatus {
    getMyZoomCredentialsStatus {
      present
      username
      lastUpdatedAt
    }
  }
`;

const M_SET_CREDS = /* GraphQL */ `
  mutation SetMyZoomCredentials($input: SetZoomCredentialsInput!) {
    setMyZoomCredentials(input: $input) {
      present
      username
      lastUpdatedAt
    }
  }
`;

const M_DELETE_CREDS = /* GraphQL */ `
  mutation DeleteMyZoomCredentials {
    deleteMyZoomCredentials
  }
`;

const ZoomCredentialsManager = ({ onChange }) => {
  const [creds, setCreds] = useState({ present: false, username: null, lastUpdatedAt: null });
  const [loading, setLoading] = useState(true);
  const [editVisible, setEditVisible] = useState(false);
  const [confirmVisible, setConfirmVisible] = useState(null); // 'remove-creds' | null
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [info, setInfo] = useState(null);
  const [form, setForm] = useState({ username: '', password: '' });

  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const credsResp = await client.graphql({ query: Q_GET_CREDS_STATUS });
      const c = credsResp?.data?.getMyZoomCredentialsStatus || { present: false, username: null, lastUpdatedAt: null };
      setCreds(c);
      onChangeRef.current?.(c);
    } catch (e) {
      setError(e?.errors?.[0]?.message || e?.message || 'Failed to load Zoom account status');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const openEdit = () => {
    setForm({ username: creds.username || '', password: '' });
    setEditVisible(true);
    setError(null);
    setInfo(null);
  };

  const submitCreds = async () => {
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
        query: M_SET_CREDS,
        variables: { input: { username: form.username.trim(), password: form.password } },
      });
      const c = r?.data?.setMyZoomCredentials || { present: true, username: form.username, lastUpdatedAt: null };
      setCreds(c);
      if (onChange) onChange(c);
      setEditVisible(false);
      setInfo(
        'Saved. Tip — sign in to Zoom on your laptop with this account at least once before ' +
          'relying on LMA. A brand-new account that only ever signs in from cloud IPs is more ' +
          'likely to hit a Zoom verification step.',
      );
    } catch (e) {
      setError(e?.errors?.[0]?.message || e?.message || 'Failed to save credentials');
    } finally {
      setSubmitting(false);
    }
  };

  const removeCreds = async () => {
    setSubmitting(true);
    setError(null);
    try {
      // deleteMyZoomCredentials wipes both the secret and the saved Chrome
      // profile (so the next sign-in starts from a clean slate). The stored
      // profile is surfaced separately by ChromeProfileManager, which reloads
      // its own status on its next mount.
      await client.graphql({ query: M_DELETE_CREDS });
      const c = { present: false, username: null, lastUpdatedAt: null };
      setCreds(c);
      if (onChange) onChange(c);
      setConfirmVisible(null);
      setInfo('Zoom credentials and stored Chrome profile removed.');
    } catch (e) {
      setError(e?.errors?.[0]?.message || e?.message || 'Failed to remove credentials');
    } finally {
      setSubmitting(false);
    }
  };

  // Credentials line.
  let credsLine;
  if (loading) {
    credsLine = <StatusIndicator type="loading">Loading Zoom account status...</StatusIndicator>;
  } else if (creds.present) {
    credsLine = (
      <SpaceBetween direction="horizontal" size="s">
        <StatusIndicator type="success">Zoom account: signed in as {creds.username || '(unknown)'}</StatusIndicator>
        <Button onClick={openEdit} disabled={submitting}>
          Update
        </Button>
        <Button onClick={() => setConfirmVisible('remove-creds')} disabled={submitting}>
          Remove Credentials
        </Button>
      </SpaceBetween>
    );
  } else {
    credsLine = (
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
        {credsLine}
      </SpaceBetween>

      {/* Add / update credentials modal */}
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
              <Button variant="primary" onClick={submitCreds} loading={submitting}>
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

      {/* Remove credentials confirmation */}
      <Modal
        visible={confirmVisible === 'remove-creds'}
        onDismiss={() => setConfirmVisible(null)}
        header="Remove Zoom credentials?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setConfirmVisible(null)} disabled={submitting}>
                Cancel
              </Button>
              <Button variant="primary" onClick={removeCreds} loading={submitting}>
                Remove
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween direction="vertical" size="s">
          <div>
            This will delete your saved Zoom username and password from Secrets Manager <strong>and</strong> wipe your
            stored Chrome profile (cookies, &quot;remember this device&quot; markers, etc.).
          </div>
          <div>The next meeting LMA joins will be as a guest and may be blocked by meetings that disallow guests.</div>
        </SpaceBetween>
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
