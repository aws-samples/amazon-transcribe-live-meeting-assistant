/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import { Box, Button, Modal, SpaceBetween } from '@awsui/components-react';
import MCPServersContent from './MCPServersContent';

/**
 * MCP Servers Modal - Manage Model Context Protocol servers
 * Shows Lambda MCP servers (always available) and VP MCP (active meetings only)
 */
const MCPServersModal = ({ visible, onDismiss, vpData }) => {
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      size="large"
      header="MCP Servers"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onDismiss}>Close</Button>
          </SpaceBetween>
        </Box>
      }
    >
      <MCPServersContent vpData={vpData} />
    </Modal>
  );
};

MCPServersModal.propTypes = {
  visible: PropTypes.bool.isRequired,
  onDismiss: PropTypes.func.isRequired,
  vpData: PropTypes.shape({
    CallId: PropTypes.string,
    mcpReady: PropTypes.bool,
    vncReady: PropTypes.bool,
    status: PropTypes.string,
  }),
};

MCPServersModal.defaultProps = {
  vpData: null,
};

export default MCPServersModal;
