/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The CallId on a VP's row and the capture session of that meeting it runs
 * (docs/virtual-participant.md#rejoin), and how the VP reads them at startup.
 */

/** The CallId on a VP's row, and the capture session of that meeting it runs. */
export interface VpCallSession {
  callId: string;
  /** 1 for a VP that was not rejoined; 2 and up, one per rejoin. */
  sessionNumber: number;
  /** Where the session starts on the meeting's transcript timeline, in seconds. */
  sessionTimeOffset: number;
}

/**
 * The VP's row could not be read. Not a row without a CallId: a rejoined VP
 * that took it for one would start its meeting under a new CallId.
 */
export class CallSessionReadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'CallSessionReadError';
  }
}

/**
 * Read a VpCallSession from a getVirtualParticipant answer: null without a
 * CallId; a session number or offset that is absent or not usable is session 1,
 * at 0, which is every VP that was not rejoined.
 */
export const callSessionFromVpRow = (row: any): VpCallSession | null => {
  const callId = typeof row?.CallId === 'string' ? row.CallId.trim() : '';
  if (!callId) return null;
  const number = Number(row?.sessionNumber);
  const offset = Number(row?.sessionTimeOffset);
  return {
    callId,
    sessionNumber: Number.isInteger(number) && number > 1 ? number : 1,
    sessionTimeOffset: Number.isFinite(offset) && offset > 0 ? offset : 0,
  };
};

export interface ReadCallSessionOptions {
  /** Reads in all, the first included. */
  attempts?: number;
  /** Wait before the second read; doubled before each later one. */
  delayMs?: number;
  sleep?: (ms: number) => Promise<void>;
  onRetry?: (attempt: number, error: unknown) => void;
}

/**
 * Read the VP's CallId and session, reading again when the read fails.
 *
 * A row read without a CallId answers null at once (a first launch, which
 * makes its own CallId). A read that still fails after `attempts` throws the
 * last CallSessionReadError, and the caller fails the launch rather than guess.
 */
export const readCallSession = async (
  read: () => Promise<VpCallSession | null>,
  { attempts = 4, delayMs = 2000, sleep, onRetry }: ReadCallSessionOptions = {},
): Promise<VpCallSession | null> => {
  const wait = sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await read();
    } catch (error) {
      if (!(error instanceof CallSessionReadError)) throw error;
      lastError = error;
      if (attempt < attempts) {
        onRetry?.(attempt, error);
        await wait(delayMs * 2 ** (attempt - 1));
      }
    }
  }
  throw lastError;
};
