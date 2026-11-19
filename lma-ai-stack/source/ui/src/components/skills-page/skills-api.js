/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// The meeting assistant's skills: the GraphQL operations the Skills page calls,
// and the file handling around them. A skill is a name, a description, Markdown
// instructions and text reference files. Importing one file (a SKILL.md, an
// Amazon Quick skill .md or a .zip of a skill folder) is parsed by the stack,
// which leaves out what a skill does not hold and says so; the library is a
// JSON document of kind lma-skill-library, version 1, imported by name.

export const LIBRARY_KIND = 'lma-skill-library';
export const LIBRARY_VERSION = 1;
// The largest library document sent in one import request; a larger one is sent
// in parts of whole skills, which an import by name makes the same as sending it
// whole. Matches MAX_IMPORT_BYTES in the SDK.
export const MAX_IMPORT_BYTES = 512 * 1024;
// The largest reference file the stack keeps, in bytes of UTF-8 (MAX_FILE_BYTES
// in skill_formats.py), so the editor can say so before a save is refused.
export const MAX_FILE_BYTES = 32 * 1024;
export const NAME_PATTERN = /^[a-z0-9]+(-[a-z0-9]+)*$/;

const SKILL_FIELDS = `
  SkillId
  Name
  DisplayName
  Description
  Instructions
  Files {
    Path
    Content
    Size
  }
  Metadata
  SourceFormat
  CreatedAt
  CreatedBy
  UpdatedAt
  UpdatedBy
`;

export const listSkillsQuery = `
  query ListSkills {
    listSkills {
      SkillId
      Name
      DisplayName
      Description
      Files {
        Path
        Size
      }
      Metadata
      SourceFormat
      UpdatedAt
      UpdatedBy
    }
  }
`;

export const getSkillQuery = `
  query GetSkill($skillId: ID!) {
    getSkill(skillId: $skillId) {
      ${SKILL_FIELDS}
    }
  }
`;

export const exportSkillQuery = `
  query ExportSkill($skillId: ID!, $format: String) {
    exportSkill(skillId: $skillId, format: $format)
  }
`;

export const exportSkillLibraryQuery = `
  query ExportSkillLibrary {
    exportSkillLibrary
  }
`;

export const createSkillMutation = `
  mutation CreateSkill($input: SkillInput!) {
    createSkill(input: $input) {
      SkillId
      Success
      Message
    }
  }
`;

export const updateSkillMutation = `
  mutation UpdateSkill($skillId: ID!, $input: SkillInput!) {
    updateSkill(skillId: $skillId, input: $input) {
      SkillId
      Success
      Message
    }
  }
`;

export const deleteSkillMutation = `
  mutation DeleteSkill($skillId: ID!) {
    deleteSkill(skillId: $skillId) {
      SkillId
      Success
      Message
    }
  }
`;

export const importSkillMutation = `
  mutation ImportSkill($fileName: String!, $content: String!, $encoding: String, $overwrite: Boolean) {
    importSkill(fileName: $fileName, content: $content, encoding: $encoding, overwrite: $overwrite) {
      Success
      Message
      SkillId
      Name
      Action
      Skipped {
        Path
        Reason
      }
      Notes
    }
  }
`;

export const importSkillLibraryMutation = `
  mutation ImportSkillLibrary($library: AWSJSON!) {
    importSkillLibrary(library: $library) {
      Success
      Message
      Skills {
        Name
        SkillId
        Action
        Message
      }
    }
  }
`;

/** The UTF-8 size of a text. */
export const byteSize = (text) => new TextEncoder().encode(text || '').length;

/** The metadata a skill carries (AWSJSON text or an object), as an object. */
export const skillMetadata = (raw) => {
  if (!raw) return {};
  if (typeof raw === 'object') return raw;
  try {
    const value = JSON.parse(raw);
    return value && typeof value === 'object' ? value : {};
  } catch (err) {
    return {};
  }
};

/** The editor's form as the SkillInput the stack takes. */
export const skillInput = (form) => ({
  Name: (form.name || '').trim(),
  DisplayName: (form.displayName || '').trim() || null,
  Description: (form.description || '').trim(),
  Instructions: form.instructions || '',
  Files: (form.files || []).map((file) => ({ Path: (file.path || '').trim(), Content: file.content || '' })),
  Metadata: JSON.stringify(form.metadata || {}),
});

/** A stored skill as the editor's form. */
export const skillForm = (skill) => ({
  name: skill?.Name || '',
  displayName: skill?.DisplayName || '',
  description: skill?.Description || '',
  instructions: skill?.Instructions || '',
  files: (skill?.Files || []).map((file) => ({ path: file.Path, content: file.Content || '' })),
  metadata: skillMetadata(skill?.Metadata),
});

/** What is wrong with the editor's form before it is sent, or [] when nothing is. */
export const formProblems = (form) => {
  const problems = [];
  if (!NAME_PATTERN.test(form.name || '') || form.name.length > 64) {
    problems.push('The name must be lowercase letters, digits and single hyphens between them, up to 64 characters.');
  }
  if (!(form.description || '').trim()) problems.push('A description is required.');
  if (!(form.instructions || '').trim()) problems.push('Instructions are required.');
  const paths = (form.files || []).map((file) => (file.path || '').trim());
  if (paths.some((path) => !path)) problems.push('Every reference file needs a path.');
  if (new Set(paths).size !== paths.length) problems.push('Two reference files have the same path.');
  (form.files || []).forEach((file) => {
    if (byteSize(file.content) > MAX_FILE_BYTES) {
      problems.push(`${file.path || 'A reference file'} is larger than ${MAX_FILE_BYTES / 1024} KB.`);
    }
  });
  return problems;
};

/** Base64 of an ArrayBuffer, in chunks so a large file does not overflow the call stack. */
export const toBase64 = (buffer) => {
  const bytes = new Uint8Array(buffer);
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
};

/** The variables of an importSkill call for a chosen file: a zip in base64, Markdown as text. */
export const importVariables = async (file, overwrite = false) => {
  const isZip = /\.zip$/i.test(file.name);
  return isZip
    ? { fileName: file.name, content: toBase64(await file.arrayBuffer()), encoding: 'base64', overwrite }
    : { fileName: file.name, content: await file.text(), encoding: 'utf-8', overwrite };
};

/** An exportSkill answer as a Blob to download, and its file name. */
export const exportedFile = (raw) => {
  const answer = typeof raw === 'string' ? JSON.parse(raw) : raw;
  if (!answer || typeof answer.content !== 'string') throw new Error('The stack did not return a skill file.');
  if (answer.encoding === 'base64') {
    const binary = atob(answer.content);
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    return { name: answer.fileName, blob: new Blob([bytes], { type: 'application/zip' }) };
  }
  return { name: answer.fileName, blob: new Blob([answer.content], { type: 'text/markdown' }) };
};

/** The export's AWSJSON answer as a library document. */
export const exportedLibrary = (raw) => {
  const document = typeof raw === 'string' ? JSON.parse(raw) : raw;
  if (!document || document.kind !== LIBRARY_KIND) throw new Error('The stack did not return a skill library.');
  return document;
};

/** The text of a library file, checked, as a document to import. */
export const parseLibraryFile = (text) => {
  let document;
  try {
    document = JSON.parse(text);
  } catch (err) {
    throw new Error(`The file is not valid JSON: ${err.message}`);
  }
  if (!document || typeof document !== 'object' || document.kind !== LIBRARY_KIND) {
    throw new Error('The file is not an exported skill library.');
  }
  if (document.version !== LIBRARY_VERSION) {
    throw new Error(
      `The library is format version ${document.version}; this release reads version ${LIBRARY_VERSION}.`,
    );
  }
  if (!Array.isArray(document.skills)) throw new Error('The library has no list of skills.');
  return document;
};

/**
 * A library split into libraries of whole skills, each at most `maxBytes` of JSON,
 * and the names left out because an earlier entry has them. The stack skips a
 * name listed twice in one document; across parts it would update instead, so
 * the copies are removed here, before the split.
 */
export const libraryParts = (document, maxBytes = MAX_IMPORT_BYTES) => {
  const { skills: entries, ...header } = document;
  const seen = new Set();
  const duplicates = [];
  const skills = entries.filter((skill) => {
    const name = skill && typeof skill === 'object' ? skill.name : undefined;
    if (typeof name !== 'string') return true;
    if (seen.has(name)) {
      duplicates.push(name);
      return false;
    }
    seen.add(name);
    return true;
  });
  const empty = byteSize(JSON.stringify({ ...header, skills: [] }));
  const parts = [];
  let current = [];
  let size = empty;
  skills.forEach((skill) => {
    const entry = byteSize(JSON.stringify(skill)) + 1;
    if (current.length && size + entry > maxBytes) {
      parts.push({ ...header, skills: current });
      current = [];
      size = empty;
    }
    current.push(skill);
    size += entry;
  });
  parts.push({ ...header, skills: current });
  return { parts, duplicates };
};

/** The file name a library export is saved under. */
export const libraryFileName = (stackName = '', now = new Date()) =>
  `${stackName ? `${stackName}-` : ''}skills-${now.toISOString().slice(0, 10)}.json`;
