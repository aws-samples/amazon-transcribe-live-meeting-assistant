/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
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
      Sections
      Default
      Custom
    }
  }
`;

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

// The one profile every configuration page saves into.
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
    sections,
    defaults: parseObject(answer?.Default, {}),
    custom: parseObject(answer?.Custom, {}),
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

export const errorText = (err) => err?.errors?.[0]?.message || err?.message || String(err);
