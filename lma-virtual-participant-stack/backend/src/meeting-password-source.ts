/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Where the Virtual Participant gets its meeting password at startup.
 *
 * The password is stored on the VP's own row in the VirtualParticipant table
 * (createVirtualParticipant and updateScheduledVirtualParticipant write it
 * there), and no launch request carries it: the VP reads it from the row when
 * it starts, on every launch type, with its task or MicroVM role. The read asks
 * for the row's id and meetingPassword only, which is all the role may read.
 *
 * This module holds the decision and the retry; it makes no AWS call itself, so
 * it can be tested directly. startup-meeting-password.ts wires it to DynamoDB.
 */

/** The row as read: null when no row has the VP's id. */
export interface MeetingPasswordRow {
  meetingPassword?: string;
}

export interface ResolveMeetingPasswordOptions {
  /** The process environment; MEETING_PASSWORD, VIRTUAL_PARTICIPANT_ID, VP_TABLE_NAME and LOCAL_TEST are read. */
  env: NodeJS.ProcessEnv;
  /** Read the VP's row. Throws when it cannot be read. */
  readRow: (tableName: string, vpId: string) => Promise<MeetingPasswordRow | null>;
  /** Reads in all, the first included. */
  attempts?: number;
  /** Wait before the second read; doubled before each later one. */
  delayMs?: number;
  sleep?: (ms: number) => Promise<void>;
  onRetry?: (attempt: number, error: unknown) => void;
}

/** Where the password came from, for the startup log line. */
export type MeetingPasswordOrigin = 'environment' | 'record' | 'none';

export type MeetingPasswordResult =
  | { ok: true; password: string; origin: MeetingPasswordOrigin }
  | { ok: false; error: string };

/**
 * The meeting password, as given (not yet normalized: details.ts applies
 * normalizeMeetingPassword once the platform is known).
 *
 * - MEETING_PASSWORD set and not empty in the environment: that value. Only a
 *   launch request queued by an earlier release sets it, which can still reach
 *   a container during a stack update (an EventBridge schedule created before
 *   the update, for example), and a local run (local-test.sh). It can be
 *   removed, with this branch, once no such request can remain.
 * - No VP id or no table name, or a local run: no row to read, so no password.
 * - Otherwise the row's meetingPassword; absent or empty is no password. A read
 *   that fails, or finds no row, is tried again, and after `attempts` reads the
 *   result says why, so the caller can fail the launch with that reason.
 */
export async function resolveMeetingPassword({
  env,
  readRow,
  attempts = 4,
  delayMs = 1000,
  sleep,
  onRetry,
}: ResolveMeetingPasswordOptions): Promise<MeetingPasswordResult> {
  const fromEnvironment = env.MEETING_PASSWORD;
  if (fromEnvironment !== undefined && fromEnvironment.trim() !== '') {
    return { ok: true, password: fromEnvironment, origin: 'environment' };
  }

  const vpId = (env.VIRTUAL_PARTICIPANT_ID || '').trim();
  const tableName = (env.VP_TABLE_NAME || '').trim();
  if (!vpId || !tableName || env.LOCAL_TEST === 'true') {
    return { ok: true, password: '', origin: 'none' };
  }

  const wait = sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const row = await readRow(tableName, vpId);
      if (row) {
        const password = typeof row.meetingPassword === 'string' ? row.meetingPassword : '';
        return { ok: true, password, origin: password ? 'record' : 'none' };
      }
      lastError = new Error(`no Virtual Participant record has the id ${vpId}`);
    } catch (error) {
      lastError = error;
    }
    if (attempt < attempts) {
      onRetry?.(attempt, lastError);
      await wait(delayMs * 2 ** (attempt - 1));
    }
  }
  const reason = lastError instanceof Error ? lastError.message : String(lastError);
  return { ok: false, error: reason };
}
