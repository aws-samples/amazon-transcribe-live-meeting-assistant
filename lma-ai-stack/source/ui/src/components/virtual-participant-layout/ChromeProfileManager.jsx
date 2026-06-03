/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/jsx-props-no-spreading */
import React, { useCallback, useEffect, useState } from 'react';
import { generateClient } from 'aws-amplify/api';
import { Alert, Box, Button, Modal, SpaceBetween, StatusIndicator } from '@cloudscape-design/components';

const client = generateClient();

const Q_GET_PROFILE_STATUS = /* GraphQL */ `
  query GetMyChromeProfileStatus {
    getMyChromeProfileStatus {
      present
      sizeBytes
      lastModified
    }
  }
`;

const M_DELETE_PROFILE = /* GraphQL */ `
  mutation DeleteMyChromeProfile {
    deleteMyChromeProfile
  }
`;

const formatSize = (bytes) => {
  if (!bytes && bytes !== 0) return null;
  const mb = bytes / (1024 * 1024);
  if (mb >= 1) return `${mb.toFixed(1)} MB`;
  const kb = bytes / 1024;
  return `${kb.toFixed(0)} KB`;
};

/**
 * Stored Chrome profile status + remove control. Platform-agnostic: the
 * persisted Chromium profile is ONE per user across every platform (cookies,
 * "remember this device" markers, and — for Teams anonymous joins — the HIP
 * CAPTCHA trust token all live in it), so this is shown for Zoom, Teams,
 * Webex, and Chime alike. Zoom *credentials* remain Zoom-only (see
 * ZoomCredentialsManager); this component is only the profile.
 */
const ChromeProfileManager = () => {
  const [profile, setProfile] = useState({ present: false, sizeBytes: null, lastModified: null });
  const [loading, setLoading] = useState(true);
  const [confirmVisible, setConfirmVisible] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [info, setInfo] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await client.graphql({ query: Q_GET_PROFILE_STATUS });
      const p = resp?.data?.getMyChromeProfileStatus || { present: false, sizeBytes: null, lastModified: null };
      setProfile(p);
    } catch (e) {
      setError(e?.errors?.[0]?.message || e?.message || 'Failed to load stored profile status');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const removeProfile = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await client.graphql({ query: M_DELETE_PROFILE });
      setProfile({ present: false, sizeBytes: null, lastModified: null });
      setConfirmVisible(false);
      setInfo(
        'Stored Chrome profile removed. The next meeting will start from a fresh browser session — ' +
          'Teams may show a CAPTCHA you need to solve via the LMA viewer, and Zoom (if credentials are ' +
          'saved) will sign in fresh.',
      );
    } catch (e) {
      setError(e?.errors?.[0]?.message || e?.message || 'Failed to remove profile');
    } finally {
      setSubmitting(false);
    }
  };

  let profileLine;
  if (loading) {
    profileLine = <StatusIndicator type="loading">Loading stored profile status...</StatusIndicator>;
  } else if (profile.present) {
    const size = formatSize(profile.sizeBytes);
    const detail = [size, profile.lastModified ? `updated ${new Date(profile.lastModified).toLocaleString()}` : null]
      .filter(Boolean)
      .join(', ');
    profileLine = (
      <SpaceBetween direction="horizontal" size="s">
        <StatusIndicator type="info">Stored Chrome profile {detail ? `(${detail})` : ''}</StatusIndicator>
        <Button onClick={() => setConfirmVisible(true)} disabled={submitting}>
          Remove Profile
        </Button>
      </SpaceBetween>
    );
  } else {
    profileLine = (
      <SpaceBetween direction="vertical" size="xxs">
        <StatusIndicator type="pending">No stored Chrome profile</StatusIndicator>
        <Box variant="small" color="text-body-secondary">
          The next meeting will start from a fresh browser session.
        </Box>
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
        {profileLine}
      </SpaceBetween>

      {/* Remove profile confirmation */}
      <Modal
        visible={confirmVisible}
        onDismiss={() => setConfirmVisible(false)}
        header="Remove stored Chrome profile?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setConfirmVisible(false)} disabled={submitting}>
                Cancel
              </Button>
              <Button variant="primary" onClick={removeProfile} loading={submitting}>
                Remove Profile
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween direction="vertical" size="s">
          <div>
            This deletes the persisted Chromium profile shared across all meeting platforms (cookies, &quot;remember
            this device&quot; markers, saved-session state). It does <strong>not</strong> remove any saved Zoom
            credentials.
          </div>
          <div>
            The next meeting will start fresh from cloud. For Teams that means a CAPTCHA may reappear (solve it once via
            the LMA viewer and it&apos;s re-saved); for Zoom, sign-in will run again from your saved credentials.
          </div>
        </SpaceBetween>
      </Modal>
    </Box>
  );
};

export default ChromeProfileManager;
