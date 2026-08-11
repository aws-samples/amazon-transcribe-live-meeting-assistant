// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Builds the noVNC connection parameters for a Virtual Participant.
 *
 * There are two transports, because the VP can run on two different compute
 * platforms (see VPLaunchType in the VP stack):
 *
 *   ECS (EC2/FARGATE) — the VP registers itself with an ALB behind CloudFront,
 *     and the browser connects to `wss://<cloudfront>/vnc/<vpId>` with the
 *     Cognito ID token as a `token` query parameter.
 *
 *   Lambda MicroVMs — each MicroVM has its own endpoint
 *     (`wss://<id>.lambda-microvm.<region>.on.aws`) and there is no path-based
 *     routing. Auth is a short-lived, port-scoped JWE that Lambda requires in
 *     the `X-aws-proxy-auth` header — but browsers cannot set headers on a
 *     WebSocket, so it is passed as a WebSocket subprotocol instead.
 *
 * Kept as a pure function so the transport choice is unit-testable without a
 * browser, a live VP, or AWS.
 */

/** Hostname marker for a Lambda MicroVMs endpoint. */
const MICROVM_HOST_MARKER = '.lambda-microvm.';

/** noVNC serves the VP's websockify on this port inside the container. */
export const MICROVM_VNC_PORT = 5901;

/**
 * True when the endpoint is a Lambda MicroVM endpoint rather than the
 * CloudFront/ALB path used by the ECS launch types.
 */
export const isMicrovmEndpoint = (endpoint) => typeof endpoint === 'string' && endpoint.includes(MICROVM_HOST_MARKER);

/**
 * Build the `{ url, wsProtocols }` to hand to noVNC's RFB constructor.
 *
 * noVNC 1.7 passes `options.wsProtocols` straight through to
 * `new WebSocket(url, protocols)`, which is what makes the MicroVM subprotocol
 * scheme usable from the browser.
 *
 * @param {object} args
 * @param {string} args.endpoint   Value published by the backend (vncEndpoint).
 * @param {string} [args.idToken]  Cognito ID token (ECS transport).
 * @param {string} [args.authToken] MicroVM JWE auth token (MicroVM transport).
 * @returns {{url: string, wsProtocols: string[]}}
 */
export const buildVncConnection = ({ endpoint, idToken, authToken }) => {
  if (!endpoint) {
    throw new Error('No VNC endpoint available');
  }

  if (isMicrovmEndpoint(endpoint)) {
    if (!authToken) {
      // Fail loudly: connecting without the token yields an opaque 403 from the
      // MicroVM endpoint, which is much harder to diagnose than this message.
      throw new Error('No MicroVM auth token available');
    }
    return {
      // No query parameter: the MicroVM endpoint rejects unknown ones, and the
      // token must not end up in a URL (they are logged by proxies far more
      // readily than subprotocols).
      url: endpoint,
      wsProtocols: [
        // Required base protocol, then the token, then the target port. Order
        // is not significant to Lambda, but all three are required.
        'lambda-microvms',
        `lambda-microvms.authentication.${authToken}`,
        `lambda-microvms.port.${MICROVM_VNC_PORT}`,
      ],
    };
  }

  if (!idToken) {
    throw new Error('No Cognito ID token available');
  }
  const url = new URL(endpoint);
  url.searchParams.append('token', idToken);
  return { url: url.toString(), wsProtocols: [] };
};

/**
 * GraphQL mutation that mints a MicroVM auth token for a VP's noVNC port.
 *
 * Minted on demand per viewer session rather than once at VP launch: the token
 * TTL is capped at 60 minutes by the service, while meetings can run up to 8
 * hours. The viewer's existing auto-reconnect re-mints on reconnect.
 */
export const createMicrovmVncTokenMutation = `
  mutation CreateMicrovmVncToken($vpId: ID!) {
    createMicrovmVncToken(vpId: $vpId) {
      authToken
      expiresAt
    }
  }
`;

/**
 * Fetch a fresh MicroVM auth token for this VP.
 *
 * Minting requires IAM credentials (CreateMicrovmAuthToken), so it happens in a
 * resolver rather than the browser.
 *
 * @param {object} client  Amplify GraphQL client.
 * @param {string} vpId
 * @returns {Promise<string>} the JWE auth token
 */
export const fetchMicrovmAuthToken = async (client, vpId) => {
  const response = await client.graphql({
    query: createMicrovmVncTokenMutation,
    variables: { vpId },
  });
  const token = response?.data?.createMicrovmVncToken?.authToken;
  if (!token) {
    throw new Error('Could not mint a MicroVM auth token for the VNC viewer');
  }
  return token;
};

export default buildVncConnection;
