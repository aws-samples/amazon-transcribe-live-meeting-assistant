/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Exposes MCP Server connection info (sourced from CloudFormation outputs
 * and injected into the Vite build via VITE_* environment variables).
 *
 * These values let the UI surface everything a user needs to connect an
 * external MCP client (Claude Desktop, Amazon Quick Suite, custom agents)
 * to LMA's hosted MCP server — without having to dig through CloudFormation
 * stack outputs.
 *
 * SECURITY NOTE: MCPServerClientSecret is intentionally NOT surfaced here.
 * It is a static secret that would ship to every browser. Administrators
 * should retrieve it from the CloudFormation stack outputs directly, e.g.:
 *   aws cloudformation describe-stacks --stack-name <stack> \
 *     --query "Stacks[0].Outputs[?OutputKey=='MCPServerClientSecret'].OutputValue"
 */
const useMcpConfig = () => {
  const env = import.meta.env || {};

  const enabled = String(env.VITE_ENABLE_MCP_SERVER || '').toLowerCase() === 'true';

  return {
    enabled,
    // REST API Gateway endpoint for API-key-authenticated MCP access.
    apiKeyEndpoint: env.VITE_MCP_SERVER_API_KEY_ENDPOINT || '',
    // Main MCP gateway endpoint (used by OAuth clients).
    mcpServerEndpoint: env.VITE_MCP_SERVER_ENDPOINT || '',
    // OAuth 2.0 (Cognito hosted UI) authorize/token endpoints.
    authorizationUrl: env.VITE_MCP_SERVER_AUTH_URL || '',
    tokenUrl: env.VITE_MCP_SERVER_TOKEN_URL || '',
    // Cognito app client ID for external applications (public).
    clientId: env.VITE_MCP_SERVER_CLIENT_ID || '',
    // Cognito User Pool ID (public, used to build JWKS URL etc).
    userPoolId: env.VITE_MCP_SERVER_USER_POOL_ID || '',
    // Redirect URI users should register in their OAuth provider / client.
    oauthCallbackUrl: env.VITE_OAUTH_CALLBACK_URL || '',
  };
};

export default useMcpConfig;
