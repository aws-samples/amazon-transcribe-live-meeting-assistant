/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  ExpandableSection,
  FormField,
  Header,
  Input,
  Modal,
  Select,
  SpaceBetween,
} from '@cloudscape-design/components';

import useSettingsContext from '../../contexts/settings';
import AdminOnly from '../common/AdminOnly';
import { isAvailable, readExport, storedSection } from './config-model';
import { compareProfiles } from './config-text';
import {
  DEFAULT_PROFILE,
  activateProfileMutation,
  createProfileMutation,
  deleteProfileMutation,
  errorText,
  getConfigurationQuery,
  invalidMessage,
  listProfilesQuery,
  parseConfiguration,
  parseProfiles,
  profileNameProblem,
  refusedMessage,
} from './configuration-api';
import ConfigurationEditor, { MAX_COMMENT_LENGTH, effectiveOf } from './ConfigurationEditor';
import ProfileComparisonModal from './ProfileComparisonModal';
import ProfilesTable, { profileText } from './ProfilesTable';
import RevisionHistoryModal from './RevisionHistoryModal';
import { HISTORY_PARAM } from '../../routes/moved-configuration-routes';

const client = generateClient();

const UNSAVED_PROMPT = 'Open another profile? Your unsaved changes will be lost.';

const sectionLabel = (section) => section.schema.sectionLabel || section.schema.title || section.name;

// The name a copy of a profile is offered.
const copyName = (source) => (source === DEFAULT_PROFILE ? 'custom' : `${source}-copy`);

const failureText = (err) => refusedMessage(err) || invalidMessage(err) || errorText(err);

const readProfile = async (name) => {
  const result = await client.graphql({ query: getConfigurationQuery, variables: { Profile: name } });
  return parseConfiguration(result.data?.getConfiguration);
};

/**
 * View / Edit Configuration: the configuration profiles, and below them the
 * editor for the one that is open. Opening a row opens that profile in the
 * editor; the table's actions work on whole profiles, the editor's on the
 * profile it shows. Which profile is open is kept in the address, so a link
 * can name one.
 */
export const ConfigurationWorkspaceContent = () => {
  const { settings } = useSettingsContext() || {};
  const location = useLocation();
  const navigate = useNavigate();
  const query = new URLSearchParams(location.search);

  const [profiles, setProfiles] = useState([]);
  const [profilesLoading, setProfilesLoading] = useState(true);
  const [profilesError, setProfilesError] = useState(null);
  // The profile open in the editor: the one the address names, else the active one.
  const [profile, setProfile] = useState(query.get('profile') || null);
  const [selected, setSelected] = useState([]);
  const [tableOpen, setTableOpen] = useState(true);
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);
  // Changed to have the editor read its profile again.
  const [reloadKey, setReloadKey] = useState(0);
  // Whether the editor holds unsaved changes; a ref for the prompt, state for what is shown.
  const dirty = useRef(false);
  const [editorDirty, setEditorDirty] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(query.get(HISTORY_PARAM) === '1');
  // One profile dialog at a time: { kind: 'create' | 'import' | 'delete', ... }.
  const [dialog, setDialog] = useState(null);
  const [dialogError, setDialogError] = useState(null);
  const [comparison, setComparison] = useState(null);
  const importInput = useRef(null);

  const loadProfiles = useCallback(async () => {
    setProfilesLoading(true);
    try {
      const result = await client.graphql({ query: listProfilesQuery });
      const listed = parseProfiles(result.data?.listConfigurationProfiles);
      setProfiles(listed);
      setProfilesError(null);
      setSelected((current) => current.filter((name) => listed.some((entry) => entry.name === name)));
      setProfile((current) => current || (listed.find((entry) => entry.active) || { name: DEFAULT_PROFILE }).name);
      return listed;
    } catch (err) {
      setProfilesError(`Could not load the configuration profiles: ${errorText(err)}`);
      return null;
    } finally {
      setProfilesLoading(false);
    }
  }, []);

  useEffect(() => {
    loadProfiles();
  }, [loadProfiles]);

  // The address names the profile open, so reloading the page or sharing the
  // link opens it again.
  useEffect(() => {
    if (!profile) return;
    const params = new URLSearchParams(location.search);
    if (params.get('profile') === profile) return;
    params.set('profile', profile);
    params.delete(HISTORY_PARAM);
    navigate({ search: `?${params.toString()}` }, { replace: true });
  }, [profile, location.search, navigate]);

  const onDirtyChange = useCallback((value) => {
    dirty.current = value;
    setEditorDirty(value);
  }, []);

  // Opening another profile discards unsaved edits, so it asks first, unless
  // the edits were just saved somewhere.
  const openProfile = (name, { ask = true } = {}) => {
    if (!name || name === profile) return true;
    // eslint-disable-next-line no-alert -- the browser's own prompt, as for leaving the page
    if (ask && dirty.current && !window.confirm(UNSAVED_PROMPT)) return false;
    dirty.current = false;
    setEditorDirty(false);
    setProfile(name);
    return true;
  };

  // A profile operation, then the list again. A failure is shown in the dialog
  // that asked for it, or on the page.
  const run = async (operation, success, showError = (text) => setStatus({ type: 'error', text })) => {
    setBusy(true);
    setDialogError(null);
    try {
      await operation();
      await loadProfiles();
      setStatus({ type: 'success', text: success });
      return true;
    } catch (err) {
      showError(failureText(err));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const activate = (name) =>
    run(
      () => client.graphql({ query: activateProfileMutation, variables: { input: { Name: name } } }),
      `The ${name} profile is now active. Every reader uses it as its cache allows; see each section.`,
    );

  const openCreate = (source) => {
    const from = source || (selected.length === 1 ? selected[0] : profile) || DEFAULT_PROFILE;
    setDialogError(null);
    setDialog({ kind: 'create', source: from, name: copyName(from), description: '' });
  };

  const create = async () => {
    const { name, source, description } = dialog;
    const made = await run(
      () =>
        client.graphql({
          query: createProfileMutation,
          variables: { input: { Name: name, CopyFrom: source, Description: description.trim() || null } },
        }),
      `Created the ${name} profile as a copy of ${source}. Activate it to have meetings use it.`,
      setDialogError,
    );
    if (made) {
      setDialog(null);
      openProfile(name);
    }
  };

  // Importing a file here makes a new profile of it, read as the editor reads
  // an import, so the file is checked before anything is created.
  const readImport = async (file) => {
    if (!file) return;
    try {
      const doc = await readProfile(profile || DEFAULT_PROFILE);
      const sections = doc.sections.filter((section) => isAvailable(section.schema, settings));
      const { values, skipped, withheld, source } = readExport(await file.text(), sections, doc.defaults, settings);
      const named = sections.filter((section) => values[section.name]);
      const body = Object.fromEntries(
        named.map((section) => [
          section.name,
          storedSection(section.schema, doc.defaults[section.name], values[section.name]),
        ]),
      );
      const notes = [
        skipped.length ? `Not used, because this deployment has no such section: ${skipped.join(', ')}.` : '',
        withheld.length
          ? `Not used, because this deployment does not offer them: ${withheld
              .map(([name, key]) => `${name} › ${key}`)
              .join(', ')}; they take their defaults.`
          : '',
      ].filter(Boolean);
      const suggested = typeof source.profile === 'string' ? source.profile : '';
      setDialogError(null);
      setDialog({
        kind: 'import',
        fileName: file.name,
        body,
        loadedText: named.map(sectionLabel).join(', ') || 'nothing',
        notes,
        name: profileNameProblem(suggested, profiles) ? '' : suggested,
        description: '',
      });
    } catch (err) {
      setStatus({ type: 'error', text: `Could not import ${file.name}: ${err.message || errorText(err)}` });
    }
  };

  const importProfile = async () => {
    const { name, body, fileName, description } = dialog;
    const made = await run(
      () =>
        client.graphql({
          query: createProfileMutation,
          variables: {
            input: {
              Name: name,
              Body: JSON.stringify(body),
              Description: description.trim() || null,
              Comment: `Imported from ${fileName}`.slice(0, MAX_COMMENT_LENGTH),
            },
          },
        }),
      `Created the ${name} profile from ${fileName}. Activate it to have meetings use it.`,
      setDialogError,
    );
    if (made) {
      setDialog(null);
      openProfile(name);
    }
  };

  const remove = async () => {
    const { names } = dialog;
    setBusy(true);
    setDialogError(null);
    const outcomes = await Promise.allSettled(
      names.map((name) => client.graphql({ query: deleteProfileMutation, variables: { input: { Name: name } } })),
    );
    const failed = outcomes
      .map((outcome, index) =>
        outcome.status === 'rejected' ? `${names[index]}: ${failureText(outcome.reason)}` : null,
      )
      .filter(Boolean);
    const gone = names.filter((name, index) => outcomes[index].status === 'fulfilled');
    const listed = await loadProfiles();
    setBusy(false);
    setDialog(null);
    if (gone.includes(profile)) {
      // Nothing is left to keep unsaved edits for.
      dirty.current = false;
      setProfile(((listed || []).find((entry) => entry.active) || { name: DEFAULT_PROFILE }).name);
    }
    const deletedText = gone.length
      ? `Deleted ${gone.join(', ')}. ${
          gone.length === 1 ? 'Its revisions stay' : 'Their revisions stay'
        } in the history.`
      : '';
    setStatus(
      failed.length
        ? {
            type: gone.length ? 'warning' : 'error',
            text: `${deletedText} Could not delete ${failed.join('; ')}`.trim(),
          }
        : { type: 'success', text: deletedText },
    );
  };

  // Compared as saved, as the export is: unsaved edits are in none of them.
  const compareSelected = async () => {
    const names = profiles.filter((entry) => selected.includes(entry.name)).map((entry) => entry.name);
    const note = editorDirty && names.includes(profile) ? `Your unsaved changes to ${profile} are not included.` : '';
    setComparison({ names, sections: [], rows: null, loading: true, error: null, note });
    try {
      const docs = await Promise.all(
        names.map(async (name) => {
          const doc = await readProfile(name);
          if (!doc.exists) throw new Error(`there is no profile named ${name}`);
          return doc;
        }),
      );
      const sections = docs[0].sections.filter((section) => isAvailable(section.schema, settings));
      setComparison((current) => ({
        ...current,
        sections,
        rows: compareProfiles(sections, docs.map(effectiveOf)),
        loading: false,
      }));
    } catch (err) {
      setComparison((current) => ({
        ...current,
        loading: false,
        error: `Could not read the profiles: ${errorText(err)}`,
      }));
    }
  };

  const closeHistory = () => {
    setHistoryOpen(false);
    const params = new URLSearchParams(location.search);
    if (params.has(HISTORY_PARAM)) {
      params.delete(HISTORY_PARAM);
      navigate({ search: `?${params.toString()}` }, { replace: true });
    }
  };

  const restored = () => {
    setReloadKey((key) => key + 1);
    loadProfiles();
  };

  const createProblem =
    dialog?.kind === 'create' || dialog?.kind === 'import' ? profileNameProblem(dialog.name, profiles) : null;
  const setDialogField = (field) => (value) => setDialog((current) => ({ ...current, [field]: value }));

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={
          'Every setting LMA reads at run time, in one place. The active profile is what every meeting uses. Open ' +
          'a profile from the list to view or edit it below.'
        }
      >
        View / Edit Configuration
      </Header>

      {status && (
        <Alert type={status.type} dismissible onDismiss={() => setStatus(null)}>
          {status.text}
        </Alert>
      )}

      {profilesError && (
        <Alert type="error" action={<Button onClick={loadProfiles}>Retry</Button>}>
          {profilesError}
        </Alert>
      )}

      <ExpandableSection
        variant="container"
        headingTagOverride="h2"
        headerText="Configuration Profiles"
        headerCounter={profilesLoading ? undefined : `(${profiles.length})`}
        headerDescription="Select profiles to compare, activate or delete them; open one by its name."
        expanded={tableOpen}
        onChange={({ detail }) => setTableOpen(detail.expanded)}
      >
        <ProfilesTable
          profiles={profiles}
          loading={profilesLoading}
          busy={busy}
          openProfile={profile}
          selected={selected}
          onSelectionChange={setSelected}
          onOpen={(name) => openProfile(name)}
          onCompare={compareSelected}
          onActivate={activate}
          onImport={() => importInput.current?.click()}
          onDelete={(names) => {
            setDialogError(null);
            setDialog({ kind: 'delete', names });
          }}
          onCreate={() => openCreate()}
          onRefresh={loadProfiles}
        />
      </ExpandableSection>

      {profile && (
        <ConfigurationEditor
          profile={profile}
          profiles={profiles}
          reloadKey={reloadKey}
          onDirtyChange={onDirtyChange}
          onOpenHistory={() => setHistoryOpen(true)}
          onCopy={() => openCreate(profile)}
          onActivate={() => activate(profile)}
          onProfileCreated={(name, text) => {
            loadProfiles();
            setStatus({ type: 'success', text });
            openProfile(name, { ask: false });
          }}
        />
      )}

      <input
        ref={importInput}
        type="file"
        accept="application/json,.json"
        style={{ display: 'none' }}
        aria-label="Configuration file to import as a new profile"
        data-testid="profile-import"
        onChange={(event) => {
          readImport(event.target.files?.[0]);
          // So choosing the same file again reads it again.
          // eslint-disable-next-line no-param-reassign -- resetting the picker
          event.target.value = '';
        }}
      />

      {historyOpen && profile && (
        <RevisionHistoryModal
          profile={profile}
          visible
          onDismiss={closeHistory}
          onRestored={restored}
          unsavedChanges={editorDirty}
        />
      )}

      {comparison && (
        <ProfileComparisonModal
          visible
          names={comparison.names}
          sections={comparison.sections}
          comparison={comparison.rows}
          loading={comparison.loading}
          error={comparison.error}
          note={comparison.note}
          onDismiss={() => setComparison(null)}
        />
      )}

      <Modal
        visible={dialog?.kind === 'create'}
        onDismiss={() => !busy && setDialog(null)}
        header="Create a profile"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDialog(null)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={create} loading={busy} disabled={Boolean(createProblem)}>
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {dialog?.kind === 'create' && (
          <SpaceBetween size="m">
            {dialogError && <Alert type="error">{dialogError}</Alert>}
            <Box>
              The new profile is a Custom copy of every setting of another profile, as it is saved, which you can change
              and save. It is not active until you activate it. To keep the editor&apos;s unsaved edits, use Save
              current edits as new profile in the editor&apos;s Actions instead.
            </Box>
            <FormField label="Copy from">
              <Select
                ariaLabel="Profile to copy"
                selectedOption={{ value: dialog.source, label: profileText({ name: dialog.source }) }}
                options={profiles.map((entry) => ({ value: entry.name, label: profileText(entry) }))}
                onChange={({ detail }) => {
                  const source = detail.selectedOption.value;
                  setDialog((current) => ({
                    ...current,
                    source,
                    // A name that was only the suggestion follows the source.
                    name: current.name === copyName(current.source) ? copyName(source) : current.name,
                  }));
                }}
              />
            </FormField>
            <FormField label="Name" errorText={dialog.name ? createProblem : null}>
              <Input
                value={dialog.name}
                ariaLabel="Profile name"
                onChange={({ detail }) => setDialogField('name')(detail.value)}
              />
            </FormField>
            <FormField label="Description" description="Optional.">
              <Input
                value={dialog.description}
                ariaLabel="Profile description"
                onChange={({ detail }) => setDialogField('description')(detail.value.slice(0, MAX_COMMENT_LENGTH))}
              />
            </FormField>
          </SpaceBetween>
        )}
      </Modal>

      <Modal
        visible={dialog?.kind === 'import'}
        onDismiss={() => !busy && setDialog(null)}
        header={dialog?.kind === 'import' ? `Import ${dialog.fileName} as a new profile` : ''}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDialog(null)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={importProfile} loading={busy} disabled={Boolean(createProblem)}>
                Import
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {dialog?.kind === 'import' && (
          <SpaceBetween size="m">
            {dialogError && <Alert type="error">{dialogError}</Alert>}
            <Box>
              {`The file sets ${dialog.loadedText}. The new profile is a Custom one with the file's settings over the ` +
                'deployment defaults, and is not active until you activate it. To load a file into the profile ' +
                "open in the editor instead, as unsaved changes, use Import from a file in the editor's Actions."}
            </Box>
            {dialog.notes.length > 0 && <Alert type="warning">{dialog.notes.join(' ')}</Alert>}
            <FormField label="Name" errorText={dialog.name ? createProblem : null}>
              <Input
                value={dialog.name}
                ariaLabel="Imported profile name"
                onChange={({ detail }) => setDialogField('name')(detail.value)}
              />
            </FormField>
            <FormField label="Description" description="Optional.">
              <Input
                value={dialog.description}
                ariaLabel="Imported profile description"
                onChange={({ detail }) => setDialogField('description')(detail.value.slice(0, MAX_COMMENT_LENGTH))}
              />
            </FormField>
          </SpaceBetween>
        )}
      </Modal>

      <Modal
        visible={dialog?.kind === 'delete'}
        onDismiss={() => !busy && setDialog(null)}
        header={
          dialog?.kind === 'delete'
            ? `Delete ${
                dialog.names.length === 1 ? `the ${dialog.names[0]} profile` : `${dialog.names.length} profiles`
              }?`
            : ''
        }
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDialog(null)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={remove} loading={busy}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {dialog?.kind === 'delete' && (
          <SpaceBetween size="m">
            {dialogError && <Alert type="error">{dialogError}</Alert>}
            {dialog.names.length > 1 && (
              <ul>
                {dialog.names.map((name) => (
                  <li key={name}>{name}</li>
                ))}
              </ul>
            )}
            <Box>
              {`${dialog.names.length === 1 ? 'The profile is' : 'The profiles are'} deleted. ` +
                `${dialog.names.length === 1 ? 'Its' : 'Their'} revisions stay in the history.` +
                `${
                  dialog.names.includes(profile) && editorDirty ? ' The unsaved changes in the editor are lost.' : ''
                }`}
            </Box>
          </SpaceBetween>
        )}
      </Modal>
    </SpaceBetween>
  );
};

// Every setting here applies to every meeting in the deployment, so the page is
// for Admin users; everyone else is told so instead of being shown a form whose
// save would be refused.
const ConfigurationWorkspace = () => (
  <AdminOnly feature="the configuration">
    <ConfigurationWorkspaceContent />
  </AdminOnly>
);

export default ConfigurationWorkspace;
