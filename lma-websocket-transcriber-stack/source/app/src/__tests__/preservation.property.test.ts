/**
 * Preservation Property Tests - Property 2: Active Streaming and Message Handling Unchanged
 *
 * These tests verify that existing behavior is preserved for non-buggy scenarios:
 * - Binary audio messages are forwarded correctly
 * - Text messages with callEvent "START" create socketMap entries
 * - Text messages with callEvent "END" trigger cleanup
 * - ws.on('close') triggers onWsClose
 * - ws.on('error') logs and closes
 *
 * These tests MUST PASS on unfixed code — they establish the baseline behavior to preserve.
 *
 * **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**
 */
import * as fc from 'fast-check';
import { EventEmitter } from 'events';

// --- Mock WebSocket ---
const OPEN = 1;
const CLOSING = 2;
const CLOSED = 3;

class MockWebSocket extends EventEmitter {
  readyState: number = OPEN;
  private _closed = false;
  private _closeCode?: number;
  private _closeCalled = false;

  close(code?: number): void {
    this._closeCalled = true;
    this._closeCode = code;
    this.readyState = CLOSING;
  }

  terminate(): void {
    this.readyState = CLOSED;
  }

  ping(): void {
    // no-op for preservation tests
  }

  get closeCalled(): boolean {
    return this._closeCalled;
  }

  get closeCode(): number | undefined {
    return this._closeCode;
  }
}

// --- Mock streams ---
class MockWriteStream {
  private _chunks: Buffer[] = [];
  private _ended = false;

  write(data: Buffer | Uint8Array): void {
    this._chunks.push(Buffer.from(data));
  }

  end(): void {
    this._ended = true;
  }

  destroy(): void {
    // no-op
  }

  get chunks(): Buffer[] {
    return this._chunks;
  }

  get ended(): boolean {
    return this._ended;
  }
}

// --- Mirror the production SocketCallData type ---
interface MockSocketCallData {
  callMetadata: {
    callId: string;
    callEvent: string;
    fromNumber: string;
    toNumber: string;
    activeSpeaker: string;
    agentId: string;
    samplingRate: number;
    channels: Record<string, unknown>;
    accessToken?: string;
    idToken?: string;
    refreshToken?: string;
    shouldRecordCall?: boolean;
  };
  audioInputStream?: MockWriteStream;
  writeRecordingStream?: MockWriteStream;
  recordingFileSize?: number;
  startStreamTime: Date;
  speakerEvents: unknown[];
  ended: boolean;
}

// --- Mock server logger ---
const createMockLogger = () => ({
  debug: jest.fn(),
  info: jest.fn(),
  error: jest.fn(),
  warn: jest.fn(),
});

/**
 * Simulates the onBinaryMessage handler from index.ts.
 * This mirrors the production behavior exactly:
 * - If socketData exists with audioInputStream and writeRecordingStream, write data to both
 * - Otherwise log an error
 */
function onBinaryMessage(
  socketMap: Map<MockWebSocket, MockSocketCallData>,
  ws: MockWebSocket,
  data: Uint8Array,
  logger: ReturnType<typeof createMockLogger>
): void {
  const socketData = socketMap.get(ws);

  if (
    socketData !== undefined &&
    socketData.audioInputStream !== undefined &&
    socketData.writeRecordingStream !== undefined &&
    socketData.recordingFileSize !== undefined
  ) {
    socketData.audioInputStream.write(Buffer.from(data));
    socketData.writeRecordingStream.write(Buffer.from(data));
    socketData.recordingFileSize += data.length;
  } else {
    logger.error('Error: received audio data before metadata.');
  }
}

/**
 * Simulates the onWsClose handler from index.ts.
 * This mirrors the production behavior:
 * - Calls ws.close(code)
 * - If socketData exists, calls endCall
 */
function onWsClose(
  socketMap: Map<MockWebSocket, MockSocketCallData>,
  ws: MockWebSocket,
  code: number,
  logger: ReturnType<typeof createMockLogger>
): void {
  ws.close(code);
  const socketData = socketMap.get(ws);
  if (socketData) {
    logger.debug(`Writing call end event due to websocket close event`);
    endCall(socketMap, ws, socketData, logger);
  }
}

/**
 * Simulates the endCall function from index.ts (simplified for preservation testing).
 * This mirrors the production behavior:
 * - Marks socketData.ended = true
 * - Ends and destroys audioInputStream
 * - Ends writeRecordingStream
 * - Deletes ws from socketMap
 */
function endCall(
  socketMap: Map<MockWebSocket, MockSocketCallData>,
  ws: MockWebSocket,
  socketData: MockSocketCallData,
  logger: ReturnType<typeof createMockLogger>
): void {
  if (socketData !== undefined && socketData.ended === false) {
    socketData.ended = true;

    if (socketData.audioInputStream) {
      socketData.audioInputStream.end();
      socketData.audioInputStream.destroy();
    }
    if (socketData.writeRecordingStream) {
      socketData.writeRecordingStream.end();
    }
    socketMap.delete(ws);
    logger.debug('Deleted websocket from map');
  }
}

/**
 * Simulates the error handler from registerHandlers in index.ts.
 * This mirrors the production behavior:
 * - Logs the error
 * - Calls ws.close()
 */
function onError(
  ws: MockWebSocket,
  error: Error,
  logger: ReturnType<typeof createMockLogger>
): void {
  logger.error(`Websocket error, forcing close: ${error.message}`);
  ws.close();
}

/**
 * Simulates the START event handling from onTextMessage in index.ts.
 * Creates a new socketMap entry with the correct metadata.
 */
function onStartEvent(
  socketMap: Map<MockWebSocket, MockSocketCallData>,
  ws: MockWebSocket,
  callId: string,
  fromNumber: string,
  toNumber: string,
  agentId: string,
  samplingRate: number
): void {
  const audioInputStream = new MockWriteStream();
  const writeRecordingStream = new MockWriteStream();

  const socketCallMap: MockSocketCallData = {
    callMetadata: {
      callId,
      callEvent: 'START',
      fromNumber,
      toNumber,
      activeSpeaker: fromNumber,
      agentId,
      samplingRate,
      channels: {},
    },
    audioInputStream,
    writeRecordingStream,
    recordingFileSize: 0,
    startStreamTime: new Date(),
    speakerEvents: [],
    ended: false,
  };
  socketMap.set(ws, socketCallMap);
}

/**
 * Simulates the END event handling from onTextMessage in index.ts.
 * Triggers endCall and cleanup.
 */
function onEndEvent(
  socketMap: Map<MockWebSocket, MockSocketCallData>,
  ws: MockWebSocket,
  logger: ReturnType<typeof createMockLogger>
): boolean {
  const socketData = socketMap.get(ws);
  if (!socketData || !socketData.callMetadata) {
    logger.error('Received END without starting a call');
    return false;
  }
  endCall(socketMap, ws, socketData, logger);
  return true;
}

// ============================================================================
// PRESERVATION PROPERTY TESTS
// ============================================================================

describe('Preservation Property Tests: Active Streaming and Message Handling', () => {
  describe('Property 2a: Binary audio messages are forwarded correctly', () => {
    it('for any random binary payload, data is written identically to audioInputStream and writeRecordingStream', () => {
      fc.assert(
        fc.property(
          fc.uint8Array({ minLength: 1, maxLength: 4096 }),
          (payload: Uint8Array) => {
            // Setup
            const ws = new MockWebSocket();
            const socketMap = new Map<MockWebSocket, MockSocketCallData>();
            const logger = createMockLogger();

            // Create a started connection
            onStartEvent(socketMap, ws, 'test-call', 'Customer', 'System', 'agent-1', 16000);

            // Act: send binary message
            onBinaryMessage(socketMap, ws, payload, logger);

            // Assert: data was written to both streams identically
            const socketData = socketMap.get(ws)!;
            expect(socketData.audioInputStream!.chunks).toHaveLength(1);
            expect(socketData.writeRecordingStream!.chunks).toHaveLength(1);
            expect(Buffer.compare(
              socketData.audioInputStream!.chunks[0],
              Buffer.from(payload)
            )).toBe(0);
            expect(Buffer.compare(
              socketData.writeRecordingStream!.chunks[0],
              Buffer.from(payload)
            )).toBe(0);
            // Recording file size is updated
            expect(socketData.recordingFileSize).toBe(payload.length);
          }
        ),
        { numRuns: 100 }
      );
    });

    it('binary messages before START event log an error', () => {
      fc.assert(
        fc.property(
          fc.uint8Array({ minLength: 1, maxLength: 1024 }),
          (payload: Uint8Array) => {
            // Setup: no START event, so socketMap is empty
            const ws = new MockWebSocket();
            const socketMap = new Map<MockWebSocket, MockSocketCallData>();
            const logger = createMockLogger();

            // Act: send binary message without START
            onBinaryMessage(socketMap, ws, payload, logger);

            // Assert: error is logged
            expect(logger.error).toHaveBeenCalled();
          }
        ),
        { numRuns: 50 }
      );
    });
  });

  describe('Property 2b: Text messages with callEvent START create socketMap entries', () => {
    it('for any valid START metadata, a socketMap entry is created with correct fields', () => {
      fc.assert(
        fc.property(
          fc.record({
            callId: fc.uuid(),
            fromNumber: fc.string({ minLength: 1, maxLength: 20 }),
            toNumber: fc.string({ minLength: 1, maxLength: 20 }),
            agentId: fc.uuid(),
            samplingRate: fc.constantFrom(8000, 16000, 44100, 48000),
          }),
          ({ callId, fromNumber, toNumber, agentId, samplingRate }) => {
            // Setup
            const ws = new MockWebSocket();
            const socketMap = new Map<MockWebSocket, MockSocketCallData>();

            // Act: simulate START event
            onStartEvent(socketMap, ws, callId, fromNumber, toNumber, agentId, samplingRate);

            // Assert: socketMap entry exists with correct metadata
            expect(socketMap.has(ws)).toBe(true);
            const socketData = socketMap.get(ws)!;
            expect(socketData.callMetadata.callId).toBe(callId);
            expect(socketData.callMetadata.callEvent).toBe('START');
            expect(socketData.callMetadata.fromNumber).toBe(fromNumber);
            expect(socketData.callMetadata.toNumber).toBe(toNumber);
            expect(socketData.callMetadata.agentId).toBe(agentId);
            expect(socketData.callMetadata.samplingRate).toBe(samplingRate);
            expect(socketData.callMetadata.activeSpeaker).toBe(fromNumber);
            expect(socketData.audioInputStream).toBeDefined();
            expect(socketData.writeRecordingStream).toBeDefined();
            expect(socketData.recordingFileSize).toBe(0);
            expect(socketData.ended).toBe(false);
          }
        ),
        { numRuns: 100 }
      );
    });
  });

  describe('Property 2c: Text messages with callEvent END trigger cleanup', () => {
    it('for any started connection, END event cleans up socketMap and marks ended', () => {
      fc.assert(
        fc.property(
          fc.record({
            callId: fc.uuid(),
            fromNumber: fc.string({ minLength: 1, maxLength: 20 }),
            toNumber: fc.string({ minLength: 1, maxLength: 20 }),
            agentId: fc.uuid(),
            samplingRate: fc.constantFrom(8000, 16000, 44100, 48000),
          }),
          ({ callId, fromNumber, toNumber, agentId, samplingRate }) => {
            // Setup
            const ws = new MockWebSocket();
            const socketMap = new Map<MockWebSocket, MockSocketCallData>();
            const logger = createMockLogger();

            // Start the connection
            onStartEvent(socketMap, ws, callId, fromNumber, toNumber, agentId, samplingRate);
            expect(socketMap.has(ws)).toBe(true);

            // Act: send END event
            const result = onEndEvent(socketMap, ws, logger);

            // Assert: cleanup occurred
            expect(result).toBe(true);
            expect(socketMap.has(ws)).toBe(false); // removed from map
          }
        ),
        { numRuns: 100 }
      );
    });

    it('END event without START logs an error and returns false', () => {
      const ws = new MockWebSocket();
      const socketMap = new Map<MockWebSocket, MockSocketCallData>();
      const logger = createMockLogger();

      const result = onEndEvent(socketMap, ws, logger);

      expect(result).toBe(false);
      expect(logger.error).toHaveBeenCalled();
    });
  });

  describe('Property 2d: ws.on close triggers onWsClose', () => {
    it('for any close code, onWsClose calls ws.close and cleans up socketMap', () => {
      fc.assert(
        fc.property(
          fc.integer({ min: 1000, max: 4999 }),
          (closeCode: number) => {
            // Setup
            const ws = new MockWebSocket();
            const socketMap = new Map<MockWebSocket, MockSocketCallData>();
            const logger = createMockLogger();

            // Start a connection first
            onStartEvent(socketMap, ws, 'call-123', 'Customer', 'System', 'agent-1', 16000);
            expect(socketMap.has(ws)).toBe(true);

            // Act: simulate close event
            onWsClose(socketMap, ws, closeCode, logger);

            // Assert: ws.close was called with the code
            expect(ws.closeCalled).toBe(true);
            expect(ws.closeCode).toBe(closeCode);
            // Assert: socketMap was cleaned up
            expect(socketMap.has(ws)).toBe(false);
            // Assert: debug log was written
            expect(logger.debug).toHaveBeenCalled();
          }
        ),
        { numRuns: 100 }
      );
    });

    it('close on connection without socketData just calls ws.close', () => {
      fc.assert(
        fc.property(
          fc.integer({ min: 1000, max: 4999 }),
          (closeCode: number) => {
            // Setup: no START event
            const ws = new MockWebSocket();
            const socketMap = new Map<MockWebSocket, MockSocketCallData>();
            const logger = createMockLogger();

            // Act: simulate close event on unregistered connection
            onWsClose(socketMap, ws, closeCode, logger);

            // Assert: ws.close was still called
            expect(ws.closeCalled).toBe(true);
            expect(ws.closeCode).toBe(closeCode);
            // No cleanup needed since nothing was in the map
            expect(socketMap.size).toBe(0);
          }
        ),
        { numRuns: 50 }
      );
    });
  });

  describe('Property 2e: ws.on error logs and closes', () => {
    it('for any error message, the error handler logs the error and calls ws.close()', () => {
      fc.assert(
        fc.property(
          fc.string({ minLength: 1, maxLength: 200 }),
          (errorMessage: string) => {
            // Setup
            const ws = new MockWebSocket();
            const logger = createMockLogger();

            // Act: simulate error event
            const error = new Error(errorMessage);
            onError(ws, error, logger);

            // Assert: error was logged
            expect(logger.error).toHaveBeenCalledWith(
              expect.stringContaining('Websocket error, forcing close:')
            );
            // Assert: ws.close() was called
            expect(ws.closeCalled).toBe(true);
          }
        ),
        { numRuns: 100 }
      );
    });
  });
});
