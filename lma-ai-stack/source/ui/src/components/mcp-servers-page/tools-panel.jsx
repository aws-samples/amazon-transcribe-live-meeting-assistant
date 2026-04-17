/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import { HelpPanel, Icon } from '@awsui/components-react';

const DOCS_BASE = 'https://aws-samples.github.io/amazon-transcribe-live-meeting-assistant';

const header = <h2>MCP Servers</h2>;
const content = (
  <>
    <p>
      Manage Model Context Protocol (MCP) server integrations for the Meeting Assistant. MCP servers extend the
      assistant&apos;s capabilities by connecting to external tools and data sources such as Salesforce, Amazon Q
      Business, and custom APIs.
    </p>
    <h3>Features</h3>
    <ul>
      <li>View and configure built-in MCP server integrations</li>
      <li>Add custom MCP servers with SSE or Streamable HTTP transport</li>
      <li>Configure authentication (API key, OAuth, custom headers) per server</li>
      <li>Enable or disable individual MCP server tools</li>
      <li>Generate and manage API keys for external MCP access</li>
      <li>Browse the public MCP server registry to discover new integrations</li>
    </ul>
    <h3>Documentation</h3>
    <ul>
      <li>
        <a href={`${DOCS_BASE}/mcp-servers/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> MCP Servers Overview
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/mcp-api-key-auth/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> MCP API Key Authentication
        </a>
      </li>
      <li>
        <a href={`${DOCS_BASE}/meeting-assistant/`} target="_blank" rel="noopener noreferrer">
          <Icon name="external" /> Meeting Assistant
        </a>
      </li>
    </ul>
  </>
);

const ToolsPanel = () => <HelpPanel header={header}>{content}</HelpPanel>;

export default ToolsPanel;
