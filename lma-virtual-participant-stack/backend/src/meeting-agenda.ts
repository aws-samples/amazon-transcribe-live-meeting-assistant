/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The agenda a Virtual Participant carries onto the meeting it records.
 *
 * The agenda is given when the VP is started or scheduled -- on the LMA form or
 * by an MCP client -- and stored on the VP's own row. The VP reads it from that
 * row when it starts, rather than receiving it as an environment variable, so
 * neither launch path (the Step Functions state machine and the scheduler
 * Lambda) has to carry it, and a long agenda cannot run into the size limit on
 * container overrides. It reaches the meeting record in the START event, so the
 * meeting has it from its creation instead of from a second write that would
 * race the creation.
 */

export interface VpAgenda {
  agenda: string;
  agendaSource: 'scheduled' | 'mcp';
}

/** Same bound as the updateCallAgenda and createVirtualParticipant resolvers. */
export const MAX_AGENDA_CHARS = 10000;

/**
 * The agenda stored on a VP row, or null when it has none.
 *
 * The source is whatever the createVirtualParticipant resolver recorded; any
 * other value is read as "scheduled", the form's, because the resolver accepts
 * no other source for this row and a meeting record should not carry one.
 */
export function agendaFromVpRow(row: { agenda?: unknown; agendaSource?: unknown } | null | undefined): VpAgenda | null {
  const agenda = typeof row?.agenda === 'string' ? row.agenda.trim() : '';
  if (!agenda) return null;
  return {
    agenda: agenda.slice(0, MAX_AGENDA_CHARS),
    agendaSource: row?.agendaSource === 'mcp' ? 'mcp' : 'scheduled',
  };
}

/**
 * The fields the START event adds for an agenda, named as CreateCallInput names
 * them. Empty when there is no agenda, so a meeting without one is created
 * exactly as before.
 */
export function agendaStartFields(
  agenda: VpAgenda | null,
  updatedBy: string,
  now: Date = new Date(),
): Record<string, string> {
  if (!agenda) return {};
  return {
    Agenda: agenda.agenda,
    AgendaSource: agenda.agendaSource,
    AgendaUpdatedBy: updatedBy || 'LMA',
    AgendaUpdatedAt: now.toISOString(),
  };
}
