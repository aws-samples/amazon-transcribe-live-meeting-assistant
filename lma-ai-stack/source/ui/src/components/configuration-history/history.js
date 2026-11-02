/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// The GraphQL documents and the pure helpers behind the Configuration History
// page, kept apart from the component so they can be tested without rendering it.

export const listRevisionsQuery = `
  query ListConfigurationRevisions($Profile: String) {
    listConfigurationRevisions(Profile: $Profile) {
      Profile
      Version
      LatestRevision
      PublishedRevision
      Revisions {
        Revision
        CreatedAt
        CreatedBy
        Sections
        Comment
        Source
        RestoredFrom
        Label
        Version
        Published
      }
    }
  }
`;

export const getRevisionQuery = `
  query GetConfigurationRevision($Profile: String, $Revision: Int!) {
    getConfigurationRevision(Profile: $Profile, Revision: $Revision) {
      Revision
      CreatedAt
      CreatedBy
      Body
    }
  }
`;

export const diffRevisionsQuery = `
  query DiffConfigurationRevisions($Profile: String, $FromRevision: Int!, $ToRevision: Int!) {
    diffConfigurationRevisions(Profile: $Profile, FromRevision: $FromRevision, ToRevision: $ToRevision) {
      FromRevision
      ToRevision
      Changes
    }
  }
`;

export const restoreRevisionMutation = `
  mutation RestoreConfigurationRevision($input: RestoreConfigurationRevisionInput!) {
    restoreConfigurationRevision(input: $input) {
      Success
      Version
      Revision
      Unchanged
    }
  }
`;

export const labelRevisionMutation = `
  mutation LabelConfigurationRevision($input: LabelConfigurationRevisionInput!) {
    labelConfigurationRevision(input: $input) {
      Success
      Revision
    }
  }
`;

// The limit the store applies to a label; a labelled revision is never pruned.
export const MAX_LABEL_LENGTH = 100;

// The one profile every configuration page saves into.
export const PROFILE = 'custom';

// What each section of the profile is, in the words of the page that edits it.
// A section a later release adds and this map does not know is shown by its key.
const SECTION_LABELS = {
  summary_prompt_templates: 'Transcript summary prompts',
  nova_sonic_config: 'Nova Sonic',
  chat_buttons: 'Chat buttons',
  asr_engine: 'Transcription engine',
  call_pricing: 'Meeting cost prices',
  language_models: 'Amazon Bedrock models',
  transcription: 'Amazon Transcribe',
  recording: 'Meeting recording',
  meeting_assist: 'Meeting assistant',
  meeting_processing: 'Meeting processing',
  retention: 'Retention',
  meeting_consent: 'Meeting consent',
  meeting_agenda: 'Meeting agenda',
  virtual_participant: 'Virtual Participant',
};

export const sectionLabel = (section) => SECTION_LABELS[section] || section;

// Null `Sections` is "not known" -- a revision recorded after the fact -- which
// is not the same as an empty list, a revision that changed nothing it names.
export const sectionsText = (sections) => {
  if (sections === null || sections === undefined) return 'Not known';
  if (sections.length === 0) return '—';
  return sections.map(sectionLabel).join(', ');
};

export const sourceText = (entry) => {
  switch (entry?.Source) {
    case 'save':
      return 'Saved';
    case 'restore':
      return entry.RestoredFrom ? `Restored from revision ${entry.RestoredFrom}` : 'Restored';
    case 'deploy':
      return 'Deployment';
    case 'backfill':
      return 'As it stood when history began';
    case 'recovered':
      return 'Recorded from the saved configuration';
    default:
      return entry?.Source || '—';
  }
};

// Revision numbers in the order a comparison reads: the older one first.
export const comparisonPair = (selected) => {
  const numbers = (selected || []).map((entry) => entry.Revision).sort((a, b) => a - b);
  return numbers.length === 2 ? { FromRevision: numbers[0], ToRevision: numbers[1] } : null;
};

// The path of a changed value, for display. Keys are joined with a separator
// that cannot be mistaken for part of one: summary prompt keys hold '#'.
export const pathText = (path) => (path || []).map((key, index) => (index === 0 ? sectionLabel(key) : key)).join(' › ');

// A value from a diff, for display. A string is shown as it is; anything else
// as compact JSON, so that `true` and "true" are told apart.
export const valueText = (value) => {
  if (value === undefined) return '';
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
};

// The changes a diff answered with, or an empty list for an answer that holds none.
export const parseChanges = (raw) => {
  try {
    const parsed = JSON.parse(raw || '[]');
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
};
