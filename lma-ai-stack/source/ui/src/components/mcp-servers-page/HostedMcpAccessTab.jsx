/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Container,
  CopyToClipboard,
  ExpandableSection,
  Header,
  Link,
  SpaceBetween,
} from '@cloudscape-design/components';
import MCPApiKeySection from './MCPApiKeySection';
import useMcpConfig from '../../hooks/use-mcp-config';

/**
 * Renders a small labeled read-only value with an inline copy-to-clipboard.
 * Used to surface CloudFormation-output values that a user needs to paste
 * into an external MCP client config.
 */
const ReadOnlyField = ({ label, value, description }) => {
  if (!value) return null;
  return (
    <Box>
      <Box variant="awsui-key-label">{label}</Box>
      {description ? (
        <Box color="text-body-secondary" fontSize="body-s" margin={{ bottom: 'xxs' }}>
          {description}
        </Box>
      ) : null}
      <CopyToClipboard
        copyButtonAriaLabel={`Copy ${label}`}
        copySuccessText={`${label} copied`}
        textToCopy={value}
        variant="inline"
      />
    </Box>
  );
};

ReadOnlyField.propTypes = {
  label: PropTypes.string.isRequired,
  value: PropTypes.string,
  description: PropTypes.string,
};

ReadOnlyField.defaultProps = {
  value: '',
  description: '',
};

/**
 * "Hosted MCP Access" tab — shows everything an external MCP client
 * (Claude Desktop, Amazon Quick Suite, custom agent) needs to connect
 * to LMA's hosted MCP server. Replaces the need to read the CloudFormation
 * stack outputs directly.
 */
const HostedMcpAccessTab = () => {
  const mcp = useMcpConfig();

  if (!mcp.enabled) {
    return (
      <Alert type="info" header="Hosted MCP Server is disabled">
        The hosted MCP Server integration was not enabled for this deployment. Re-deploy the stack with
        <Box variant="code" display="inline" margin={{ horizontal: 'xxs' }}>
          EnableMCP=true
        </Box>
        to expose MCP connection endpoints here.
      </Alert>
    );
  }

  return (
    <SpaceBetween size="l">
      <Alert type="info">
        Use these endpoints to connect external MCP clients (Amazon Quick Suite, Claude Desktop, custom agents) to
        LMA&apos;s hosted MCP server. See{' '}
        <Link
          external
          href="https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/main/docs/mcp-api-key-auth.md"
        >
          MCP API Key setup
        </Link>{' '}
        and{' '}
        <Link
          external
          href="https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant/blob/main/docs/quicksuite-mcp-setup.md"
        >
          Amazon Quick Suite setup
        </Link>{' '}
        for walkthroughs.
      </Alert>

      {/* Section 1 — API Key authentication (simplest path for most users) */}
      <Container
        header={
          <Header
            variant="h2"
            description={
              'Generate a personal API key and pass it via the x-api-key header. ' +
              'Simplest option for Claude Desktop, Quick Suite, and scripts.'
            }
          >
            API Key Authentication
          </Header>
        }
      >
        <SpaceBetween size="l">
          <ReadOnlyField
            label="MCP Server URL (API key auth)"
            description="Set this as the MCP server URL in your client; pass your API key in the x-api-key header."
            value={mcp.apiKeyEndpoint}
          />
          <MCPApiKeySection />
        </SpaceBetween>
      </Container>

      {/* Section 2 — OAuth (external app) flow. More advanced. */}
      <Container
        header={
          <Header
            variant="h2"
            description={
              'OAuth 2.0 Authorization Code flow for registered external applications. ' +
              'Use when your MCP client supports OAuth and you want per-user authentication.'
            }
          >
            OAuth (External Application)
          </Header>
        }
      >
        <SpaceBetween size="l">
          <ReadOnlyField
            label="MCP Server URL (OAuth)"
            description="Base MCP gateway URL for OAuth-authenticated clients."
            value={mcp.mcpServerEndpoint}
          />
          <ReadOnlyField
            label="Authorization URL"
            description="OAuth authorize endpoint. Browser-based clients redirect users here to log in."
            value={mcp.authorizationUrl}
          />
          <ReadOnlyField
            label="Token URL"
            description="OAuth token endpoint. Exchange authorization codes for access tokens here."
            value={mcp.tokenUrl}
          />
          <ReadOnlyField
            label="Redirect URI (OAuth callback)"
            description="Register this URL as an allowed redirect URI in your OAuth client configuration."
            value={mcp.oauthCallbackUrl}
          />
          <ReadOnlyField
            label="Client ID"
            description={
              'Cognito app client ID for external applications. ' +
              'Pair with the Client Secret (below) to obtain OAuth tokens.'
            }
            value={mcp.clientId}
          />

          <Alert type="warning" header="Client Secret">
            The <b>Client Secret</b> is intentionally not displayed in the browser. Retrieve it from CloudFormation
            stack outputs:
            <Box variant="code" margin={{ top: 'xs' }}>
              aws cloudformation describe-stacks --stack-name &lt;your-lma-stack&gt; --query
              &quot;Stacks[0].Outputs[?OutputKey==&apos;MCPServerClientSecret&apos;].OutputValue&quot; --output text
            </Box>
          </Alert>

          <ExpandableSection headerText="Advanced">
            <SpaceBetween size="m">
              <ReadOnlyField
                label="Cognito User Pool ID"
                description="Rarely needed directly — useful if a client asks for the issuer / JWKS endpoint."
                value={mcp.userPoolId}
              />
            </SpaceBetween>
          </ExpandableSection>
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
};

export default HostedMcpAccessTab;
