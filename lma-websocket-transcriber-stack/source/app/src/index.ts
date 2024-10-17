// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

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
} from './calleventdata';

import {
    createWavHeader,
    posixifyFilename,
    normalizeErrorForLogging,
    getClientIP,
} from './utils';

import { jwtVerifier } from './utils/jwt-verifier';

const AWS_REGION = process.env['AWS_REGION'] || 'us-east-1';
const RECORDINGS_BUCKET_NAME =
  process.env['RECORDINGS_BUCKET_NAME'] || undefined;
const RECORDING_FILE_PREFIX =
  process.env['RECORDING_FILE_PREFIX'] || 'lma-audio-recordings/';
const CPU_HEALTH_THRESHOLD = parseInt(
    process.env['CPU_HEALTH_THRESHOLD'] || '50',
    10
);
const LOCAL_TEMP_DIR = process.env['LOCAL_TEMP_DIR'] || '/tmp/';
const WS_LOG_LEVEL = process.env['WS_LOG_LEVEL'] || 'debug';
const WS_LOG_INTERVAL = parseInt(process.env['WS_LOG_INTERVAL'] || '120', 10);
const SHOULD_RECORD_CALL = (process.env['SHOULD_RECORD_CALL'] || '') === 'true';

const s3Client = new S3Client({ region: AWS_REGION });

const socketMap = new Map<WebSocket, SocketCallData>();

// create fastify server (with logging enabled for non-PROD environments)
const server = fastify({
    logger: {
        level: WS_LOG_LEVEL,
        prettyPrint: {
            ignore: 'pid,hostname',
            translateTime: 'SYS:HH:MM:ss.l',
            colorize: false,
            levelFirst: true,
        },
    },
    disableRequestLogging: true,
});
// register the @fastify/websocket plugin with the fastify server
server.register(websocket);

// Setup preHandler hook to authenticate
server.addHook('preHandler', async (request, reply) => {
    if (!request.url.includes('health')) {
        const clientIP = getClientIP(request.headers);
        server.log.debug(
            `[AUTH]: [${clientIP}] - Received preHandler hook for authentication. URI: <${
                request.url
            }>, Headers: ${JSON.stringify(request.headers)}`
        );

        await jwtVerifier(request, reply);
    }
});

// Setup Route for websocket connection
server.get(
    '/api/v1/ws',
    { websocket: true, logLevel: 'debug' },
    (connection, request) => {
        const clientIP = getClientIP(request.headers);
        server.log.debug(
            `[NEW CONNECTION]: [${clientIP}] - Received new connection request @ /api/v1/ws. URI: <${
                request.url
            }>, Headers: ${JSON.stringify(request.headers)}`
        );

        registerHandlers(clientIP, connection.socket, request); // setup the handler functions for websocket events
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
            server.log.error(
                `[ON MESSAGE]: [${clientIP}] - Error processing message: ${normalizeErrorForLogging(
                    error
                )}`
            );
            process.exit(1);
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
    } else {
        server.log.error(
            `[ON BINARY MESSAGE]: [${clientIP}] - Error: received audio data before metadata. Check logs for errors in START event.`
        );
    }
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
  const callMetaData: CallMetaData = JSON.parse(data);
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

      // if (typeof callMetaData.shouldRecordCall === 'undefined' || callMetaData.shouldRecordCall === null) {
      //     server.log.debug(`[${callMetaData.callEvent}]: [${callMetaData.callId}] - Client did not provide ShouldRecordCall in CallMetaData. Defaulting to  CFN parameter EnableAudioRecording =  ${SHOULD_RECORD_CALL}`);

      //     callMetaData.shouldRecordCall = SHOULD_RECORD_CALL;
      // } else {
      //     server.log.debug(`[${callMetaData.callEvent}]: [${callMetaData.callId}] - Using client provided ShouldRecordCall parameter in CallMetaData =  ${callMetaData.shouldRecordCall}`);
      // }

      callMetaData.agentId = callMetaData.agentId || randomUUID();

      await writeCallStartEvent(callMetaData, server);
      const tempRecordingFilename = getTempRecordingFileName(callMetaData);
      const writeRecordingStream = fs.createWriteStream(
          path.join(LOCAL_TEMP_DIR, tempRecordingFilename)
      );
      const recordingFileSize = 0;

      const highWaterMarkSize = (callMetaData.samplingRate / 10) * 2 * 2;
      const audioInputStream = new BlockStream({ size: highWaterMarkSize });
      const socketCallMap: SocketCallData = {
      // copy (not reference) callMetaData into socketCallMap
          callMetadata: Object.assign({}, callMetaData),
          audioInputStream: audioInputStream,
          writeRecordingStream: writeRecordingStream,
          recordingFileSize: recordingFileSize,
          startStreamTime: new Date(),
          speakerEvents: [],
          ended: false,
      };
      socketMap.set(ws, socketCallMap);
      startTranscribe(socketCallMap, server);
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

      if (
          typeof callMetaData.shouldRecordCall === 'undefined' ||
      callMetaData.shouldRecordCall === null
      ) {
          server.log.debug(
              `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Client did not provide ShouldRecordCall in CallMetaData. Defaulting to  CFN parameter EnableAudioRecording =  ${SHOULD_RECORD_CALL}`
          );

          callMetaData.shouldRecordCall = SHOULD_RECORD_CALL;
      } else {
          server.log.debug(
              `[${callMetaData.callEvent}]: [${callMetaData.callId}] - Using client provided ShouldRecordCall parameter in CallMetaData =  ${callMetaData.shouldRecordCall}`
          );
      }
      await endCall(ws, socketData, callMetaData);
  }
};

const onWsClose = async (ws: WebSocket, code: number): Promise<void> => {
    ws.close(code);
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
                    const readStream = fs.createReadStream(
                        path.join(LOCAL_TEMP_DIR, tempRecordingFilename)
                    );
                    const writeStream = fs.createWriteStream(
                        path.join(LOCAL_TEMP_DIR, wavRecordingFilename)
                    );
                    writeStream.write(header);
                    for await (const chunk of readStream) {
                        writeStream.write(chunk);
                    }
                    writeStream.end();

                    await writeToS3(callMetaData, tempRecordingFilename);
                    await writeToS3(callMetaData, wavRecordingFilename);
                    await deleteTempFile(
                        callMetaData,
                        path.join(LOCAL_TEMP_DIR, tempRecordingFilename)
                    );
                    await deleteTempFile(
                        callMetaData,
                        path.join(LOCAL_TEMP_DIR, wavRecordingFilename)
                    );

                    const url = new URL(
                        RECORDING_FILE_PREFIX + wavRecordingFilename,
                        `https://${RECORDINGS_BUCKET_NAME}.s3.${AWS_REGION}.amazonaws.com`
                    );
                    const recordingUrl = url.href;

                    await writeCallRecordingEvent(callMetaData, recordingUrl, server);
                } else {
                    server.log.debug(
                        `[${callMetaData.callEvent}]: [${
                            callMetaData.callId
                        }] - Audio Recording disabled. Add s3 url event is not written to KDS. : ${JSON.stringify(
                            callMetaData
                        )}`
                    );
                }
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
    const sourceFile = path.join(LOCAL_TEMP_DIR, tempFileName);

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
    (err) => {
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
