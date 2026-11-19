/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// The Skills page: the meeting assistant's skills library. Every signed-in user
// can read it, as the MCP Servers page's server list; creating, editing,
// deleting and importing skills, and exporting the whole library, are for the
// Admin group, which the stack enforces as well. Which skills the assistant
// uses is set per skill on the Configuration page (Meeting Assistant › Tool
// selection), not here.

import React, { useCallback, useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Alert,
  Box,
  Button,
  ButtonDropdown,
  Checkbox,
  Header,
  Link,
  Modal,
  SpaceBetween,
  Table,
} from '@cloudscape-design/components';

import useUserGroups from '../../hooks/use-user-groups';
import useSettingsContext from '../../contexts/settings';
import { CONFIGURATION_PATH } from '../../routes/constants';
import SkillEditorModal from './SkillEditorModal';
import {
  createSkillMutation,
  deleteSkillMutation,
  exportedFile,
  exportedLibrary,
  exportSkillLibraryQuery,
  exportSkillQuery,
  getSkillQuery,
  importSkillLibraryMutation,
  importSkillMutation,
  importVariables,
  libraryFileName,
  libraryParts,
  listSkillsQuery,
  parseLibraryFile,
  skillForm,
  skillInput,
  skillMetadata,
  updateSkillMutation,
} from './skills-api';

const client = generateClient();
const logger = new ConsoleLogger('SkillsPage');

const download = (blob, name) => {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  // Revoked a moment later: some browsers start the download asynchronously.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

const errorText = (err, fallback) => err?.message || err?.errors?.[0]?.message || fallback;

const SOURCE_LABELS = {
  manual: 'Written here',
  'agent-skills': 'SKILL.md',
  'amazon-quick': 'Amazon Quick',
  library: 'Skill library',
};

export const SkillImportResult = ({ result }) => (
  <Alert
    type={result.Success ? 'success' : 'error'}
    header={result.Success ? result.Message : 'The skill was not imported'}
  >
    <SpaceBetween size="xs">
      {!result.Success && <Box>{result.Message}</Box>}
      {(result.Skipped || []).length > 0 && (
        <Box>
          Left out, because a skill holds only instructions and text reference files:
          <ul style={{ margin: 0 }}>
            {result.Skipped.map((skipped) => (
              <li key={skipped.Path}>{`${skipped.Path}: ${skipped.Reason}`}</li>
            ))}
          </ul>
        </Box>
      )}
      {(result.Notes || []).map((note) => (
        <Box key={note}>{note}</Box>
      ))}
    </SpaceBetween>
  </Alert>
);

SkillImportResult.propTypes = {
  result: PropTypes.shape({
    Success: PropTypes.bool,
    Message: PropTypes.string,
    Skipped: PropTypes.arrayOf(PropTypes.shape({ Path: PropTypes.string, Reason: PropTypes.string })),
    Notes: PropTypes.arrayOf(PropTypes.string),
  }).isRequired,
};

export const LibraryImportResult = ({ result }) => (
  <Alert
    type={result.Success ? 'success' : 'error'}
    header={result.Success ? 'Skill library imported' : 'The import did not complete'}
  >
    <SpaceBetween size="xs">
      {result.Message && <Box>{result.Message}</Box>}
      <ul style={{ margin: 0 }}>
        {(result.Skills || []).map((skill) => (
          <li key={`${skill.Name}-${skill.SkillId}`}>
            {`${skill.Name}: ${skill.Action}`}
            {skill.Message ? ` (${skill.Message})` : ''}
          </li>
        ))}
      </ul>
    </SpaceBetween>
  </Alert>
);

LibraryImportResult.propTypes = {
  result: PropTypes.shape({
    Success: PropTypes.bool,
    Message: PropTypes.string,
    Skills: PropTypes.arrayOf(PropTypes.shape({})),
  }).isRequired,
};

const SkillsPage = () => {
  const { isAdmin } = useUserGroups();
  const { settings } = useSettingsContext() || {};
  const [skills, setSkills] = useState(null);
  const [selected, setSelected] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [editor, setEditor] = useState(null); // { skillId, form } while the editor is open
  const [editorError, setEditorError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(null);
  const [replace, setReplace] = useState(false);
  const skillFile = useRef(null);
  const libraryFile = useRef(null);

  const load = useCallback(async () => {
    try {
      const response = await client.graphql({ query: listSkillsQuery });
      setSkills(response.data.listSkills || []);
    } catch (err) {
      logger.error('Could not list the skills', err);
      setSkills([]);
      setError(errorText(err, 'Could not list the skills'));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const run = async (label, action) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (err) {
      logger.error(`${label} failed`, err);
      setError(errorText(err, `${label} failed`));
    } finally {
      setBusy(false);
    }
  };

  const openEditor = async (skill) => {
    setEditorError(null);
    if (!skill) {
      setEditor({ skillId: null, form: null });
      return;
    }
    await run('Opening the skill', async () => {
      const response = await client.graphql({ query: getSkillQuery, variables: { skillId: skill.SkillId } });
      setEditor({ skillId: skill.SkillId, form: skillForm(response.data.getSkill) });
    });
  };

  const save = async (form) => {
    setSaving(true);
    setEditorError(null);
    try {
      const input = skillInput(form);
      const response = editor.skillId
        ? await client.graphql({ query: updateSkillMutation, variables: { skillId: editor.skillId, input } })
        : await client.graphql({ query: createSkillMutation, variables: { input } });
      const answer = response.data[editor.skillId ? 'updateSkill' : 'createSkill'];
      if (!answer?.Success) {
        setEditorError(answer?.Message || 'The skill was not saved');
        return;
      }
      setEditor(null);
      await load();
    } catch (err) {
      logger.error('Saving the skill failed', err);
      setEditorError(errorText(err, 'The skill was not saved'));
    } finally {
      setSaving(false);
    }
  };

  const remove = () =>
    run('Deleting the skill', async () => {
      const response = await client.graphql({ query: deleteSkillMutation, variables: { skillId: deleting.SkillId } });
      if (!response.data.deleteSkill?.Success) throw new Error(response.data.deleteSkill?.Message);
      setDeleting(null);
      setSelected([]);
      await load();
    });

  const exportSkill = (skill, format) =>
    run('Exporting the skill', async () => {
      const response = await client.graphql({ query: exportSkillQuery, variables: { skillId: skill.SkillId, format } });
      const file = exportedFile(response.data.exportSkill);
      download(file.blob, file.name);
    });

  const importSkill = (file) =>
    run('Importing the skill', async () => {
      if (!file) return;
      setResult(null);
      try {
        const response = await client.graphql({
          query: importSkillMutation,
          variables: await importVariables(file, replace),
        });
        setResult({ kind: 'skill', value: response.data.importSkill });
        await load();
      } finally {
        if (skillFile.current) skillFile.current.value = '';
      }
    });

  const exportLibrary = () =>
    run('Exporting the skill library', async () => {
      const response = await client.graphql({ query: exportSkillLibraryQuery });
      const document = exportedLibrary(response.data.exportSkillLibrary);
      download(
        new Blob([`${JSON.stringify(document, null, 2)}\n`], { type: 'application/json' }),
        libraryFileName(settings?.StackName),
      );
    });

  const importLibrary = (file) =>
    run('Importing the skill library', async () => {
      if (!file) return;
      setResult(null);
      try {
        const document = parseLibraryFile(await file.text());
        const combined = { Success: true, Skills: [] };
        // Sent in parts of whole skills when large; matched by name, so the
        // parts together do what the whole would.
        // eslint-disable-next-line no-restricted-syntax
        for (const part of libraryParts(document)) {
          // eslint-disable-next-line no-await-in-loop
          const response = await client.graphql({
            query: importSkillLibraryMutation,
            variables: { library: JSON.stringify(part) },
          });
          const answer = response.data.importSkillLibrary || {};
          combined.Success = combined.Success && Boolean(answer.Success);
          combined.Skills.push(...(answer.Skills || []));
          if (!answer.Success && !(answer.Skills || []).length) combined.Message = answer.Message;
        }
        if (!combined.Message) {
          const done = combined.Skills.filter((s) => s.Action !== 'failed').length;
          combined.Message = `${done} of ${combined.Skills.length} skills processed`;
        }
        setResult({ kind: 'library', value: combined });
        await load();
      } finally {
        if (libraryFile.current) libraryFile.current.value = '';
      }
    });

  const current = selected[0];

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError(null)} header="Skills">
          {error}
        </Alert>
      )}
      {result?.kind === 'skill' && <SkillImportResult result={result.value} />}
      {result?.kind === 'library' && <LibraryImportResult result={result.value} />}
      {!isAdmin && <Alert type="info">Skills are managed by administrators. You can read and export them here.</Alert>}
      <Table
        loading={skills === null}
        loadingText="Loading skills"
        items={skills || []}
        trackBy="SkillId"
        selectionType="single"
        selectedItems={selected}
        onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
        variant="full-page"
        header={
          <Header
            variant="awsui-h1-sticky"
            counter={skills ? `(${skills.length})` : undefined}
            description={
              <>
                Guidance for the meeting assistant, loaded only when a request matches a skill&apos;s description.
                Choose which skills it uses on the{' '}
                <Link href={`#${CONFIGURATION_PATH}?section=meeting_assist`}>Configuration page</Link>.
              </>
            }
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <ButtonDropdown
                  disabled={!current || busy}
                  items={[
                    { id: 'skill-md', text: 'Export as SKILL.md' },
                    { id: 'zip', text: 'Export as .zip' },
                  ]}
                  onItemClick={({ detail }) => exportSkill(current, detail.id)}
                >
                  Export skill
                </ButtonDropdown>
                {isAdmin && (
                  <>
                    <Button disabled={!current || busy} onClick={() => setDeleting(current)}>
                      Delete
                    </Button>
                    <Button disabled={!current || busy} onClick={() => openEditor(current)}>
                      Edit
                    </Button>
                    <ButtonDropdown
                      disabled={busy}
                      items={[
                        { id: 'import-skill', text: 'Import a skill (SKILL.md, Quick .md or .zip)' },
                        { id: 'import-library', text: 'Import a skill library (.json)' },
                        { id: 'export-library', text: 'Export the skill library' },
                      ]}
                      onItemClick={({ detail }) => {
                        if (detail.id === 'import-skill') skillFile.current?.click();
                        if (detail.id === 'import-library') libraryFile.current?.click();
                        if (detail.id === 'export-library') exportLibrary();
                      }}
                    >
                      Import / export
                    </ButtonDropdown>
                    <Button variant="primary" disabled={busy} onClick={() => openEditor(null)}>
                      Create skill
                    </Button>
                  </>
                )}
              </SpaceBetween>
            }
          >
            Skills
          </Header>
        }
        filter={
          isAdmin ? (
            <Checkbox checked={replace} onChange={({ detail }) => setReplace(detail.checked)}>
              When importing a skill, replace a skill of the same name
            </Checkbox>
          ) : undefined
        }
        empty={<Box color="text-status-inactive">No skills yet.</Box>}
        columnDefinitions={[
          {
            id: 'name',
            header: 'Name',
            cell: (skill) => (skill.DisplayName ? `${skill.DisplayName} (${skill.Name})` : skill.Name),
          },
          { id: 'description', header: 'Description', cell: (skill) => skill.Description },
          { id: 'files', header: 'Reference files', cell: (skill) => (skill.Files || []).length },
          {
            id: 'trigger',
            header: 'Trigger',
            cell: (skill) => skillMetadata(skill.Metadata).trigger || '',
          },
          { id: 'source', header: 'Source', cell: (skill) => SOURCE_LABELS[skill.SourceFormat] || skill.SourceFormat },
          {
            id: 'updated',
            header: 'Updated',
            cell: (skill) => [skill.UpdatedAt, skill.UpdatedBy].filter(Boolean).join(' by '),
          },
        ]}
      />
      <input
        ref={skillFile}
        type="file"
        accept=".md,.markdown,.zip,text/markdown,application/zip"
        style={{ display: 'none' }}
        data-testid="skill-file"
        onChange={(event) => importSkill(event.target.files?.[0])}
      />
      <input
        ref={libraryFile}
        type="file"
        accept="application/json,.json"
        style={{ display: 'none' }}
        data-testid="skill-library-file"
        onChange={(event) => importLibrary(event.target.files?.[0])}
      />
      <SkillEditorModal
        visible={editor !== null}
        initial={editor?.form || null}
        saving={saving}
        error={editorError}
        onCancel={() => setEditor(null)}
        onSave={save}
      />
      <Modal
        visible={deleting !== null}
        onDismiss={() => setDeleting(null)}
        header="Delete skill"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleting(null)}>
                Cancel
              </Button>
              <Button variant="primary" loading={busy} onClick={remove}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {deleting &&
          `Delete the skill ${deleting.Name}? The meeting assistant stops using it. A profile that names it keeps ` +
            'its entry, which then has no effect.'}
      </Modal>
    </SpaceBetween>
  );
};

export default SkillsPage;
