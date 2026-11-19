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
 * The row is the source whenever there is one to read: a VP id and a table
 * name, outside a local run. Its meetingPassword wins over anything in the
 * environment, so a password changed on the row after the launch request was
 * queued is the one used. A read that fails, or finds no row, is tried again.
 *
 * MEETING_PASSWORD in the environment is used only where the row gives no
 * password: there is no row to read, the row has none, or every read failed.
 * Two things set it:
 * - local-test.sh, which runs the VP with no row. This use stays.
 * - a launch request queued by an earlier release, which can still reach an
 *   ECS container during a stack update (an EventBridge schedule created
 *   before the update, for example). This use can be removed once no such
 *   request can remain; the local-run use above still needs the variable.
 *
 * With no password from either, the result is no password, except that reads
 * which all failed with nothing in the environment fail the launch with the
 * reason.
 */
export async function resolveMeetingPassword({
  env,
  readRow,
  attempts = 4,
  delayMs = 1000,
  sleep,
  onRetry,
}: ResolveMeetingPasswordOptions): Promise<MeetingPasswordResult> {
  const fromEnvironment = (env.MEETING_PASSWORD ?? '').trim() !== '' ? (env.MEETING_PASSWORD as string) : '';
  const environmentOr = (fallback: MeetingPasswordResult): MeetingPasswordResult =>
    fromEnvironment ? { ok: true, password: fromEnvironment, origin: 'environment' } : fallback;

  const vpId = (env.VIRTUAL_PARTICIPANT_ID || '').trim();
  const tableName = (env.VP_TABLE_NAME || '').trim();
  if (!vpId || !tableName || env.LOCAL_TEST === 'true') {
    return environmentOr({ ok: true, password: '', origin: 'none' });
  }

  const wait = sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      const row = await readRow(tableName, vpId);
      if (row) {
        const password = typeof row.meetingPassword === 'string' ? row.meetingPassword : '';
        if (password.trim() !== '') return { ok: true, password, origin: 'record' };
        return environmentOr({ ok: true, password: '', origin: 'none' });
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
  return environmentOr({ ok: false, error: reason });
}

/** The status detail of a launch that failed because the password could not be read. */
export const PASSWORD_UNREADABLE_MESSAGE =
  "Could not read the meeting password from this Virtual Participant's record, so it did not start. Try again.";
/** Its endTrigger. */
export const PASSWORD_UNREADABLE_TRIGGER = 'startup:meeting-password-unreadable';

/**
 * Fail the launch when the startup read gave no password: the VP is marked
 * FAILED with a reason and the process exits non-zero, before any join.
 * Joining without the password would fail at the meeting's passcode prompt, or
 * join with no password where one was given. Does nothing when the read
 * succeeded. A failure to record the status is logged and the process still
 * exits.
 */
export async function failLaunchIfPasswordUnreadable(
  result: MeetingPasswordResult,
  deps: {
    setFailed: (message: string, endTrigger: string) => Promise<unknown>;
    exit: (code: number) => void;
    logError: (message: string) => void;
  },
): Promise<void> {
  if (result.ok) return;
  try {
    await deps.setFailed(PASSWORD_UNREADABLE_MESSAGE, PASSWORD_UNREADABLE_TRIGGER);
  } catch (statusError) {
    deps.logError(`Failed to record the startup failure: ${statusError}`);
  }
  deps.exit(1);
}
