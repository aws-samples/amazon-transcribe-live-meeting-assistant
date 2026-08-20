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
    runCalibration,
    CalibrationError,
    CalibrationRequest,
} from './calleventdata';

import {
    createWavHeader,
    posixifyFilename,
    normalizeErrorForLogging,
    getClientIP,
    resolveShouldRecordCall,
} from './utils';

import { jwtVerifier, getAuthenticatedCaller } from './utils/jwt-verifier';

import {
    VideoSession,
    startVideoSession,
    endVideoSession,
    videoSocketDropped,
    writeVideoChunk,
    notifyAudioRecordingDone,
    awaitPendingMuxes,
} from './videorecording';

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
const WS_LOG_INTERVAL = parseInt(process.env['WS_LOG_INTERVAL'] || '120', 10);
const SHOULD_RECORD_CALL = (process.env['SHOULD_RECORD_CALL'] || '') === 'true';

const s3Client = new S3Client({ region: AWS_REGION });

const socketMap = new Map<WebSocket, SocketCallData>();
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
    return undefined;
};

// create fastify server (with logging enabled for non-PROD environments)
const server = fastify({
    logger: {
        level: WS_LOG_LEVEL,
        transport: {
            target: 'pino-pretty',
            options: {
                ignore: 'pid,hostname',
                translateTime: 'SYS:HH:MM:ss.l',
                colorize: false,
                levelFirst: true,
            },
        },
    },
    disableRequestLogging: true,
});
// register the @fastify/websocket plugin with the fastify server
server.register(websocket);

// Setup preHandler hook to authenticate
server.addHook('preHandler', async (request, reply) => {
    // A CORS preflight carries no credentials by design, so authenticating it
    // would 401 every cross-origin call before the real request is ever made.
    if (!request.url.includes('health') && request.method !== 'OPTIONS') {
        const clientIP = getClientIP(request.headers);
        server.log.debug(
            `[AUTH]: [${clientIP}] - Received preHandler hook for authentication. URI: <${
                request.url
            }>, Headers: ${JSON.stringify(request.headers)}`
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
            const clientIP = getClientIP(request.headers);
            server.log.debug(
                `[NEW CONNECTION]: [${clientIP}] - Received new connection request @ /api/v1/ws. URI: <${
                    request.url
                }>, Headers: ${JSON.stringify(request.headers)}`
            );

            registerHandlers(clientIP, socket, request); // setup the handler functions for websocket events
        }
    );
});

// The ASR Config admin page lives on the UI's CloudFront domain, not this one, so
// its calls are cross-origin. No cookies are involved (the bearer token travels in
// the query string, as the WebSocket route's does), so echoing the origin grants
// nothing a request without that token could not already do.
const ASR_CALIBRATE_PATH = '/api/v1/asr/calibrate';
const ADMIN_GROUP = 'Admin';

const allowCrossOrigin = (
    request: FastifyRequest,
    reply: { header: (name: string, value: string) => unknown }
): void => {
    reply.header('Access-Control-Allow-Origin', request.headers.origin || '*');
    reply.header('Vary', 'Origin');
    reply.header('Access-Control-Allow-Methods', 'POST, OPTIONS');
    reply.header('Access-Control-Allow-Headers', 'authorization, content-type');
    reply.header('Access-Control-Max-Age', '600');
};

// The calibration sample is posted as raw WAV bytes. It is never stored: the audio
// is embedded in memory and only the resulting statistics come back, so a sample can
// be a rehearsed recording or a clip from a public corpus without leaving a copy.
const ASR_CALIBRATION_MAX_UPLOAD_BYTES = parseInt(
    process.env['ASR_CALIBRATION_MAX_UPLOAD_BYTES'] || String(64 * 1024 * 1024),
    10
);

server.addContentTypeParser(
    ['application/octet-stream', 'audio/wav', 'audio/wave', 'audio/x-wav'],
    { parseAs: 'buffer', bodyLimit: ASR_CALIBRATION_MAX_UPLOAD_BYTES },
    (_request, body, done) => done(null, body)
);

server.options(ASR_CALIBRATE_PATH, { logLevel: 'warn' }, (request, reply) => {
    allowCrossOrigin(request, reply);
    reply.code(204).send();
});

/**
 * Measure the diarization operating point from a meeting this deployment recorded.
 *
 * Admin-only: it launches an ASR MicroVM and reads a recording. The route only
 * measures and reports — the admin decides whether to save the result — so a run
 * on unrepresentative audio cannot quietly change how meetings are transcribed.
 */
server.post(
    ASR_CALIBRATE_PATH,
    { logLevel: 'info', bodyLimit: ASR_CALIBRATION_MAX_UPLOAD_BYTES },
    async (request, reply) => {
        allowCrossOrigin(request, reply);
        const caller = getAuthenticatedCaller(request);
        if (!caller?.groups.includes(ADMIN_GROUP)) {
            server.log.warn(
                `[ASR CALIBRATE]: refused for non-admin caller ${caller?.username || caller?.sub || 'unknown'}`
            );
            return reply
                .code(403)
                .send({ message: 'Calibration is limited to users in the Admin group.' });
        }
        const wav = Buffer.isBuffer(request.body) ? request.body : Buffer.alloc(0);
        const query = request.query as Record<string, string | undefined>;
        const perChannel = query['maxSegmentsPerChannel'];
        try {
            const run = await runCalibration(
                {
                    wav,
                    ...(perChannel ? { maxSegmentsPerChannel: Number(perChannel) } : {}),
                } as CalibrationRequest,
                server
            );
            return reply.code(200).send(run);
        } catch (error) {
            const status = error instanceof CalibrationError ? error.status : 500;
            const message =
                error instanceof CalibrationError
                    ? error.message
                    : `calibration failed: ${normalizeErrorForLogging(error)}`;
            server.log.error(`[ASR CALIBRATE]: ${status} - ${message}`);
            return reply.code(status).send({ message });
        }
    }
);

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
            `[HEALTH CHECK]: [${remoteIp}] - Received First health check from load balancer. URI: <${
                request.url
            }>, Headers: ${JSON.stringify(
                request.headers
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
                } from load balancer. URI: <${request.url}>, Headers: ${JSON.stringify(
                    request.headers
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
    request: FastifyRequest
): void => {
    ws.on('message', async (data, isBinary): Promise<void> => {
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
            try {
                ws.close(1011, 'internal error processing message');
            } catch (closeErr) {
                server.log.error(
                    `[ON MESSAGE]: [${clientIP}] - Error closing connection: ${normalizeErrorForLogging(closeErr)}`
                );
            }
        }
    });

    ws.on('close', (code: number) => {
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
    });

    ws.on('error', (error: Error) => {
        server.log.error(
            `[ON WSERROR]: [${clientIP}] - Websocket error, forcing close: ${normalizeErrorForLogging(
                error
            )}`
        );
        ws.close();
    });
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
    // from the ASR Config page without redeploying the task.
    const runtime = await getAsrRuntimeConfig(server_);
    if (resolveAsrEngine(socketData.callMetadata, server_, runtime) === 'microvm') {
        if (await startMicrovmAsr(socketData, server_)) {
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
        // for tearing down every other call on this task.
        server.log.error(
            `[ON TEXT MESSAGE]: [${clientIP}] - Ignoring unparseable control frame: ${normalizeErrorForLogging(parseErr)}`
        );
        return;
    }
    if (!match) {
        server.log.error(
            `[AUTH]: [${clientIP}] - No Bearer token found in header or query string. URI: <${
                request.url
            }>, Headers: ${JSON.stringify(request.headers)}`
        );

        return;
    }

    const accessToken = match[1];

    try {
        server.log.debug(
            `[ON TEXT MESSAGE]: [${clientIP}][${callMetaData.callId}] - Call Metadata received from client: ${data}`
        );
    } catch (error) {
        server.log.error(
            `[ON TEXT MESSAGE]: [${clientIP}][${
                callMetaData.callId
            }] - Error parsing call metadata: ${data} ${normalizeErrorForLogging(
                error
            )}`
        );
        callMetaData.callId = randomUUID();
    }

    callMetaData.accessToken = accessToken;
    callMetaData.idToken = idToken;
    callMetaData.refreshToken = refreshToken;

    if (callMetaData.callEvent === 'START') {
        // generate random metadata if none is provided
        callMetaData.callId = callMetaData.callId || randomUUID();
        callMetaData.fromNumber = callMetaData.fromNumber || 'Customer Phone';
        callMetaData.toNumber = callMetaData.toNumber || 'System Phone';
        callMetaData.activeSpeaker =
            callMetaData.activeSpeaker ?? callMetaData?.fromNumber ?? 'unknown';

        callMetaData.shouldRecordCall = resolveShouldRecordCall(
            callMetaData.shouldRecordCall,
            SHOULD_RECORD_CALL
        );

        callMetaData.agentId = callMetaData.agentId || randomUUID();

        await writeCallStartEvent(callMetaData, server);
        const tempRecordingFilename = getTempRecordingFileName(callMetaData);
        // Sanitize filename to prevent path traversal attacks
        const sanitizedFilename = path.basename(tempRecordingFilename).replace(/[^a-zA-Z0-9._-]/g, '_');
        if (!sanitizedFilename || sanitizedFilename === '.' || sanitizedFilename === '..') {
            throw new Error('Invalid recording filename provided');
        }
        const writeRecordingStream = fs.createWriteStream(
            path.resolve(LOCAL_TEMP_DIR, sanitizedFilename)
        );
        const recordingFileSize = 0;

        const highWaterMarkSize = (callMetaData.samplingRate / 10) * 2 * 2;
        const audioInputStream = new BlockStream({ size: highWaterMarkSize });
        const socketCallMap: SocketCallData = {
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
            startStreamTime: new Date(),
            speakerEvents: [],
            ended: false,
            // From the VERIFIED token, not the message body — this is what
            // later authorizes START_VIDEO for the same callId.
            ownerSub: getAuthenticatedCaller(request)?.sub,
        };
        socketMap.set(ws, socketCallMap);
        await startTranscription(socketCallMap, server);
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
                }] - Invalid call metadata: ${JSON.stringify(callMetaData)}`
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
        // AUTHORIZATION. callId comes from the client and is guessable (the web
        // UI builds it as "<meeting topic> - <timestamp>"), so accepting it on
        // faith would let ANY authenticated user attach video to someone else's
        // call — and, because the mux pulls in that call's audio WAV, would
        // publish the victim's audio under an object the attacker can read.
        // Require a LIVE audio session for this callId, owned by the same
        // verified Cognito subject that opened it.
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
        const session = startVideoSession(callMetaData, server);
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
                }] - Received END without starting a call:  ${JSON.stringify(
                    callMetaData
                )}`
            );
            return;
        }
        server.log.debug(
            `[${callMetaData.callEvent}]: [${
                callMetaData.callId
            }] - Received call end event from client, writing it to KDS:  ${JSON.stringify(
                callMetaData
            )}`
        );

        callMetaData.shouldRecordCall = resolveShouldRecordCall(
            callMetaData.shouldRecordCall,
            socketData.callMetadata.shouldRecordCall ?? SHOULD_RECORD_CALL
        );
        await endCall(ws, socketData, callMetaData);
    }
};

const onWsClose = async (ws: WebSocket, code: number): Promise<void> => {
    ws.close(code);
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
        server.log.debug(
            `[ON WSCLOSE]: [${
                socketData.callMetadata.callId
            }] - Writing call end event due to websocket close event ${JSON.stringify(
                socketData.callMetadata
            )}`
        );
        await endCall(ws, socketData);
    }
};

const endCall = async (
    ws: WebSocket,
    socketData: SocketCallData,
    callMetaData?: CallMetaData
): Promise<void> => {
    if (callMetaData === undefined) {
        callMetaData = socketData.callMetadata;
    }

    if (socketData !== undefined && socketData.ended === false) {
        socketData.ended = true;

        if (callMetaData !== undefined && callMetaData != null) {
            await writeCallEndEvent(callMetaData, server);
            if (socketData.writeRecordingStream && socketData.recordingFileSize) {
                socketData.writeRecordingStream.end();

                if (callMetaData.shouldRecordCall) {
                    server.log.debug(
                        `[${callMetaData.callEvent}]: [${
                            callMetaData.callId
                        }] - Audio Recording enabled. Writing to S3.: ${JSON.stringify(
                            callMetaData
                        )}`
                    );
                    const header = createWavHeader(
                        callMetaData.samplingRate,
                        socketData.recordingFileSize
                    );
                    const tempRecordingFilename = getTempRecordingFileName(callMetaData);
                    const wavRecordingFilename = getWavRecordingFileName(callMetaData);
                    // Sanitize filenames to prevent path traversal attacks
                    const sanitizedTempFilename = path.basename(tempRecordingFilename).replace(/[^a-zA-Z0-9._-]/g, '_');
                    const sanitizedWavFilename = path.basename(wavRecordingFilename).replace(/[^a-zA-Z0-9._-]/g, '_');
                    
                    if (!sanitizedTempFilename || sanitizedTempFilename === '.' || sanitizedTempFilename === '..' ||
                        !sanitizedWavFilename || sanitizedWavFilename === '.' || sanitizedWavFilename === '..') {
                        throw new Error('Invalid filename provided for recording conversion');
                    }
                    
                    const readStream = fs.createReadStream(
                        path.resolve(LOCAL_TEMP_DIR, sanitizedTempFilename)
                    );
                    const writeStream = fs.createWriteStream(
                        path.resolve(LOCAL_TEMP_DIR, sanitizedWavFilename)
                    );
                    writeStream.write(header);
                    for await (const chunk of readStream) {
                        writeStream.write(chunk);
                    }
                    // Await the flush: the file is read back for the S3 upload
                    // and (when video was recorded) by the ffmpeg mux.
                    await new Promise<void>((resolve) =>
                        writeStream.end(() => resolve())
                    );

                    await writeToS3(callMetaData, sanitizedTempFilename);
                    await writeToS3(callMetaData, sanitizedWavFilename);
                    await deleteTempFile(
                        callMetaData,
                        path.resolve(LOCAL_TEMP_DIR, sanitizedTempFilename)
                    );

                    // If a video stream was recorded for this call, hand the
                    // WAV over so it can be muxed into the video MP4 (the
                    // video session deletes it when done). Otherwise delete.
                    const wavPath = path.resolve(LOCAL_TEMP_DIR, sanitizedWavFilename);
                    const wavAdopted = notifyAudioRecordingDone(
                        callMetaData.callId,
                        wavPath,
                        server
                    );
                    if (!wavAdopted) {
                        await deleteTempFile(callMetaData, wavPath);
                    }

                    const recordingUrl = `https://${RECORDINGS_BUCKET_NAME}.s3.${AWS_REGION}.amazonaws.com/${RECORDING_FILE_PREFIX}${wavRecordingFilename}`;
                    server.log.info(
                        `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Recording uploaded: ${RECORDING_FILE_PREFIX}${wavRecordingFilename} (${socketData.recordingFileSize} bytes)`
                    );

                    await writeCallRecordingEvent(callMetaData, recordingUrl, server);
                } else {
                    // No audio WAV to mux; let any video session finalize
                    // video-only.
                    notifyAudioRecordingDone(callMetaData.callId, undefined, server);
                    server.log.info(
                        `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Recording NOT uploaded: shouldRecordCall=${callMetaData.shouldRecordCall} (deployment default ${SHOULD_RECORD_CALL}).`
                    );
                    server.log.debug(
                        `[${callMetaData.callEvent}]: [${
                            callMetaData.callId
                        }] - Audio Recording disabled. Add s3 url event is not written to KDS. : ${JSON.stringify(
                            callMetaData
                        )}`
                    );
                }
            } else {
                // No audio recording was produced (e.g. no audio bytes ever
                // arrived); let any video session finalize video-only.
                notifyAudioRecordingDone(callMetaData.callId, undefined, server);
                server.log.info(
                    `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Recording NOT uploaded: no audio was written (${socketData.recordingFileSize ?? 0} bytes).`
                );
            }

            if (socketData.audioInputStream) {
                server.log.debug(
                    `[${callMetaData.callEvent}]: [${
                        callMetaData.callId
                    }] - Closing audio input stream:  ${JSON.stringify(callMetaData)}`
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
                    }] - Deleting websocket from map: ${JSON.stringify(callMetaData)}`
                );
                socketMap.delete(ws);
            }
        } else {
            server.log.error('[END]: Missing Call Meta Data in END event');
        }
    } else {
        if (callMetaData !== undefined && callMetaData != null) {
            server.log.error(
                `[${callMetaData.callEvent}]: [${
                    callMetaData.callId
                }] - Duplicate End call event. Already received the end call event: ${JSON.stringify(
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

const writeToS3 = async (callMetaData: CallMetaData, tempFileName: string) => {
    // Sanitize filename to prevent path traversal attacks
    const sanitizedFileName = path.basename(tempFileName).replace(/[^a-zA-Z0-9._-]/g, '_');
    if (!sanitizedFileName || sanitizedFileName === '.' || sanitizedFileName === '..') {
        throw new Error('Invalid filename provided');
    }
    const sourceFile = path.resolve(LOCAL_TEMP_DIR, sanitizedFileName);
    let data;
    const fileStream = fs.createReadStream(sourceFile);
    const uploadParams = {
        Bucket: RECORDINGS_BUCKET_NAME,
        Key: RECORDING_FILE_PREFIX + tempFileName,
        Body: fileStream,
    };
    try {
        data = await s3Client.send(new PutObjectCommand(uploadParams));
        server.log.debug(
            `[${callMetaData.callEvent}]: [${
                callMetaData.callId
            }] - Uploaded ${sourceFile} to S3 complete: ${JSON.stringify(data)}`
        );
    } catch (err) {
        server.log.error(
            `[${callMetaData.callEvent}]: [${
                callMetaData.callId
            }] - Error uploading ${sourceFile} to S3: ${normalizeErrorForLogging(
                err
            )}`
        );
    } finally {
        fileStream.destroy();
    }
    return data;
};

const getTempRecordingFileName = (callMetaData: CallMetaData): string => {
    return `${posixifyFilename(callMetaData.callId)}.raw`;
};

const getWavRecordingFileName = (callMetaData: CallMetaData): string => {
    return `${posixifyFilename(callMetaData.callId)}.wav`;
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
