/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The LMA roles in the web UI, as lma-ai-stack/source/appsync/role-matrix.json
 * defines them; roles.test.js holds this file to it.
 *
 * A caller's role comes from their Cognito groups: Admin when they include
 * Admin, otherwise Participant when they include Participant, otherwise User.
 * The user pool's pre token generation trigger puts exactly that role in every
 * token's cognito:groups claim; a token issued before the trigger existed
 * carries no group for a User, which the rule reads as User too.
 *
 * What the UI shows by role is presentation. The GraphQL API, the transcriber,
 * the identity pool and the MCP server are what refuse a role.
 */
import {
  BROWSER_EXTENSION_PATH,
  CALLS_PATH,
  CONFIGURATION_PATH,
  DESKTOP_CAPTURE_APP_PATH,
  EMBED_PATH,
  HOME_PATH,
  MCP_SERVERS_PATH,
  MEETINGS_QUERY_PATH,
  SKILLS_PATH,
  STREAM_AUDIO_PATH,
  TROUBLESHOOTING_PATH,
  UPLOAD_AUDIO_PATH,
  USER_MANAGEMENT_PATH,
  VIRTUAL_PARTICIPANT_PATH,
  VOICE_ENROLLMENT_PATH,
} from '../../routes/constants';

export const ADMIN = 'Admin';
export const USER = 'User';
export const PARTICIPANT = 'Participant';
export const ROLES = [ADMIN, USER, PARTICIPANT];

/** The caller's role from their Cognito group names; names compare exactly. */
export const effectiveRole = (groups = []) => {
  const list = Array.isArray(groups) ? groups : [groups];
  if (list.includes(ADMIN)) return ADMIN;
  if (list.includes(PARTICIPANT)) return PARTICIPANT;
  return USER;
};

/** Whether a token's groups already name a role, which a token from the trigger always does. */
export const groupsNameARole = (groups = []) => {
  const list = Array.isArray(groups) ? groups : [groups];
  return ROLES.some((role) => list.includes(role));
};

const MEMBERS = [ADMIN, USER];

/** The roles that may open each page, by its route (the matrix's uiRoutes). */
export const ROUTE_ROLES = {
  [HOME_PATH]: [PARTICIPANT],
  [CALLS_PATH]: MEMBERS,
  [MEETINGS_QUERY_PATH]: MEMBERS,
  [STREAM_AUDIO_PATH]: MEMBERS,
  [UPLOAD_AUDIO_PATH]: MEMBERS,
  [VIRTUAL_PARTICIPANT_PATH]: MEMBERS,
  [EMBED_PATH]: MEMBERS,
  [BROWSER_EXTENSION_PATH]: MEMBERS,
  [DESKTOP_CAPTURE_APP_PATH]: MEMBERS,
  [VOICE_ENROLLMENT_PATH]: MEMBERS,
  [MCP_SERVERS_PATH]: MEMBERS,
  [SKILLS_PATH]: MEMBERS,
  [CONFIGURATION_PATH]: [ADMIN],
  [USER_MANAGEMENT_PATH]: [ADMIN],
  [TROUBLESHOOTING_PATH]: [ADMIN],
};

/**
 * Whether a role may open a path. The most specific route that contains the
 * path decides; a path under no route is refused.
 */
export const roleMayOpen = (role, path) => {
  const route = Object.keys(ROUTE_ROLES)
    .filter((prefix) => path === prefix || path.startsWith(`${prefix}/`))
    .sort((a, b) => b.length - a.length)[0];
  return Boolean(route) && ROUTE_ROLES[route].includes(role);
};
