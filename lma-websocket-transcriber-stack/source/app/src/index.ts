/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import fastify from 'fastify';
import websocket from '@fastify/websocket';
import { FastifyRequest } from 'fastify';

import WebSocket from 'ws'; // type structure for the websocket object used by fastify/websocket
// import stream from 'stream';
import os from 'os';
import path from 'path';
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';
import BlockStream from 'block-stream2';

import fs from 'fs';
import { randomUUID } from 'crypto';

import {
    startTranscribe,
    writeCallStartEvent,
    writeCallEndEvent,
    CallMetaData,
    SocketCallData,
    writeCallRecordingEvent,
    resolveAsrEngine,
    startMicrovmAsr,
    pushAsrAudio,
    stopMicrovmAsr,
    shouldFallbackToTranscribe,
    getAsrRuntimeConfig,
} from './calleventdata';

import {
    createWavHeader,
    posixifyFilename,
    normalizeErrorForLogging,
    getClientIP,
    resolveShouldRecordCall,
    describeRequest,
    stringifyCallMetaData,
    redactTokenLike,
} from './utils';
import { buildLoggerOptions } from './utils/logger-options';
import { s3RecordingStore, uploadRecording } from './utils/recording-upload';
import { DetachedSessions } from './calleventdata/detached-sessions';
import { bindLogContext, runWithLogContext, LogContext } from './utils/log-context';

import { jwtVerifier, getAuthenticatedCaller } from './utils/jwt-verifier';

import { noteStreamingSessionStarted, noteStreamingSessionEnded } from './metrics/session';
import { getSessionSettings } from './calleventdata/session-settings';

import {
    VideoSession,
    startVideoSession,
    endVideoSession,
    videoSocketDropped,
    writeVideoChunk,
    notifyAudioRecordingDone,
    awaitPendingMuxes,
    getVideoSession,
} from './videorecording';

import { ConsentApi, createSignedGraphqlCaller } from './consent/appsync';
import { MEETING_NOT_OWNED_MESSAGE, decideAtStart, parseClientConsent, settingsFrame } from './consent/rules';
import { ConsentSession } from './consent/session';
import { PendingConsentFrames } from './consent/pending-frames';
import { readConsentSettings } from './consent/settings';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const RECORDINGS_BUCKET_NAME =
    process.env['RECORDINGS_BUCKET_NAME'] || undefined;
// RECORDINGS_KEY_PREFIX is what the CFN task definition sets (AudioFilePrefix
// parameter); RECORDING_FILE_PREFIX kept for backward compatibility with any
// standalone deployments that set the old (previously ignored) name.
const RECORDING_FILE_PREFIX =
    process.env['RECORDINGS_KEY_PREFIX'] ||
    process.env['RECORDING_FILE_PREFIX'] ||
    'lma-audio-recordings/';
const CPU_HEALTH_THRESHOLD = parseInt(
    process.env['CPU_HEALTH_THRESHOLD'] || '50',
    10
);
const LOCAL_TEMP_DIR = process.env['LOCAL_TEMP_DIR'] || '/tmp/';
const WS_LOG_LEVEL = (process.env['WS_LOG_LEVEL'] || 'debug').toLowerCase();
const LOG_FORMAT = process.env['LOG_FORMAT'] || 'json';
const WS_LOG_INTERVAL = parseInt(process.env['WS_LOG_INTERVAL'] || '120', 10);
// How long a meeting whose audio connection closed without an END waits for its
// client to reconnect and continue it (see calleventdata/detached-sessions.ts).
// 0 ends the meeting as soon as the connection closes.
const AUDIO_RECONNECT_GRACE_MS = parseInt(process.env['AUDIO_RECONNECT_GRACE_MS'] || '60000', 10);
// Whether a meeting is recorded when its client says nothing is the Meeting
// Recording section's setting, read per meeting with this task's
// SHOULD_RECORD_CALL as the fallback: see calleventdata/session-settings.ts.

const s3Client = new S3Client({ region: AWS_REGION });

// Meeting consent (docs/meeting-consent.md). A client's consent step is recorded
// on the meeting through the AppSync API with this task's role; without the
// endpoint no consent can be recorded, so an opt-in meeting is not captured.
const GRAPHQL_ENDPOINT = process.env['GRAPHQL_ENDPOINT'] || '';
const CONSENT_EVIDENCE_PREFIX = process.env['CONSENT_EVIDENCE_PREFIX'] || 'lma-consent-evidence/';
const consentApi = GRAPHQL_ENDPOINT
    ? new ConsentApi(createSignedGraphqlCaller(GRAPHQL_ENDPOINT, AWS_REGION))
    : undefined;

/** A JSON frame to the client. Clients that predate these frames ignore them. */
const sendFrame = (ws: WebSocket, frame: Record<string, unknown>): void => {
    if (ws.readyState !== WebSocket.OPEN) {
        return;
    }
    try {
        ws.send(JSON.stringify(frame));
    } catch (error) {
        server.log.error(`[CONSENT]: could not send a frame to the client: ${normalizeErrorForLogging(error)}`);
    }
};

/** Consent frames that arrived while their connection's START was still being handled. */
const pendingConsentFrames = new PendingConsentFrames<WebSocket>();

const putConsentEvidence = async (key: string, bytes: Buffer, contentType: string): Promise<void> => {
    await s3Client.send(
        new PutObjectCommand({ Bucket: RECORDINGS_BUCKET_NAME, Key: key, Body: bytes, ContentType: contentType })
    );
};

const socketMap = new Map<WebSocket, SocketCallData>();
// The connection a meeting's session is currently streaming on. Changes when a
// client reconnects and resumes the session, so whatever the session sends the
// client (consent frames) goes to the connection that is open now.
const sessionConnection = new WeakMap<SocketCallData, { ws: WebSocket }>();
// Sessions whose connection closed without an END, waiting for their client.
const detachedSessions = new DetachedSessions<SocketCallData>(AUDIO_RECONNECT_GRACE_MS, (socketData) => {
    runWithLogContext({ sessionId: randomUUID(), callId: socketData.callMetadata.callId }, () => {
        server.log.info(
            `[ON WSCLOSE]: [${socketData.callMetadata.callId}] - The client did not reconnect within ${AUDIO_RECONNECT_GRACE_MS} ms; ending the meeting.`
        );
        endCall(undefined, socketData).catch((error) => {
            server.log.error(
                `[ON WSCLOSE]: [${socketData.callMetadata.callId}] - Error ending the meeting: ${normalizeErrorForLogging(error)}`
            );
        });
    });
});
const recordingStore = s3RecordingStore(s3Client, RECORDINGS_BUCKET_NAME, {
    info: (message) => server.log.info(message),
    warn: (message) => server.log.warn(message),
    error: (message) => server.log.error(message),
});
// Sockets dedicated to a video stream (announced by START_VIDEO). Binary
// frames on these sockets are fragmented-MP4 chunks, not audio PCM.
const videoSocketMap = new Map<WebSocket, VideoSession>();

/**
 * The live audio session for a callId, if any. Used to authorize START_VIDEO:
 * a video stream may only join a call that is currently streaming audio, and
 * only for the user who started it.
 *
 * Linear scan over socketMap — one entry per concurrent call on this task, so
 * a handful to low hundreds, and this runs once per video stream (not per
 * frame).
 */
const findAudioSessionByCallId = (
    callId: string
): SocketCallData | undefined => {
    for (const data of socketMap.values()) {
        if (!data.ended && data.callMetadata?.callId === callId) {
            return data;
        }
    }
    // A meeting waiting for its audio connection to come back is still live:
    // its video stream may reconnect first.
    return detachedSessions.find(callId);
};

// create fastify server (with logging enabled for non-PROD environments)
const server = fastify({
    logger: buildLoggerOptions({ level: WS_LOG_LEVEL, format: LOG_FORMAT }),
    disableRequestLogging: true,
});
// register the @fastify/websocket plugin with the fastify server
server.register(websocket);

// Authenticate at onRequest, which runs BEFORE body parsing: authentication needs
// only the headers, and rejecting early means an unauthenticated request never
// gets a body buffered for it on a task that is also transcribing live meetings.
server.addHook('onRequest', async (request, reply) => {
    // A CORS preflight carries no credentials by design, so authenticating it
    // would 401 every cross-origin call before the real request is ever made.
    if (!request.url.includes('health') && request.method !== 'OPTIONS') {
        const clientIP = getClientIP(request.headers);
        server.log.debug(
            `[AUTH]: [${clientIP}] - Received onRequest hook for authentication. ${describeRequest(
                request
            )}`
        );

        await jwtVerifier(request, reply);
    }
});

// Setup Route for websocket connection.
//
// The `websocket: true` route MUST be registered only after the
// `@fastify/websocket` plugin has finished loading — otherwise the plugin's
// onRoute hook (which installs the HTTP-upgrade interception) isn't wired yet,
// so upgrade requests fall through to the normal HTTP handler and the handler
// is invoked with (request, reply) instead of (socket, request). That yields
// `ws.on is not a function` -> HTTP 500 on every WS connect. Under Fastify 3 /
// @fastify/websocket 5 the unawaited `server.register(websocket)` above
// happened to order correctly; under Fastify 5 / @fastify/websocket 11 it does
// not. `server.after()` guarantees the plugin is loaded before we add the route
// (no top-level await needed in this module).
server.after(() => {
    server.get(
        '/api/v1/ws',
        { websocket: true, logLevel: 'debug' },
        (socket, request) => {
            // This connection's log context: every record written while one of
            // its handlers runs carries the session id, and the call id once
            // the START frame names it.
            const logContext = { sessionId: randomUUID() };
            runWithLogContext(logContext, () => {
                const clientIP = getClientIP(request.headers);
                server.log.debug(
                    `[NEW CONNECTION]: [${clientIP}] - Received new connection request @ /api/v1/ws. ${describeRequest(
                        request
                    )}`
                );

                registerHandlers(clientIP, socket, request, logContext); // setup the handler functions for websocket events
            });
        }
    );
});

type HealthCheckRemoteInfo = {
    addr: string;
    tsFirst: number;
    tsLast: number;
    count: number;
};
const healthCheckStats = new Map<string, HealthCheckRemoteInfo>();

// Setup Route for health check
server.get('/health/check', { logLevel: 'warn' }, (request, response) => {
    const now = Date.now();
    const cpuUsage = (os.loadavg()[0] / os.cpus().length) * 100;
    const isHealthy = cpuUsage > CPU_HEALTH_THRESHOLD ? false : true;
    const status = isHealthy ? 200 : 503;

    const remoteIp = request.socket.remoteAddress || 'unknown';
    const item = healthCheckStats.get(remoteIp);
    if (!item) {
        server.log.debug(
            `[HEALTH CHECK]: [${remoteIp}] - Received First health check from load balancer. ${describeRequest(
                request
            )} ==> Health Check status - CPU Usage%: ${cpuUsage}, IsHealthy: ${isHealthy}, Status: ${status}`
        );
        healthCheckStats.set(remoteIp, {
            addr: remoteIp,
            tsFirst: now,
            tsLast: now,
            count: 1,
        });
    } else {
        item.tsLast = now;
        ++item.count;
        const elapsed_seconds = Math.round((item.tsLast - item.tsFirst) / 1000);
        if (elapsed_seconds % WS_LOG_INTERVAL == 0) {
            server.log.debug(
                `[HEALTH CHECK]: [${remoteIp}] - Received Health check # ${
                    item.count
                } from load balancer. ${describeRequest(
                    request
                )} ==> Health Check status - CPU Usage%: ${cpuUsage}, IsHealthy: ${isHealthy}, Status: ${status}`
            );
        }
    }

    response
        .code(status)
        .header(
            'Cache-Control',
            'max-age=0, no-cache, no-store, must-revalidate, proxy-revalidate'
        )
        .send({ 'Http-Status': status, Healthy: isHealthy });
});

// Setup handlers for websocket events - 'message', 'close', 'error'
const registerHandlers = (
    clientIP: string,
    ws: WebSocket,
    request: FastifyRequest,
    logContext: LogContext
): void => {
    // A listener runs in the context of whatever emitted the event, not of the
    // code that registered it, so each one enters the connection's context.
    ws.on('message', (data, isBinary) => runWithLogContext(logContext, () => onWsMessage(data, isBinary)));
    ws.on('close', (code: number) => runWithLogContext(logContext, () => onWsCloseEvent(code)));
    ws.on('error', (error: Error) => runWithLogContext(logContext, () => onWsErrorEvent(error)));

    async function onWsMessage(data: WebSocket.RawData, isBinary: boolean): Promise<void> {
        try {
            if (isBinary) {
                const audioinput = Buffer.from(data as Uint8Array);
                await onBinaryMessage(clientIP, ws, audioinput);
            } else {
                await onTextMessage(
                    clientIP,
                    ws,
                    Buffer.from(data as Uint8Array).toString('utf8'),
                    request
                );
            }
        } catch (error) {
            // Close THIS socket; do not exit. This task multiplexes many
            // concurrent calls, so terminating the process because one client
            // sent a malformed frame (or one recording hit an I/O error) would
            // drop every other meeting in progress too.
            server.log.error(
                `[ON MESSAGE]: [${clientIP}] - Error processing message; closing this connection: ${normalizeErrorForLogging(
                    error
                )}`
            );
            // A START that failed part-way registered nothing: its held consent
            // frames are not waiting for anything any more.
            pendingConsentFrames.drop(ws);
            try {
                ws.close(1011, 'internal error processing message');
            } catch (closeErr) {
                server.log.error(
                    `[ON MESSAGE]: [${clientIP}] - Error closing connection: ${normalizeErrorForLogging(closeErr)}`
                );
            }
        }
    }

    function onWsCloseEvent(code: number): void {
        server.log.debug(
            `[ON WSCLOSE]: [${clientIP}] Received Websocket close message from the client. Closing the connection.`
        );

        try {
            onWsClose(ws, code);
        } catch (err) {
            server.log.error(
                `[ON WSCLOSE]: [${clientIP}] Error in WS close handler: ${normalizeErrorForLogging(
                    err
                )}`
            );
        }
    }

    function onWsErrorEvent(error: Error): void {
        server.log.error(
            `[ON WSERROR]: [${clientIP}] - Websocket error, forcing close: ${normalizeErrorForLogging(
                error
            )}`
        );
        ws.close();
    }
};

const onBinaryMessage = async (
    clientIP: string,
    ws: WebSocket,
    data: Uint8Array
): Promise<void> => {
    // Video sockets carry fragmented-MP4 chunks, never audio PCM.
    const videoSession = videoSocketMap.get(ws);
    if (videoSession !== undefined) {
        writeVideoChunk(videoSession, data, server);
        return;
    }

    const socketData = socketMap.get(ws);

    // Audio a meeting's consent does not allow is neither transcribed nor
    // recorded: an opt-in meeting before its consent is recorded, and any
    // meeting after an objection.
    if (socketData?.consent && !socketData.consent.gateOpen) {
        return;
    }

    if (
        socketData !== undefined &&
    socketData.audioInputStream !== undefined &&
    socketData.writeRecordingStream !== undefined &&
    socketData.recordingFileSize !== undefined
    ) {
        socketData.audioInputStream.write(data);
        socketData.writeRecordingStream.write(data);
        socketData.recordingFileSize += data.length;
        pushAsrAudio(socketData, data);
    } else {
        server.log.error(
            `[ON BINARY MESSAGE]: [${clientIP}] - Error: received audio data before metadata. Check logs for errors in START event.`
        );
    }
};

/**
 * Start transcription for a call with the engine it asked for.
 *
 * The MicroVM ASR engine has to acquire a MicroVM before it can transcribe
 * anything, so a failure there (quota, region, image still building) falls back
 * to Amazon Transcribe rather than losing the meeting's transcript. Audio that
 * arrives during the acquisition is buffered by both engines.
 */
const startTranscription = async (
    socketData: SocketCallData,
    server_: typeof server
): Promise<void> => {
    // Runtime config decides the deployment default, so the engine can be switched
    // from the Transcription Engine section of the Configuration page without redeploying the task.
    const runtime = await getAsrRuntimeConfig(server_);
    if (resolveAsrEngine(socketData.callMetadata, server_, runtime) === 'microvm') {
        if (await startMicrovmAsr(socketData, server_)) {
            // startTranscribe is the ONLY consumer of audioInputStream, and it is not
            // running on this path — so every frame onBinaryMessage writes would sit
            // in the stream's buffer for the whole meeting. At the 16 kHz stereo this
            // engine negotiates that is 64 kB/s, about 230 MB an hour, in a 1 GiB
            // task shared by every other meeting on it. Draining discards each frame
            // as it arrives; writes stay unconditional so the recording is untouched,
            // and this happens only AFTER a successful start so the audio buffered
            // during MicroVM acquisition is still there for the fallback below.
            socketData.audioInputStream?.resume();
            return;
        }
        if (!shouldFallbackToTranscribe()) {
            server_.log.error(
                `[TRANSCRIBING]: [${socketData.callMetadata.callId}] - MicroVM ASR could not start and fallback is disabled; this meeting will not be transcribed.`
            );
            return;
        }
        server_.log.warn(
            `[TRANSCRIBING]: [${socketData.callMetadata.callId}] - MicroVM ASR could not start; falling back to Amazon Transcribe.`
        );
    }
    void startTranscribe(socketData, server_);
};

const onTextMessage = async (
    clientIP: string,
    ws: WebSocket,
    data: string,
    request: FastifyRequest
): Promise<void> => {
    type queryobj = {
        authorization: string;
        id_token: string;
        refresh_token: string;
    };

    type headersobj = {
        authorization: string;
        id_token: string;
        refresh_token: string;
    };

    const query = request.query as queryobj;
    const headers = request.headers as headersobj;
    const auth = query.authorization || headers.authorization;
    const idToken = query.id_token || headers.id_token;
    const refreshToken = query.refresh_token || headers.refresh_token;

    const match = auth?.match(/^Bearer (.+)$/);
    let callMetaData: CallMetaData;
    try {
        callMetaData = JSON.parse(data) as CallMetaData;
    } catch (parseErr) {
        // A truncated/garbled control frame is the client's problem, not grounds
        // for tearing down every other call on this task. The serialized error can
        // quote a fragment of the frame it failed on, so it goes through
        // redactTokenLike() for the same reason the verifier errors do.
        server.log.error(
            `[ON TEXT MESSAGE]: [${clientIP}] - Ignoring unparseable control frame: ${redactTokenLike(
                normalizeErrorForLogging(parseErr)
            )}`
        );
        return;
    }
    // Synchronously, before this handler's first await: the socket hands each
    // frame to its handler as it arrives without waiting for the previous one,
    // so consent frames sent right behind a START are held from here until the
    // START has registered its meeting on the connection.
    if (callMetaData.callEvent === 'START') {
        pendingConsentFrames.begin(ws);
    }
    if (!match) {
        server.log.error(
            `[AUTH]: [${clientIP}] - No Bearer token found in header or query string. ${describeRequest(
                request
            )}`
        );
        pendingConsentFrames.drop(ws);
        return;
    }

    const accessToken = match[1];

    // Consent frames are answered before the generic log line below: an
    // evidence frame carries an image, which does not belong in the log.
    if (await onConsentFrame(clientIP, ws, callMetaData as unknown as Record<string, unknown>)) {
        return;
    }

    // The parsed frame is logged rather than the raw `data` string: a client may
    // put token fields in the control frame as well as in the query string, and
    // stringifyCallMetaData drops those while keeping the rest of the metadata.
    server.log.debug(
        `[ON TEXT MESSAGE]: [${clientIP}][${
            callMetaData.callId
        }] - Call Metadata received from client: ${stringifyCallMetaData(callMetaData)}`
    );

    callMetaData.accessToken = accessToken;
    callMetaData.idToken = idToken;
    callMetaData.refreshToken = refreshToken;

    if (callMetaData.callEvent === 'START') {
        // generate random metadata if none is provided
        callMetaData.callId = callMetaData.callId || randomUUID();
        bindLogContext({ callId: callMetaData.callId });
        callMetaData.fromNumber = callMetaData.fromNumber || 'Customer Phone';
        callMetaData.toNumber = callMetaData.toNumber || 'System Phone';
        callMetaData.activeSpeaker =
            callMetaData.activeSpeaker ?? callMetaData?.fromNumber ?? 'unknown';

        // Once per meeting: the engine paths below and every Transcribe
        // reconnect read these from the call rather than again.
        const sessionSettings = await getSessionSettings(server.log);
        callMetaData.shouldRecordCall = resolveShouldRecordCall(
            callMetaData.shouldRecordCall,
            sessionSettings.recording.recordAudio
        );

        callMetaData.agentId = callMetaData.agentId || randomUUID();

        // Whether this meeting may be captured, from the Meeting Consent settings
        // and the client's consent step (docs/meeting-consent.md). A refused
        // meeting is not created at all.
        const clientConsent = parseClientConsent(callMetaData.consent);
        const consentDecision = decideAtStart(await readConsentSettings(server.log), clientConsent);
        if (consentDecision.kind === 'reject') {
            server.log.warn(
                `[CONSENT]: [${clientIP}][${callMetaData.callId}] - Not capturing this meeting (${consentDecision.code}): ${consentDecision.message}`
            );
            sendFrame(ws, {
                event: 'CONSENT_STATE',
                callId: callMetaData.callId,
                captureAllowed: false,
                code: consentDecision.code,
                message: consentDecision.message,
            });
            pendingConsentFrames.drop(ws);
            ws.close(1008, 'meeting consent required');
            return;
        }
        // A consent step is recorded only on the caller's own meeting: a START
        // naming an existing meeting of somebody else's is refused before
        // anything is written for it. (A meeting created between this read and
        // the session's own is caught there.)
        if (consentDecision.kind === 'consent' && consentApi) {
            let owner: string | null | undefined;
            try {
                owner = await consentApi.meetingOwner(callMetaData.callId);
            } catch (error) {
                server.log.warn(
                    `[CONSENT]: [${clientIP}][${callMetaData.callId}] - Could not read the meeting's owner at START: ${normalizeErrorForLogging(error)}`
                );
            }
            if (typeof owner === 'string' && owner !== getAuthenticatedCaller(request)?.username) {
                server.log.warn(
                    `[CONSENT]: [${clientIP}][${callMetaData.callId}] - The meeting belongs to another user; not capturing it.`
                );
                sendFrame(ws, {
                    event: 'CONSENT_STATE',
                    callId: callMetaData.callId,
                    captureAllowed: false,
                    code: 'MEETING_NOT_OWNED',
                    message: MEETING_NOT_OWNED_MESSAGE,
                });
                pendingConsentFrames.drop(ws);
                ws.close(1008, 'meeting consent required');
                return;
            }
        }
        if (consentDecision.kind === 'capture' && consentDecision.warning) {
            server.log.warn(`[CONSENT]: [${clientIP}][${callMetaData.callId}] - ${consentDecision.warning}`);
            sendFrame(ws, {
                event: 'CONSENT_STATE',
                callId: callMetaData.callId,
                captureAllowed: true,
                code: 'CONSENT_NOT_RECORDED',
                message: 'This meeting is captured without a consent record.',
            });
        }

        // The same user reconnecting to a meeting whose connection dropped on
        // this task: carry on with that session rather than starting another.
        // Its old connection may not have been seen to close yet (a dropped
        // network is noticed by the client first), so a live session of the
        // same meeting and user is taken over the same way.
        const callerSub = getAuthenticatedCaller(request)?.sub;
        const resumed =
            detachedSessions.resume(callMetaData.callId, callerSub) ?? takeOverLiveSession(callMetaData.callId, callerSub, ws);
        if (resumed) {
            await resumeSession(clientIP, ws, resumed.session, callMetaData, resumed.detachedForMs);
            return;
        }

        await writeCallStartEvent(callMetaData, server);
        // Local files named per session (see SocketCallData.recordingTempPath);
        // the S3 key is the meeting's, from its callId (see endCall).
        const sessionFileTag = randomUUID().slice(0, 8);
        const recordingTempPath = localRecordingPath(callMetaData, sessionFileTag, 'raw');
        const recordingWavPath = localRecordingPath(callMetaData, sessionFileTag, 'wav');
        const writeRecordingStream = fs.createWriteStream(recordingTempPath);
        const recordingFileSize = 0;

        const highWaterMarkSize = (callMetaData.samplingRate / 10) * 2 * 2;
        const audioInputStream = new BlockStream({ size: highWaterMarkSize });
        const socketCallMap: SocketCallData = {
            sessionSettings,
            callMetadata: {
                callId: callMetaData.callId,
                callEvent: callMetaData.callEvent,
                fromNumber: callMetaData.fromNumber,
                toNumber: callMetaData.toNumber,
                activeSpeaker: callMetaData.activeSpeaker,
                agentId: callMetaData.agentId,
                accessToken: callMetaData.accessToken,
                idToken: callMetaData.idToken,
                refreshToken: callMetaData.refreshToken,
                shouldRecordCall: callMetaData.shouldRecordCall,
                samplingRate: callMetaData.samplingRate,
                channels: callMetaData.channels,
                asrEngine: callMetaData.asrEngine,
                maxSpeakers: callMetaData.maxSpeakers,
                // Per-channel diarization opt-in, honoured by both engines. This
                // object is a WHITELIST copy, so a field omitted here is silently
                // dropped and the feature would appear to do nothing.
                diarizeSystemChannel: callMetaData.diarizeSystemChannel,
                diarizeMicChannel: callMetaData.diarizeMicChannel
            },
            audioInputStream: audioInputStream,
            writeRecordingStream: writeRecordingStream,
            recordingFileSize: recordingFileSize,
            recordingTempPath,
            recordingWavPath,
            startStreamTime: new Date(),
            speakerEvents: [],
            ended: false,
            // From the VERIFIED token, not the message body — this is what
            // later authorizes START_VIDEO for the same callId.
            ownerSub: getAuthenticatedCaller(request)?.sub,
        };
        const connection = { ws };
        sessionConnection.set(socketCallMap, connection);
        if (consentDecision.kind === 'consent' && clientConsent) {
            socketCallMap.consent = new ConsentSession(callMetaData.callId, consentDecision, clientConsent, {
                api: consentApi,
                callerUsername: getAuthenticatedCaller(request)?.username,
                putEvidence: putConsentEvidence,
                evidencePrefix: CONSENT_EVIDENCE_PREFIX,
                send: (frame) => sendFrame(connection.ws, frame),
                onGate: (open, reason, discard) => onConsentGate(connection.ws, socketCallMap, open, reason, discard),
                log: server.log,
            });
        }
        socketMap.set(ws, socketCallMap);
        void socketCallMap.consent?.begin();
        // Consent frames sent right behind this START, before transcription
        // starts: a held objection closes the gate before the engine is started.
        for (const frame of pendingConsentFrames.release(ws)) {
            await onConsentFrame(clientIP, ws, frame);
        }
        // An opt-in meeting starts transcribing when its consent record allows
        // it (onConsentGate); every other meeting starts now.
        if (
            !socketCallMap.ended &&
            !socketCallMap.transcriptionStarted &&
            (!socketCallMap.consent || socketCallMap.consent.gateOpen)
        ) {
            socketCallMap.transcriptionStarted = true;
            // Before transcription starts, so the clock the time-to-first-segment
            // metric reads covers establishing the engine's stream as well. That is
            // part of what an operator asking "why is there no transcript" is
            // waiting on. Not before: the wait for an opt-in meeting's consent is
            // not part of it.
            noteStreamingSessionStarted(socketCallMap.callMetadata);
            await startTranscription(socketCallMap, server);
        }
    } else if (callMetaData.callEvent === 'SPEAKER_CHANGE') {
        const socketData = socketMap.get(ws);
        server.log.debug(
            `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Received speaker change. Active speaker = ${callMetaData.activeSpeaker}`
        );

        if (socketData && socketData.callMetadata) {
            // We already know speaker name for the microphone channel (ch_1) - represented in callMetaData.agentId.
            // We should only use SPEAKER_CHANGE to track who is speaking on the incoming meeting channel (ch_0)
            // If the speaker is the same as the agentId, then we should ignore the event.
            const mic_channel_speaker = callMetaData.agentId;
            const activeSpeaker = callMetaData.activeSpeaker;
            if (activeSpeaker !== mic_channel_speaker) {
                server.log.debug(
                    `[${callMetaData.callEvent}]: [${callMetaData.callId}] - active speaker '${activeSpeaker}' assigned to meeting channel (ch_0) as name does not match mic channel (ch_1) speaker '${mic_channel_speaker}'`
                );
                // set active speaker in the socketData structure being used by startTranscribe results loop.
                socketData.callMetadata.activeSpeaker = callMetaData.activeSpeaker;
            } else {
                server.log.debug(
                    `[${callMetaData.callEvent}]: [${callMetaData.callId}] - active speaker '${activeSpeaker}' not assigned to meeting channel (ch_0) as name matches mic channel (ch_1) speaker '${mic_channel_speaker}'`
                );
            }
        } else {
            // this is not a valid call metadata
            server.log.error(
                `[${callMetaData.callEvent}]: [${
                    callMetaData.callId
                }] - Invalid call metadata: ${stringifyCallMetaData(callMetaData)}`
            );
        }
    } else if (callMetaData.callEvent === 'START_VIDEO') {
        // Second socket dedicated to a fragmented-MP4 video stream for an
        // audio call with the same callId. Servers without video support
        // ignore this event (unknown callEvent) and drop the binary frames.
        if (!callMetaData.callId) {
            server.log.error(
                `[START_VIDEO]: [${clientIP}] - START_VIDEO missing callId; ignoring.`
            );
            return;
        }
        // AUTHORIZATION. A video stream is admitted for this callId only when
        // there is a live audio session for the same callId AND that session's
        // owner is the same verified Cognito subject as this caller. The callId
        // in the frame is client-supplied and is not used as evidence of
        // anything on its own; ownership comes from the verified token.
        const caller = getAuthenticatedCaller(request);
        const audioSession = findAudioSessionByCallId(callMetaData.callId);
        if (!audioSession) {
            server.log.error(
                `[START_VIDEO]: [${clientIP}][${callMetaData.callId}] - No active audio call for this callId; refusing video stream.`
            );
            ws.close(1008, 'no active call for callId');
            return;
        }
        if (
            !caller?.sub ||
            !audioSession.ownerSub ||
            audioSession.ownerSub !== caller.sub
        ) {
            server.log.error(
                `[START_VIDEO]: [${clientIP}][${callMetaData.callId}] - Caller does not own this call; refusing video stream.`
            );
            ws.close(1008, 'not authorized for callId');
            return;
        }
        bindLogContext({ callId: callMetaData.callId });
        if (audioSession.consent && !audioSession.consent.gateOpen) {
            server.log.warn(
                `[START_VIDEO]: [${clientIP}][${callMetaData.callId}] - The meeting's consent does not allow capture now; refusing video stream.`
            );
            ws.close(1008, 'meeting consent does not allow capture');
            return;
        }
        const session = startVideoSession(
            callMetaData,
            server,
            audioSession.startStreamTime.getTime()
        );
        videoSocketMap.set(ws, session);
    } else if (callMetaData.callEvent === 'END_VIDEO') {
        const session = videoSocketMap.get(ws);
        if (!session) {
            server.log.error(
                `[END_VIDEO]: [${clientIP}][${callMetaData.callId}] - END_VIDEO without START_VIDEO on this socket; ignoring.`
            );
            return;
        }
        videoSocketMap.delete(ws);
        await endVideoSession(session.callId, server);
    } else if (callMetaData.callEvent === 'END') {
        const socketData = socketMap.get(ws);
        if (!socketData || !socketData.callMetadata) {
            server.log.error(
                `[${callMetaData.callEvent}]: [${
                    callMetaData.callId
                }] - Received END without starting a call:  ${stringifyCallMetaData(
                    callMetaData
                )}`
            );
            return;
        }
        server.log.debug(
            `[${callMetaData.callEvent}]: [${
                callMetaData.callId
            }] - Received call end event from client, writing it to KDS:  ${stringifyCallMetaData(
                callMetaData
            )}`
        );

        callMetaData.shouldRecordCall = resolveShouldRecordCall(
            callMetaData.shouldRecordCall,
            socketData.callMetadata.shouldRecordCall ??
                (socketData.sessionSettings ?? (await getSessionSettings(server.log))).recording.recordAudio
        );
        await endCall(ws, socketData, callMetaData);
    }
};

/**
 * The live session of a meeting on another connection of the same verified
 * user, detached from that connection, which is closed: its client has
 * reconnected and the new connection carries the meeting from now on.
 */
const takeOverLiveSession = (
    callId: string,
    callerSub: string | undefined,
    ws: WebSocket
): { session: SocketCallData; detachedForMs: number } | undefined => {
    if (!callerSub) {
        return undefined;
    }
    for (const [other, data] of socketMap) {
        if (other !== ws && !data.ended && data.callMetadata.callId === callId && data.ownerSub === callerSub) {
            socketMap.delete(other);
            try {
                other.close(1000, 'the meeting continues on a newer connection');
            } catch {
                // already closing
            }
            return { session: data, detachedForMs: 0 };
        }
    }
    return undefined;
};

/**
 * Continue a held session on the connection its client reconnected with: the
 * same recording file, the same transcription engine and transcript clock, and
 * no second START or END for the meeting. The audio the client could not send
 * while it was disconnected is missing from the recording and the transcript
 * alike, so the two stay in step.
 */
const resumeSession = async (
    clientIP: string,
    ws: WebSocket,
    socketData: SocketCallData,
    callMetaData: CallMetaData,
    detachedForMs: number
): Promise<void> => {
    // The new connection's tokens: the events written from here on carry them.
    socketData.callMetadata.accessToken = callMetaData.accessToken;
    socketData.callMetadata.idToken = callMetaData.idToken;
    socketData.callMetadata.refreshToken = callMetaData.refreshToken;
    const connection = sessionConnection.get(socketData);
    if (connection) {
        connection.ws = ws;
    } else {
        sessionConnection.set(socketData, { ws });
    }
    socketMap.set(ws, socketData);
    server.log.info(
        `[START]: [${clientIP}][${socketData.callMetadata.callId}] - The client reconnected after ${detachedForMs} ms; continuing the meeting's session (${socketData.recordingFileSize ?? 0} bytes recorded so far).`
    );
    for (const frame of pendingConsentFrames.release(ws)) {
        await onConsentFrame(clientIP, ws, frame);
    }
    if (
        !socketData.ended &&
        !socketData.transcriptionStarted &&
        (!socketData.consent || socketData.consent.gateOpen)
    ) {
        socketData.transcriptionStarted = true;
        noteStreamingSessionStarted(socketData.callMetadata);
        await startTranscription(socketData, server);
    }
};

/**
 * The three consent frames: CONSENT_SETTINGS (what a client's consent step
 * shows), CONSENT_OBJECTION and CONSENT_EVIDENCE (for the meeting this socket
 * started). True when the frame was one of them.
 */
const onConsentFrame = async (
    clientIP: string,
    ws: WebSocket,
    frame: Record<string, unknown>
): Promise<boolean> => {
    const kind = frame['callEvent'];
    if (kind === 'CONSENT_SETTINGS') {
        const settings = await readConsentSettings(server.log);
        sendFrame(
            ws,
            settings
                ? settingsFrame(settings)
                : {
                    event: 'CONSENT_SETTINGS',
                    available: false,
                    message: 'The meeting consent settings could not be read; try again.',
                }
        );
        return true;
    }
    if (kind !== 'CONSENT_OBJECTION' && kind !== 'CONSENT_EVIDENCE') {
        return false;
    }
    // Only for the meeting this socket started: the frame's own callId is not
    // used to find one.
    // An ended meeting still on the connection (its recording uploading) is not
    // the one a frame is for: the frame waits for the START that follows it.
    const registered = socketMap.get(ws);
    const socketData = registered && !registered.ended ? registered : undefined;
    if (!socketData) {
        const held = pendingConsentFrames.hold(ws, frame);
        if (held === 'held') {
            return true;
        }
        server.log.warn(`[CONSENT]: [${clientIP}] - ${kind} on a connection with no meeting; ignoring.`);
        sendFrame(ws, { event: 'CONSENT_STATE', code: 'NO_MEETING', message: `${kind} needs a started meeting on this connection.` });
        return true;
    }
    const callId = socketData.callMetadata.callId;
    if (kind === 'CONSENT_OBJECTION') {
        server.log.info(`[CONSENT]: [${clientIP}][${callId}] - The client reported an objection; stopping capture.`);
        if (socketData.consent) {
            void socketData.consent.objection({
                participantName: frame['participantName'] as string | undefined,
                source: frame['source'] as string | undefined,
                text: frame['text'] as string | undefined,
            });
        } else {
            // A meeting captured without a consent record: capture still stops.
            sendFrame(ws, {
                event: 'CONSENT_STATE',
                callId,
                captureAllowed: false,
                code: 'CONSENT_NOT_RECORDED',
                message: 'Capture stopped. The objection was not recorded, because this meeting has no consent record.',
            });
            await stopCaptureForConsent(ws, socketData, 'the client reported an objection');
        }
        return true;
    }
    if (!socketData.consent) {
        sendFrame(ws, {
            event: 'CONSENT_STATE',
            callId,
            code: 'EVIDENCE_REFUSED',
            message: 'Evidence can only be kept for a meeting with a consent record.',
        });
        return true;
    }
    void socketData.consent.evidence({
        image: frame['image'],
        capturedAt: frame['capturedAt'],
        note: frame['note'],
    });
    return true;
};

/** The meeting's consent record allows capture (open) or no longer does. */
const onConsentGate = (
    ws: WebSocket,
    socketData: SocketCallData,
    open: boolean,
    reason: string,
    discard = false
): void => {
    if (open) {
        if (!socketData.transcriptionStarted && !socketData.ended) {
            socketData.transcriptionStarted = true;
            noteStreamingSessionStarted(socketData.callMetadata);
            startTranscription(socketData, server).catch((error) => {
                server.log.error(
                    `[CONSENT]: [${socketData.callMetadata.callId}] - Could not start transcription: ${normalizeErrorForLogging(error)}`
                );
            });
        }
        return;
    }
    void (discard ? discardCaptureForConsent(ws, socketData, reason) : stopCaptureForConsent(ws, socketData, reason));
};

/**
 * Drop this connection's capture of a meeting that is not the caller's: its
 * streams, engine and temporary recording are released and the connection is
 * closed, with no end event and no recording written for that meeting.
 */
const discardCaptureForConsent = async (ws: WebSocket, socketData: SocketCallData, reason: string): Promise<void> => {
    const callMetaData = socketData.callMetadata;
    server.log.warn(`[CONSENT]: [${callMetaData.callId}] - Discarding this connection's capture: ${reason}.`);
    if (!socketData.ended) {
        socketData.ended = true;
        noteStreamingSessionEnded(callMetaData);
        socketData.writeRecordingStream?.end();
        socketData.audioInputStream?.end();
        socketData.audioInputStream?.destroy();
        try {
            await stopMicrovmAsr(socketData, server);
        } catch (error) {
            server.log.error(
                `[CONSENT]: [${callMetaData.callId}] - Error stopping the MicroVM ASR engine: ${normalizeErrorForLogging(error)}`
            );
        }
        detachedSessions.forget(socketData);
        if (socketData.recordingTempPath) {
            await deleteTempFile(callMetaData, socketData.recordingTempPath);
        }
        if (socketMap.get(ws) === socketData) {
            socketMap.delete(ws);
        }
        // This connection's video of the meeting, if it streamed any, is
        // discarded too -- unless another live session on this task streams the
        // same meeting, whose video it would be.
        const sharedWithAnother = [...socketMap.values()].some(
            (other) => other !== socketData && !other.ended && other.callMetadata.callId === callMetaData.callId
        );
        const video = getVideoSession(callMetaData.callId);
        if (video && !sharedWithAnother && !video.muxStarted) {
            video.discarding = true;
            await endVideoSession(callMetaData.callId, server);
        }
    }
    try {
        ws.close(1008, 'meeting consent does not allow capture');
    } catch {
        // already closed
    }
};

/**
 * Stop capturing a meeting for consent: its video stream is finalized, the
 * meeting is ended as if the client had sent END (what was captured while it
 * was allowed is kept), and the connection is closed so nothing more arrives.
 */
const stopCaptureForConsent = async (ws: WebSocket, socketData: SocketCallData, reason: string): Promise<void> => {
    const callId = socketData.callMetadata.callId;
    server.log.info(`[CONSENT]: [${callId}] - Ending the meeting: ${reason}.`);
    try {
        await endVideoSession(callId, server);
        await endCall(ws, socketData);
    } catch (error) {
        server.log.error(`[CONSENT]: [${callId}] - Error ending the meeting: ${normalizeErrorForLogging(error)}`);
    }
    try {
        ws.close(1008, 'meeting consent does not allow capture');
    } catch {
        // already closed
    }
};

const onWsClose = async (ws: WebSocket, code: number): Promise<void> => {
    ws.close(code);
    pendingConsentFrames.drop(ws);
    const videoSession = videoSocketMap.get(ws);
    if (videoSession) {
        // Video socket closed without END_VIDEO (crash / network drop). The
        // session survives a grace period for a client reconnect (fresh
        // START_VIDEO), then finalizes with whatever was received.
        server.log.debug(
            `[ON WSCLOSE]: [${videoSession.callId}] - Video socket closed without END_VIDEO.`
        );
        videoSocketMap.delete(ws);
        videoSocketDropped(videoSession.callId, server);
        return;
    }
    const socketData = socketMap.get(ws);
    if (socketData) {
        // A connection that closed without an END: unless the meeting is over,
        // hold its session for the client to reconnect to (see
        // calleventdata/detached-sessions.ts).
        if (!shuttingDown && !socketData.ended && detachedSessions.detach(socketData)) {
            socketMap.delete(ws);
            server.log.info(
                `[ON WSCLOSE]: [${socketData.callMetadata.callId}] - Connection closed without END; holding the meeting for ${AUDIO_RECONNECT_GRACE_MS} ms for the client to reconnect.`
            );
            return;
        }
        server.log.debug(
            `[ON WSCLOSE]: [${
                socketData.callMetadata.callId
            }] - Writing call end event due to websocket close event ${stringifyCallMetaData(
                socketData.callMetadata
            )}`
        );
        await endCall(ws, socketData);
    }
};

const endCall = async (
    ws: WebSocket | undefined,
    socketData: SocketCallData,
    callMetaData?: CallMetaData
): Promise<void> => {
    if (callMetaData === undefined) {
        callMetaData = socketData.callMetadata;
    }

    if (socketData !== undefined && socketData.ended === false) {
        socketData.ended = true;
        // Ended some other way (consent) while it waited for its client.
        detachedSessions.forget(socketData);

        // Inside the ran-once guard, so a second close does not count the same
        // meeting twice. Emits only when the session produced no transcript at
        // all, which is the one ending nothing else reports.
        noteStreamingSessionEnded(socketData.callMetadata);

        if (callMetaData !== undefined && callMetaData != null) {
            await writeCallEndEvent(callMetaData, server);
            // The raw file is complete only once its stream has flushed: the
            // WAV below is built by reading it back.
            const rawFlushed = finishWriting(socketData.writeRecordingStream);
            if (socketData.writeRecordingStream && socketData.recordingFileSize) {

                if (callMetaData.shouldRecordCall) {
                    server.log.debug(
                        `[${callMetaData.callEvent}]: [${
                            callMetaData.callId
                        }] - Audio Recording enabled. Writing to S3.: ${stringifyCallMetaData(
                            callMetaData
                        )}`
                    );
                    const header = createWavHeader(
                        callMetaData.samplingRate,
                        socketData.recordingFileSize
                    );
                    const rawPath =
                        socketData.recordingTempPath ?? localRecordingPath(callMetaData, 'session', 'raw');
                    const wavPath =
                        socketData.recordingWavPath ?? localRecordingPath(callMetaData, 'session', 'wav');
                    await rawFlushed;

                    const readStream = fs.createReadStream(rawPath);
                    const writeStream = fs.createWriteStream(wavPath);
                    writeStream.write(header);
                    for await (const chunk of readStream) {
                        writeStream.write(chunk);
                    }
                    // Await the flush: the file is read back for the S3 upload
                    // and (when video was recorded) by the ffmpeg mux.
                    await new Promise<void>((resolve) =>
                        writeStream.end(() => resolve())
                    );

                    const upload = await uploadRecording(
                        recordingStore,
                        {
                            prefix: RECORDING_FILE_PREFIX,
                            baseName: posixifyFilename(callMetaData.callId),
                            wavPath,
                            rawPath,
                        },
                        server.log
                    );
                    await deleteTempFile(callMetaData, rawPath);

                    // If a video stream was recorded for this call, hand the
                    // WAV over so it can be muxed into the video MP4 (the
                    // video session deletes it when done). Otherwise delete.
                    const wavAdopted = notifyAudioRecordingDone(
                        callMetaData.callId,
                        wavPath,
                        server
                    );
                    if (!wavAdopted) {
                        await deleteTempFile(callMetaData, wavPath);
                    }

                    server.log.info(
                        `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Recording ${upload.outcome}: ${upload.wavKey} (${socketData.recordingFileSize} bytes)`
                    );
                    // A meeting that already has a recording keeps it: this
                    // session's audio is stored beside it, not in its place.
                    if (!upload.storedSeparately) {
                        const recordingUrl = `https://${RECORDINGS_BUCKET_NAME}.s3.${AWS_REGION}.amazonaws.com/${upload.wavKey}`;
                        await writeCallRecordingEvent(callMetaData, recordingUrl, server);
                    }
                } else {
                    // No audio WAV to mux; let any video session finalize
                    // video-only. The raw audio is not kept either.
                    notifyAudioRecordingDone(callMetaData.callId, undefined, server);
                    await rawFlushed;
                    if (socketData.recordingTempPath) {
                        await deleteTempFile(callMetaData, socketData.recordingTempPath);
                    }
                    server.log.info(
                        `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Recording NOT uploaded: shouldRecordCall=${callMetaData.shouldRecordCall}.`
                    );
                    server.log.debug(
                        `[${callMetaData.callEvent}]: [${
                            callMetaData.callId
                        }] - Audio Recording disabled. Add s3 url event is not written to KDS. : ${stringifyCallMetaData(
                            callMetaData
                        )}`
                    );
                }
            } else {
                // No audio recording was produced (e.g. no audio bytes ever
                // arrived); let any video session finalize video-only.
                notifyAudioRecordingDone(callMetaData.callId, undefined, server);
                await rawFlushed;
                if (socketData.recordingTempPath) {
                    await deleteTempFile(callMetaData, socketData.recordingTempPath);
                }
                server.log.info(
                    `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Recording NOT uploaded: no audio was written (${socketData.recordingFileSize ?? 0} bytes).`
                );
            }

            if (socketData.audioInputStream) {
                server.log.debug(
                    `[${callMetaData.callEvent}]: [${
                        callMetaData.callId
                    }] - Closing audio input stream:  ${stringifyCallMetaData(callMetaData)}`
                );
                socketData.audioInputStream.end();
                socketData.audioInputStream.destroy();
            }

            // Flushes the tail utterance on each channel, then terminates the
            // MicroVM. No-op for meetings transcribed by Amazon Transcribe.
            try {
                await stopMicrovmAsr(socketData, server);
            } catch (error) {
                server.log.error(
                    `[${callMetaData.callEvent}]: [${
                        callMetaData.callId
                    }] - Error stopping the MicroVM ASR engine: ${normalizeErrorForLogging(error)}`
                );
            }

            if (socketData) {
                server.log.debug(
                    `[${callMetaData.callEvent}]: [${
                        callMetaData.callId
                    }] - Deleting websocket from map: ${stringifyCallMetaData(callMetaData)}`
                );
                // Only this meeting's entry: a client that reuses the connection
                // may have started its next meeting while this one finished.
                if (ws && socketMap.get(ws) === socketData) {
                    socketMap.delete(ws);
                }
                // And wherever else it streams now, after a reconnect.
                const current = sessionConnection.get(socketData)?.ws;
                if (current && socketMap.get(current) === socketData) {
                    socketMap.delete(current);
                }
            }
        } else {
            server.log.error('[END]: Missing Call Meta Data in END event');
        }
    } else {
        if (callMetaData !== undefined && callMetaData != null) {
            server.log.error(
                `[${callMetaData.callEvent}]: [${
                    callMetaData.callId
                }] - Duplicate End call event. Already received the end call event: ${stringifyCallMetaData(
                    callMetaData
                )}`
            );
        } else {
            server.log.error(
                '[END]: Duplicate End call event. Missing Call Meta Data in END event'
            );
        }
    }
};

/** End a write stream and wait for it to flush; resolves at once when there is none. */
const finishWriting = (writable: fs.WriteStream | undefined): Promise<void> =>
    new Promise<void>((resolve) => {
        if (!writable || writable.writableFinished || writable.destroyed) {
            resolve();
            return;
        }
        writable.once('finish', () => resolve());
        writable.once('error', () => resolve());
        writable.end();
    });

/**
 * A session's local recording file: the meeting's callId, made safe as a file
 * name, plus a tag of the session's own.
 */
const localRecordingPath = (callMetaData: CallMetaData, sessionTag: string, extension: 'raw' | 'wav'): string => {
    const name = path
        .basename(`${posixifyFilename(callMetaData.callId)}.${sessionTag}.${extension}`)
        .replace(/[^a-zA-Z0-9._-]/g, '_');
    if (!name || name === '.' || name === '..') {
        throw new Error('Invalid recording filename provided');
    }
    return path.resolve(LOCAL_TEMP_DIR, name);
};

const deleteTempFile = async (
    callMetaData: CallMetaData,
    sourceFile: string
) => {
    // Ensure we're not deleting files outside of our designated directory
    if (!sourceFile.startsWith(LOCAL_TEMP_DIR)) {
        server.log.error(
            `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Attempted to delete file outside of temp directory: ${sourceFile}`
        );
        return;
    }
    try {
        await fs.promises.unlink(sourceFile);
        server.log.debug(
            `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Deleted tmp file ${sourceFile}`
        );
    } catch (err) {
        server.log.error(
            `[${callMetaData.callEvent}]: [${
                callMetaData.callId
            }] - Error deleting tmp file ${sourceFile} : ${normalizeErrorForLogging(
                err
            )}`
        );
    }
};

// Start the websocket server on default port 3000 if no port supplied in environment variables
server.listen(
    {
        port: parseInt(process.env?.['SERVERPORT'] ?? '8080'),
        host: process.env?.['SERVERHOST'] ?? '127.0.0.1',
    },
    (err: Error | null) => {
        if (err) {
            server.log.error(
                `[WS SERVER STARTUP]: Error starting websocket server: ${normalizeErrorForLogging(
                    err
                )}`
            );
            process.exit(1);
        }
        server.log.debug(
            '[WS SERVER STARTUP]: Websocket server is ready and listening.'
        );
        server.log.info(`[[WS SERVER STARTUP]]: Routes: \n${server.printRoutes()}`);
    }
);

/**
 * Graceful shutdown. ECS sends SIGTERM on every deploy, scale-in, and
 * health-check replacement, then SIGKILLs after StopTimeout. End-of-call video
 * muxes run detached from the request path, so without this they are killed
 * mid-ffmpeg and the user's recording vanishes with no event and no error.
 *
 * We stop accepting new work, give in-flight muxes a bounded window to finish,
 * then exit. Keep SHUTDOWN_MUX_GRACE_MS below the container's StopTimeout.
 */
const SHUTDOWN_MUX_GRACE_MS = parseInt(
    process.env['SHUTDOWN_MUX_GRACE_MS'] || '90000',
    10
);
let shuttingDown = false;
const shutdown = async (signal: string): Promise<void> => {
    if (shuttingDown) {
        return;
    }
    shuttingDown = true;
    server.log.info(`[SHUTDOWN]: Received ${signal}; finishing in-flight work.`);

    // Meetings waiting for their client to reconnect will not get one from this
    // task: end them now, so their recordings are uploaded before it exits.
    for (const socketData of detachedSessions.drain()) {
        try {
            await endCall(undefined, socketData);
        } catch (err) {
            server.log.error(
                `[SHUTDOWN]: Error ending ${socketData.callMetadata.callId}: ${normalizeErrorForLogging(err)}`
            );
        }
    }

    // Release ASR MicroVMs explicitly. Their maximumDurationInSeconds is only a
    // backstop, so without this a deploy or scale-in would leave one running
    // (and billing) per in-flight meeting until that ceiling expired.
    for (const socketData of socketMap.values()) {
        if (socketData.asr) {
            try {
                await stopMicrovmAsr(socketData, server);
            } catch (err) {
                server.log.error(
                    `[SHUTDOWN]: Error releasing the ASR MicroVM for ${socketData.callMetadata.callId}: ${normalizeErrorForLogging(err)}`
                );
            }
        }
    }

    try {
        await awaitPendingMuxes(server, SHUTDOWN_MUX_GRACE_MS);
    } catch (err) {
        server.log.error(
            `[SHUTDOWN]: Error waiting for in-flight video muxes: ${normalizeErrorForLogging(err)}`
        );
    }
    try {
        await server.close();
    } catch (err) {
        server.log.error(
            `[SHUTDOWN]: Error closing server: ${normalizeErrorForLogging(err)}`
        );
    }
    server.log.info('[SHUTDOWN]: Complete.');
    process.exit(0);
};
process.on('SIGTERM', () => void shutdown('SIGTERM'));
process.on('SIGINT', () => void shutdown('SIGINT'));
