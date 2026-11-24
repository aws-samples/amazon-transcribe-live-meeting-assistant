/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
// Frames from the transcriber are open-ended JSON objects.
/* eslint-disable @typescript-eslint/no-explicit-any */

/**
 * The browser extension's consent step, without React.
 *
 * The WebSocket transcriber answers a CONSENT_SETTINGS frame with the Meeting
 * Consent settings in force; the extension shows the notice, offers to copy it
 * or post it in the meeting chat, collects the user's confirmation and how it
 * was given, and sends that in the `consent` field of START. The transcriber
 * records it on the meeting and gates capture on the record. The same step as
 * the Stream Audio/Video page's (see docs/meeting-consent.md and
 * docs/websocket-streaming-api.md, "6.1.5 Meeting Consent Frames").
 */

export const CONSENT_MODE_OFF = 'off';
export const CONSENT_MODE_NOTIFY = 'notify';
export const CONSENT_MODE_OPT_IN = 'opt_in';
export const CLIENT_NAME = 'Browser extension';
export const CLIENT_TYPE = 'BROWSER_EXTENSION';

export type ConsentMethod = 'verbal' | 'chat' | 'invite_terms';

export const CONSENT_METHODS: { value: ConsentMethod, label: string, description: string }[] = [
  { value: 'verbal', label: 'Out loud', description: 'I read the notice out and everyone heard it.' },
  { value: 'chat', label: 'In the meeting chat', description: 'The notice was posted in the meeting chat.' },
  { value: 'invite_terms', label: "In the invitation's terms", description: 'The meeting invitation said the meeting is recorded and transcribed.' },
];

/** The transcriber's CONSENT_SETTINGS answer. */
export type ConsentSettings = {
  event: 'CONSENT_SETTINGS',
  available: boolean,
  mode?: string,
  rule?: string | null,
  supported?: boolean,
  reason?: string,
  noticeTemplate?: string,
  consentPhrase?: string,
  objectionPhrase?: string,
  clientsWithoutConsent?: string,
  message?: string,
  /** Whether this client may send screen video (docs/websocket-streaming-api.md, "6.1.6 Screen Video"). */
  videoAllowed?: boolean,
};

/**
 * Where the notice stands in the meeting chat: not posted from here, posted
 * and waiting for the user to say whether it appeared, or confirmed by them.
 * It counts as posted only once the user has seen it appear.
 */
export type ChatPost = 'none' | 'sent' | 'confirmed' | 'failed';

export type ConsentStep = {
  confirmed: boolean,
  method?: ConsentMethod,
  statement: string,
  chatPost: ChatPost,
};

export const emptyStep = (): ConsentStep => ({ confirmed: false, statement: '', chatPost: 'none' });

export const SETTINGS_LOADING = 'Reading the meeting consent settings; try again in a moment.';
export const OBJECTION_NOT_SENT =
  'The objection could not be sent to LMA because the connection was lost, so capture stopped. '
  + "Record the objection in the Consent section of the meeting's page in LMA.";


/** The notice with its placeholders filled as the server fills them. */
export const renderNotice = (
  template: string | undefined,
  { lmaUser, meeting, consentPhrase, objectionPhrase }: { lmaUser?: string, meeting?: string, consentPhrase?: string, objectionPhrase?: string },
): string => {
  const values: Record<string, string> = {
    '{LMA_USER}': lmaUser || 'The meeting organizer',
    '{meeting}': meeting || 'this meeting',
    '{consent_phrase}': consentPhrase || '',
    '{objection_phrase}': objectionPhrase || '',
  };
  return (template || '')
    .replace(/\{LMA_USER\}|\{meeting\}|\{consent_phrase\}|\{objection_phrase\}/g, (key) => values[key])
    .slice(0, 2000);
};

/** Whether the consent step applies: the server answered and its mode is not off. */
export const consentRequired = (settings: ConsentSettings | null | undefined): boolean =>
  Boolean(settings?.available) && [CONSENT_MODE_NOTIFY, CONSENT_MODE_OPT_IN].includes(settings?.mode || '');

/**
 * Why capture may not start yet, or '' when it may. An opt-in meeting needs the
 * confirmation and how it was given; a notice posted in the chat from here
 * must have been seen to appear before "In the meeting chat" counts.
 */
export const startBlockedReason = (settings: ConsentSettings | null | undefined, step: ConsentStep, loading = false): string => {
  // Not knowing the settings yet is not the same as there being none: the
  // notice the meeting may need has not been shown.
  if (loading) return SETTINGS_LOADING;
  if (!consentRequired(settings)) return '';
  if (settings?.supported === false) return settings.reason || 'This deployment does not allow capture from the browser extension.';
  if (step.chatPost === 'sent') return 'Say whether the notice appeared in the meeting chat.';
  if (settings?.mode === CONSENT_MODE_OPT_IN) {
    if (!step.confirmed) return 'Confirm that everyone agreed before you start.';
    if (!step.method) return 'Choose how everyone agreed.';
  }
  if (step.confirmed && !step.method) return 'Choose how everyone was told.';
  return '';
};

/** The `consent` field of START; mode off when the settings are not known. */
export const consentPayload = (
  settings: ConsentSettings | null | undefined,
  step: ConsentStep,
  { lmaUser, meetingName, noticeText }: { lmaUser: string, meetingName: string, noticeText: string },
): Record<string, unknown> => {
  if (!consentRequired(settings)) {
    return { mode: CONSENT_MODE_OFF, confirmed: false, client: CLIENT_NAME };
  }
  return {
    mode: settings?.mode,
    confirmed: Boolean(step.confirmed),
    ...(step.method ? { method: step.method } : {}),
    noticeText,
    lmaUser,
    meetingName,
    ...(step.statement.trim() ? { statement: step.statement.trim() } : {}),
    client: CLIENT_NAME,
  };
};

/** A JSON frame from the transcriber, or null for anything else. */
export const parseServerFrame = (data: unknown): Record<string, any> | null => {
  if (typeof data !== 'string') return null;
  try {
    const frame = JSON.parse(data);
    return frame && typeof frame === 'object' && typeof frame.event === 'string' ? frame : null;
  } catch {
    return null;
  }
};

/** A control frame without its consent step: END and SPEAKER_CHANGE do not repeat START's. */
export const withoutConsent = <T extends { consent?: unknown }>({ consent, ...frame }: T): Omit<T, 'consent'> => frame;

/** Whether a CONSENT_STATE frame says the transcriber stopped, or will not start, capturing. */
export const captureStopped = (frame: Record<string, any> | null): boolean =>
  frame?.event === 'CONSENT_STATE' && frame.captureAllowed === false;

/** What the panel says about the meeting's consent record, from a CONSENT_STATE frame. */
export const describeConsentState = (frame: Record<string, any> | null): { type: 'success' | 'info' | 'warning' | 'error', text: string } | null => {
  if (!frame || frame.event !== 'CONSENT_STATE') return null;
  if (frame.message) return { type: frame.captureAllowed ? 'warning' : 'error', text: frame.message };
  if (frame.status === 'objected') return { type: 'error', text: 'A participant objected. Capture stopped.' };
  if (frame.captureAllowed) return { type: 'success', text: `Consent recorded on the meeting (${frame.status}). Recording and transcribing.` };
  return { type: 'info', text: `Consent ${frame.status || 'pending'}: not recording or transcribing yet.` };
};

/** The transcriber's upgrade URL: a browser WebSocket cannot set headers. */
export const transcriberUrl = (endpoint: string, tokens: { access_token?: string, id_token?: string, refresh_token?: string }): string => {
  const query = `authorization=${encodeURIComponent(`Bearer ${tokens.access_token ?? ''}`)}`
    + `&id_token=${encodeURIComponent(tokens.id_token ?? '')}&refresh_token=${encodeURIComponent(tokens.refresh_token ?? '')}`;
  return `${endpoint}${endpoint.includes('?') ? '&' : '?'}${query}`;
};

/**
 * Ask the transcriber for its settings on a short-lived connection, before
 * capture starts. null when it gives no answer within `timeoutMs` (an older
 * transcriber, or no connection): the step is then not shown, and START
 * carries mode off, which an opt-in deployment refuses with a message.
 */
export const fetchConsentSettings = (
  url: string,
  { timeoutMs = 10000, WebSocketClass = WebSocket }: { timeoutMs?: number, WebSocketClass?: typeof WebSocket } = {},
): Promise<ConsentSettings | null> => new Promise((resolve) => {
  let done = false;
  let socket: WebSocket;
  const finish = (value: ConsentSettings | null) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    try {
      socket?.close();
    } catch {
      // already closed
    }
    resolve(value);
  };
  const timer = setTimeout(() => finish(null), timeoutMs);
  try {
    socket = new WebSocketClass(url);
  } catch {
    finish(null);
    return;
  }
  socket.onopen = () => socket.send(JSON.stringify({ callEvent: 'CONSENT_SETTINGS', clientType: CLIENT_TYPE }));
  socket.onmessage = (event: MessageEvent) => {
    const frame = parseServerFrame(event.data);
    if (frame?.event === 'CONSENT_SETTINGS') finish(frame as ConsentSettings);
  };
  socket.onerror = () => finish(null);
  socket.onclose = () => finish(null);
});
