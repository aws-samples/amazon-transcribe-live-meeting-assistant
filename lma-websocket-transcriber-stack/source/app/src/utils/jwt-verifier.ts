/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { FastifyRequest, FastifyReply } from 'fastify';
import { CognitoJwtVerifier } from 'aws-jwt-verify';
import dotenv from 'dotenv';
import { normalizeErrorForLogging } from './common';
import { getClientIP } from './headers';
import { describeRequest, redactTokenLike } from './log-redaction';
import { claimedGroups, effectiveRole, mayUseTranscriber } from './roles';

// dotenv v17 prints an "injected env" banner to stdout by default; quiet
// suppresses it to keep production logs clean.
dotenv.config({ quiet: true });

const USERPOOL_ID = process.env['USERPOOL_ID'] || '';
const cognitoJwtVerifier = CognitoJwtVerifier.create({
    userPoolId: USERPOOL_ID,
});

type queryobj = {
    authorization: string
};

type headersobj = {
    authorization: string
};

/**
 * Identity of the authenticated caller, taken from the VERIFIED access token
 * and stashed on the request so the websocket handlers can authorize per-call
 * actions (see requireCallOwner in index.ts).
 *
 * `sub` is the Cognito user id — stable, and not something the client can
 * choose. Anything the client sends in a message body (including callId) is
 * untrusted; only this is trustworthy.
 */
export type AuthenticatedCaller = {
    sub: string;
    username?: string;
    /** Cognito groups from the verified token; 'Admin' gates admin-only routes. */
    groups: string[];
};

/** Retrieve the identity established by jwtVerifier for this connection. */
export const getAuthenticatedCaller = (
    request: FastifyRequest
): AuthenticatedCaller | undefined =>
    (request as FastifyRequest & { lmaCaller?: AuthenticatedCaller }).lmaCaller;

export const jwtVerifier = async (request: FastifyRequest, reply: FastifyReply) => {
    const query = request.query as queryobj;
    const headers = request.headers as headersobj;
    const auth = query.authorization || headers.authorization;
    const clientIP = getClientIP(headers);
    // The bearer token may arrive in the query string as well as in a header, so
    // every log line below describes the request through describeRequest(): the
    // path with the query string dropped, plus an allowlist of header names.
    const requestDescription = describeRequest(request);

    if (!auth) {
        request.log.error(`[AUTH]: [${clientIP}] - No authorization query string or header found. ${requestDescription}`);

        return reply.status(401).send();
    }

    const match = auth?.match(/^Bearer (.+)$/);
    if (!match) {
        request.log.error(`[AUTH]: [${clientIP}] - No Bearer token found in header or query string. ${requestDescription}`);

        return reply.status(401).send();
    }

    const accessToken = match[1];
    try {
        const payload = await cognitoJwtVerifier.verify(accessToken, { clientId: null, tokenUse: 'access' });      
        if (!payload) {
            request.log.error(`[AUTH]: [${clientIP}] - Connection not authorized. Returning 401. ${requestDescription}`);

            return reply.status(401).send();
        }
        const groups = claimedGroups(payload as unknown as Record<string, unknown>);
        // Only the roles the role matrix lists for meeting capture may stream.
        // 403 rather than 401: the token is valid, its role is not admitted.
        if (!mayUseTranscriber(groups)) {
            request.log.warn(
                `[AUTH]: [${clientIP}] - Role ${effectiveRole(groups)} may not use the transcriber. Returning 403. ${requestDescription}`
            );

            return reply.status(403).send();
        }
        // Stash the verified identity for downstream per-call authorization.
        // Without this the handlers only know "some valid pool user", which is
        // not enough to stop one user acting on another user's call.
        (request as FastifyRequest & { lmaCaller?: AuthenticatedCaller }).lmaCaller = {
            sub: String(payload.sub),
            username: typeof payload['username'] === 'string' ? payload['username'] : undefined,
            groups,
        };
        request.log.info(`[AUTH]: [${clientIP}] - Connection request authorized. ${requestDescription}`);

        return;
    } catch (err) {
        request.log.error(`[AUTH]: [${clientIP}] - Error Authorizing client connection. ${redactTokenLike(normalizeErrorForLogging(err))} ${requestDescription}`);

        return reply.status(401).send();
    }
};