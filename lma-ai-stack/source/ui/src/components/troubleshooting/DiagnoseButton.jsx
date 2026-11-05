/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Diagnose button of a meeting or a Virtual Participant, and the window it
 * opens.
 *
 * Opening the window the first time starts a diagnosis of that resource by the
 * troubleshooting agent, with the resolver's fixed question for the resource
 * (see troubleshooting_resolver). Closing the window does not stop the
 * diagnosis, and opening it again shows the same one, running or finished:
 * each run is paid for, so a new one starts only on Run again.
 *
 * Rendered for members of the Admin group only, the group the
 * startTroubleshooting field and its resolver accept a diagnosis from; for
 * anyone else, and until the session's groups are known, it renders nothing.
 */
import React, { useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Header, Modal, SpaceBetween } from '@cloudscape-design/components';

import useTroubleshootingJob, { isTerminal } from '../../hooks/use-troubleshooting-job';
import useUserGroups from '../../hooks/use-user-groups';
import TroubleshootingAnswer from './TroubleshootingAnswer';

const DESCRIPTION =
  "The troubleshooting agent reads this deployment's logs and records about it. It reads only; it changes nothing.";

const DiagnoseButton = ({ callId = '', vpId = '', resourceLabel = '', pollIntervalMs = undefined }) => {
  const { isAdmin, isLoading } = useUserGroups();
  const [visible, setVisible] = useState(false);
  const { job, error, starting, running, start } = useTroubleshootingJob(
    pollIntervalMs ? { pollIntervalMs } : undefined,
  );
  const [started, setStarted] = useState(false);

  const run = () => {
    setStarted(true);
    start({ callId, vpId });
  };

  useEffect(() => {
    if (visible && !started) run();
  }, [visible]);

  const finished = isTerminal(job) || (Boolean(error) && !running);
  const subject = resourceLabel || (vpId ? 'this Virtual Participant' : 'this meeting');

  if (isLoading || !isAdmin) return null;

  return (
    <>
      <Button iconName="search" onClick={() => setVisible(true)} disabled={!callId && !vpId}>
        Diagnose
      </Button>
      <Modal
        visible={visible}
        size="large"
        // Only the close button and Close dismiss, so a stray click outside the
        // window does not hide a diagnosis that is still running.
        onDismiss={({ detail }) => {
          if (detail.reason === 'closeButton') setVisible(false);
        }}
        header={
          <Header variant="h2" description={DESCRIPTION}>
            {`Diagnose ${subject}`}
          </Header>
        }
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              {finished && (
                <Button iconName="refresh" onClick={run}>
                  Run again
                </Button>
              )}
              <Button variant="primary" onClick={() => setVisible(false)}>
                Close
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {visible && <TroubleshootingAnswer job={job} error={error} starting={starting} />}
      </Modal>
    </>
  );
};

DiagnoseButton.propTypes = {
  callId: PropTypes.string,
  vpId: PropTypes.string,
  /** How the window's title names the resource, e.g. the meeting's title. */
  resourceLabel: PropTypes.string,
  pollIntervalMs: PropTypes.number,
};

export default DiagnoseButton;
