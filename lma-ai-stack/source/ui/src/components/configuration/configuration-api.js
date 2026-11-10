/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// The GraphQL documents behind the configuration editor, and turning their
// answers into what the editor works with.

export const getConfigurationQuery = `
  query GetConfiguration($Profile: String) {
    getConfiguration(Profile: $Profile) {
      Profile
      Exists
      Version
      UpdatedBy
      UpdatedAt
      PublishedRevision
      Managed
      Description
      Active
      FullCopy
      Sections
      Default
      Custom
      Presets
    }
  }
`;

export const listProfilesQuery = `
  query ListConfigurationProfiles {
    listConfigurationProfiles {
      Profiles {
        Name
        Managed
        Active
        Description
        Version
        UpdatedAt
        UpdatedBy
        Origin
        FullCopy
      }
    }
  }
`;

const PROFILE_OUTPUT = `
      Success
      Profile
      Version
      Revision
`;

export const createProfileMutation = `
  mutation CreateConfigurationProfile($input: CreateConfigurationProfileInput!) {
    createConfigurationProfile(input: $input) {${PROFILE_OUTPUT}}
  }
`;

export const deleteProfileMutation = `
  mutation DeleteConfigurationProfile($input: ConfigurationProfileNameInput!) {
    deleteConfigurationProfile(input: $input) {${PROFILE_OUTPUT}}
  }
`;

export const activateProfileMutation = `
  mutation ActivateConfigurationProfile($input: ConfigurationProfileNameInput!) {
    activateConfigurationProfile(input: $input) {${PROFILE_OUTPUT}}
  }
`;

// The deployment's default profile: read-only, rewritten by every stack update.
export const DEFAULT_PROFILE = 'default';

// What a profile may be named; the same rule the store applies.
export const PROFILE_NAME = /^[A-Za-z0-9._-]{1,64}$/;

/** Why a name cannot be used for a new profile, or null when it can. */
export const profileNameProblem = (name, profiles) => {
  if (!name) return 'Enter a name.';
  if (!PROFILE_NAME.test(name)) return 'Use at most 64 letters, digits, ".", "_" and "-".';
  const lowered = name.toLowerCase();
  if (lowered.startsWith('__') || ['default', '__active', 'schema'].includes(lowered)) {
    return 'That name is reserved.';
  }
  if ((profiles || []).some((profile) => profile.name.toLowerCase() === lowered)) {
    return 'A profile with that name already exists.';
  }
  return null;
};

/** The profiles as the page lists them: the default first, then by name. */
export const parseProfiles = (answer) =>
  (answer?.Profiles || [])
    .filter((entry) => entry && entry.Name)
    .map((entry) => ({
      name: entry.Name,
      managed: entry.Managed === true,
      active: entry.Active === true,
      description: entry.Description || '',
      updatedAt: entry.UpdatedAt || null,
      updatedBy: entry.UpdatedBy || null,
      origin: entry.Origin || null,
    }))
    .sort((a, b) => (a.name !== DEFAULT_PROFILE) - (b.name !== DEFAULT_PROFILE) || a.name.localeCompare(b.name));

export const updateConfigurationMutation = `
  mutation UpdateConfiguration($input: UpdateConfigurationInput!) {
    updateConfiguration(input: $input) {
      Success
      Profile
      Version
      Revision
      Unchanged
    }
  }
`;

// The profile an earlier release created on every deployment, and the one
// this page opens when it cannot tell which is active.
export const PROFILE = 'custom';

const parseObject = (raw, fallback) => {
  try {
    const parsed = JSON.parse(raw || '');
    return parsed && typeof parsed === 'object' ? parsed : fallback;
  } catch {
    return fallback;
  }
};

/**
 * The read, as the editor uses it: the sections in the order their schemas
 * give, each with its schema and cache tier, the shipped values, the stored
 * changes, and the version a save sends back.
 */
export const parseConfiguration = (answer) => {
  const sections = parseObject(answer?.Sections, [])
    .filter((entry) => entry && entry.Name && entry.Schema)
    .map((entry) => ({ name: entry.Name, tier: entry.CacheTier, schema: entry.Schema }))
    .sort((a, b) => (a.schema.order ?? 1000) - (b.schema.order ?? 1000) || a.name.localeCompare(b.name));
  return {
    profile: answer?.Profile || PROFILE,
    exists: answer?.Exists !== false,
    version: typeof answer?.Version === 'number' ? answer.Version : 0,
    updatedBy: answer?.UpdatedBy || null,
    updatedAt: answer?.UpdatedAt || null,
    publishedRevision: answer?.PublishedRevision ?? null,
    // The default and Managed profiles are read-only: copy one to change it.
    managed: answer?.Managed === true,
    active: answer?.Active === true,
    description: answer?.Description || '',
    sections,
    defaults: parseObject(answer?.Default, {}),
    custom: parseObject(answer?.Custom, {}),
    presets: parseObject(answer?.Presets, []).filter(
      (preset) => preset && preset.Id && preset.Sections && typeof preset.Sections === 'object',
    ),
  };
};

// A refused save says why after a fixed prefix: "Conflict: " when the profile
// was saved by someone else since it was loaded, "Invalid: " when a value does
// not match its section's schema. Matched on the prefix, as the other
// configuration pages match "Conflict: ".
const messagesOf = (err) => [...(err?.errors ?? []).map((entry) => entry?.message ?? ''), err?.message ?? ''];

export const invalidMessage = (err) => {
  const message = messagesOf(err).find((text) => text.startsWith('Invalid: '));
  return message ? message.slice('Invalid: '.length) : null;
};

// Refusals of a profile operation ("Refused: "): a name in use, the active
// profile, or a profile the deployment manages.
export const refusedMessage = (err) => {
  const message = messagesOf(err).find((text) => text.startsWith('Refused: '));
  return message ? message.slice('Refused: '.length) : null;
};

export const errorText = (err) => err?.errors?.[0]?.message || err?.message || String(err);
