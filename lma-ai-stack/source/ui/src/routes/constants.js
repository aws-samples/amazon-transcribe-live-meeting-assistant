/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
export const LOGIN_PATH = '/login';
export const LOGOUT_PATH = '/logout';
export const CALLS_PATH = '/calls';
export const STREAM_AUDIO_PATH = '/stream';
export const UPLOAD_AUDIO_PATH = '/upload';
export const VIRTUAL_PARTICIPANT_PATH = '/virtual-participant';
export const BROWSER_EXTENSION_PATH = '/browser-extension';
export const DESKTOP_CAPTURE_APP_PATH = '/desktop-capture-app';
export const MCP_SERVERS_PATH = '/configuration/mcp-servers';
// The pages that each edited one section of the configuration. Each now
// redirects to its section of the Configuration page, so bookmarks keep working.
export const NOVA_SONIC_CONFIG_PATH = '/configuration/nova-sonic';
export const ASR_CONFIG_PATH = '/configuration/asr';
export const TRANSCRIPT_SUMMARY_PATH = '/configuration/transcript-summary';
export const USER_MANAGEMENT_PATH = '/configuration/user-management';
export const CONFIGURATION_HISTORY_PATH = '/configuration/history';
// The schema-driven editor for every section of the runtime configuration.
export const CONFIGURATION_PATH = '/configuration/settings';
// Each earlier single-section configuration page, and the section of the
// Configuration page it now opens.
export const MOVED_CONFIGURATION_PAGES = {
  [NOVA_SONIC_CONFIG_PATH]: 'nova_sonic_config',
  [ASR_CONFIG_PATH]: 'asr_engine',
  [TRANSCRIPT_SUMMARY_PATH]: 'summary_prompt_templates',
};
export const DEFAULT_PATH = CALLS_PATH;
export const EMBED_PATH = '/embed';
// The enrollment portal. Top-level rather than under /configuration, because
// enrolling your own voice is something every user does for themselves, not an
// administrative task -- only the person whose voice it is can consent to it
// being stored.
export const VOICE_ENROLLMENT_PATH = '/voice-enrollment';
export const REDIRECT_URL_PARAM = 'redirect';
export const MEETINGS_QUERY_PATH = `${CALLS_PATH}/query`;
