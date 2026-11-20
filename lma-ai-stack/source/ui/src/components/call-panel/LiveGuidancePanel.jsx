/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Live guidance for one meeting: the switch that turns it on or off, and the
 * Digest so far button. The guidance itself arrives in the meeting's
 * Notifications drawer, for the meeting's owner and the users it is shared
 * with; it is never posted to the meeting chat or spoken. See
 * docs/live-guidance.md.
 */
import React, { useState } from 'react';
import { Alert, Box, Button, Container, Header, SpaceBetween, Toggle } from '@cloudscape-design/components';

/* eslint-disable react/prop-types */

export const DIGEST_FAILED_MESSAGE =
  'The digest took too long or failed. It may still appear in the Notifications drawer; try again in a minute.';
const DESCRIPTION =
  'Offers guidance in the Notifications drawer while the meeting is in progress. ' +
  'Each run is a model call billed to this meeting.';
export const DIGEST_PUBLISHED_MESSAGE = 'The digest was published to the Notifications drawer.';

const LiveGuidancePanel = ({ item, readOnly = false, isLive = false, onToggle, onRequestDigest }) => {
  const [saving, setSaving] = useState(false);
  const [digesting, setDigesting] = useState(false);
  const [notice, setNotice] = useState(null);

  // Left out for a viewer who can change nothing here, unless guidance is on.
  if ((readOnly || (!onToggle && !onRequestDigest)) && item.guidanceEnabled !== true) return null;

  const enabled = item.guidanceEnabled === true;
  const toggleDisabled = readOnly || !onToggle || !isLive || saving;
  let toggleReason;
  if (readOnly) toggleReason = 'You may not change this meeting.';
  else if (!isLive) toggleReason = 'Live guidance runs only while the meeting is in progress.';

  const toggle = async (checked) => {
    setSaving(true);
    setNotice(null);
    try {
      const result = await onToggle(checked);
      if (!result?.ok) {
        setNotice({ type: 'error', text: result?.message || 'Live guidance could not be changed.' });
      }
    } finally {
      setSaving(false);
    }
  };

  const digest = async () => {
    setDigesting(true);
    setNotice(null);
    try {
      const result = await onRequestDigest();
      if (result?.Success) setNotice({ type: 'success', text: DIGEST_PUBLISHED_MESSAGE });
      else if (result?.Failed || !result?.Message) setNotice({ type: 'warning', text: DIGEST_FAILED_MESSAGE });
      else setNotice({ type: 'error', text: result.Message });
    } finally {
      setDigesting(false);
    }
  };

  return (
    <Container
      data-testid="live-guidance-panel"
      header={
        <Header
          variant="h2"
          actions={
            onRequestDigest ? (
              <Button onClick={digest} loading={digesting} disabled={readOnly}>
                Digest so far
              </Button>
            ) : undefined
          }
          description={DESCRIPTION}
        >
          Live guidance
        </Header>
      }
    >
      <SpaceBetween size="s">
        <Toggle checked={enabled} disabled={toggleDisabled} onChange={({ detail }) => toggle(detail.checked)}>
          {enabled ? 'On for this meeting' : 'Off for this meeting'}
        </Toggle>
        {toggleReason && (
          <Box variant="small" color="text-body-secondary">
            {toggleReason}
          </Box>
        )}
        {notice && (
          <Alert type={notice.type} dismissible onDismiss={() => setNotice(null)}>
            {notice.text}
          </Alert>
        )}
      </SpaceBetween>
    </Container>
  );
};

export default LiveGuidancePanel;
