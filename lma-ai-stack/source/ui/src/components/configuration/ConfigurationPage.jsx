/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import {
  Alert,
  Box,
  Button,
  ButtonDropdown,
  FormField,
  Header,
  Input,
  Modal,
  Select,
  SpaceBetween,
  Spinner,
  Textarea,
} from '@cloudscape-design/components';

import useSettingsContext from '../../contexts/settings';
import { CONFIGURATION_HISTORY_PATH } from '../../routes/constants';
import AdminOnly from '../common/AdminOnly';
import { isConflict } from '../common/config-conflict';
import { MAX_LABEL_LENGTH, labelRevisionMutation } from '../configuration-history/history';
import {
  appliesText,
  applyPresetSection,
  changedCount,
  deepEqual,
  effectiveSection,
  exportDocument,
  isAvailable,
  isPresetOffered,
  readExport,
  rebaseSection,
  storedSection,
} from './config-model';
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
  updateConfigurationMutation,
} from './configuration-api';
import ConfigurationSection, { sectionElementId } from './ConfigurationSection';
import { UNAVAILABLE_REASONS } from './section-notices';
import { checkDetailed, formatPath, limitProblems } from './schema-validation';
import './configuration.css';

const client = generateClient();

// The limit the store applies to a revision comment.
export const MAX_COMMENT_LENGTH = 500;

const sectionLabel = (section) => section.schema.sectionLabel || section.schema.title || section.name;

const effectiveOf = (doc) =>
  Object.fromEntries(
    doc.sections.map((section) => [
      section.name,
      effectiveSection(section.schema, doc.defaults[section.name], doc.custom[section.name]),
    ]),
  );

// What a profile is called in the selector.
const profileText = (entry) => (entry.name === DEFAULT_PROFILE ? 'default (deployment default)' : entry.name);

const profileTags = (entry) => [entry.active ? 'Active' : null, entry.managed ? 'Managed' : 'Custom'].filter(Boolean);

const whenText = (iso) => {
  const parsed = iso ? new Date(iso) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString() : iso || '';
};

export const ConfigurationContent = () => {
  const { settings } = useSettingsContext() || {};
  const location = useLocation();
  const query = new URLSearchParams(location.search);
  const focus = query.get('section');
  const requestedProfile = query.get('profile');

  const [doc, setDoc] = useState(null);
  // Each section's values as loaded, and as edited since.
  const [loaded, setLoaded] = useState({});
  const [edits, setEdits] = useState({});
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null);
  const [conflict, setConflict] = useState(false);
  const [saveOpen, setSaveOpen] = useState(false);
  const [comment, setComment] = useState('');
  const [label, setLabel] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [preset, setPreset] = useState(null);
  // The profiles, and the one this page shows: the active one unless a link
  // names another.
  const [profiles, setProfiles] = useState([]);
  const [profile, setProfile] = useState(requestedProfile || null);
  const [copyOpen, setCopyOpen] = useState(false);
  const [copyName, setCopyName] = useState('');
  const [copyDescription, setCopyDescription] = useState('');
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState(null);
  const scrolled = useRef(false);
  const fileInput = useRef(null);

  const loadProfiles = useCallback(async () => {
    const result = await client.graphql({ query: listProfilesQuery });
    const listed = parseProfiles(result.data?.listConfigurationProfiles);
    setProfiles(listed);
    return listed;
  }, []);

  const read = useCallback(
    async (name) => {
      const result = await client.graphql({ query: getConfigurationQuery, variables: { Profile: name || profile } });
      return parseConfiguration(result.data?.getConfiguration);
    },
    [profile],
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      let name = profile;
      if (!name) {
        const listed = await loadProfiles();
        name = (listed.find((entry) => entry.active) || { name: DEFAULT_PROFILE }).name;
        setProfile(name);
      } else {
        loadProfiles().catch(() => null);
      }
      const parsed = await read(name);
      const effective = effectiveOf(parsed);
      setDoc(parsed);
      setLoaded(effective);
      setEdits(effective);
      setConflict(false);
      setStatus((previous) => (previous?.type === 'error' ? null : previous));
    } catch (err) {
      setStatus({ type: 'error', text: `Could not load the configuration: ${errorText(err)}` });
    } finally {
      setLoading(false);
    }
  }, [read, profile, loadProfiles]);

  useEffect(() => {
    load();
  }, [load]);

  const sections = useMemo(
    () => (doc?.sections || []).filter((section) => isAvailable(section.schema, settings)),
    [doc, settings],
  );
  const changed = sections.filter((section) => !deepEqual(edits[section.name], loaded[section.name]));
  const dirty = changed.length > 0;

  // What a save would store for each changed section, and what the validator
  // finds wrong with it: the same check the server makes. Cheap enough to
  // compute on every render.
  const stored = Object.fromEntries(
    changed.map((section) => [
      section.name,
      storedSection(section.schema, doc.defaults[section.name], edits[section.name]),
    ]),
  );
  // The schema's own checks, then the limits the deployment's defaults set
  // (maximumIsDefault), which the server applies too.
  const problems = Object.fromEntries(
    changed.map((section) => [
      section.name,
      [
        ...checkDetailed(section.schema, stored[section.name]),
        ...limitProblems(section.schema, stored[section.name], doc.defaults[section.name]),
      ],
    ]),
  );
  const problemCount = Object.values(problems).reduce((total, list) => total + list.length, 0);

  const selected = profiles.find((entry) => entry.name === profile);

  // Choosing another profile discards unsaved edits, so it asks first.
  const chooseProfile = (name) => {
    // eslint-disable-next-line no-alert -- the browser's own prompt, as for leaving the page
    if (dirty && !window.confirm('Open another profile? Your unsaved changes will be lost.')) return;
    setStatus(null);
    setProfile(name);
  };

  const runProfileOperation = async (operation, success) => {
    setProfileBusy(true);
    setProfileError(null);
    try {
      await operation();
      await loadProfiles();
      setStatus({ type: 'success', text: success });
      return true;
    } catch (err) {
      setProfileError(refusedMessage(err) || invalidMessage(err) || errorText(err));
      return false;
    } finally {
      setProfileBusy(false);
    }
  };

  const activate = () =>
    runProfileOperation(
      () => client.graphql({ query: activateProfileMutation, variables: { input: { Name: profile } } }),
      `The ${profile} profile is now active. Every reader uses it as its cache allows; see each section.`,
    ).then(() => load());

  const openCopy = () => {
    setProfileError(null);
    setCopyName(profile === DEFAULT_PROFILE ? 'custom' : `${profile}-copy`);
    setCopyDescription('');
    setCopyOpen(true);
  };

  const copyProblem = profileNameProblem(copyName, profiles);

  const copy = async () => {
    const name = copyName;
    const made = await runProfileOperation(
      () =>
        client.graphql({
          query: createProfileMutation,
          variables: {
            input: { Name: name, CopyFrom: profile, Description: copyDescription.trim() || null },
          },
        }),
      `Created the ${name} profile as a copy of ${profile}. Activate it to have meetings use it.`,
    );
    if (made) {
      setCopyOpen(false);
      setProfile(name);
    }
  };

  const remove = async () => {
    const name = profile;
    const gone = await runProfileOperation(
      () => client.graphql({ query: deleteProfileMutation, variables: { input: { Name: name } } }),
      `Deleted the ${name} profile. Its revisions stay in its history.`,
    );
    if (gone) {
      setDeleteOpen(false);
      setProfile((profiles.find((entry) => entry.active) || { name: DEFAULT_PROFILE }).name);
    }
  };

  // Closing or reloading the page with unsaved changes asks first. Following
  // View history asks too; other navigation inside the app does not.
  useEffect(() => {
    if (!dirty) return undefined;
    const warn = (event) => {
      event.preventDefault();
      // eslint-disable-next-line no-param-reassign -- the browser reads it from the event
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);

  // A link to one section opens it and brings it into view, once.
  useEffect(() => {
    if (!focus || !doc || scrolled.current) return;
    scrolled.current = true;
    document.getElementById(sectionElementId(focus))?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
  }, [focus, doc]);

  const confirmLeaving = (event) => {
    // eslint-disable-next-line no-alert -- the browser's own prompt, as for closing the page
    if (dirty && !window.confirm('Leave the Configuration page? Your unsaved changes will be lost.')) {
      event.preventDefault();
    }
  };

  const setSection = (name) => (values) => setEdits((current) => ({ ...current, [name]: values }));

  // Export is a download made here, of the configuration as saved: what the
  // profile stores for each section, its changes over the release defaults.
  const exportConfiguration = () => {
    const now = new Date().toISOString();
    const document = exportDocument({
      sections,
      defaults: doc.defaults,
      values: loaded,
      profile: doc.profile,
      revision: doc.publishedRevision,
      stackName: settings?.StackName,
      now,
    });
    const blob = new Blob([`${JSON.stringify(document, null, 2)}\n`], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = window.document.createElement('a');
    link.href = url;
    link.download = `lma-configuration-${settings?.StackName || 'export'}-${now.slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(url);
    setStatus({
      type: 'info',
      text: dirty
        ? 'Exported the configuration as saved. Your unsaved changes are not in the file.'
        : 'Exported the configuration as saved.',
    });
  };

  // Import never saves. What the file holds becomes unsaved changes on the
  // page, marked and checked like any edit, and is saved, as one revision that
  // can be restored, only when the admin saves it.
  const importConfiguration = async (file) => {
    if (!file) return;
    try {
      const { values, skipped, withheld } = readExport(await file.text(), sections, doc.defaults, settings, edits);
      setEdits((current) => ({ ...current, ...values }));
      setComment((current) => current || `Imported from ${file.name}`.slice(0, MAX_COMMENT_LENGTH));
      const names = sections.filter((section) => values[section.name]).map(sectionLabel);
      const replacedEdits = sections.some(
        (section) => values[section.name] && !deepEqual(edits[section.name], loaded[section.name]),
      );
      const notes = [
        replacedEdits ? 'It replaced unsaved edits you had made in those sections.' : '',
        skipped.length ? `Not used, because this deployment has no such section: ${skipped.join(', ')}.` : '',
        withheld.length
          ? `Not used, because this deployment does not offer them: ${withheld
              .map(([name, key]) => {
                const section = sections.find((candidate) => candidate.name === name);
                return `${sectionLabel(section)} › ${section.schema.properties?.[key]?.title || key}`;
              })
              .join(', ')}.`
          : '',
      ].filter(Boolean);
      setStatus({
        type: skipped.length || withheld.length || replacedEdits ? 'warning' : 'info',
        text: [
          `Loaded ${names.join(', ') || 'nothing'} from ${file.name}.`,
          'Nothing is saved yet: review the marked changes, then save.',
          ...notes,
        ].join(' '),
      });
    } catch (err) {
      setStatus({ type: 'error', text: `Could not import ${file.name}: ${err.message}` });
    }
  };

  const presetSections = (entry) =>
    Object.keys(entry?.Sections || {}).map((name) => ({
      name,
      section: sections.find((candidate) => candidate.name === name),
    }));

  // A preset, like an import, only changes the page.
  const applyPreset = () => {
    const next = {};
    presetSections(preset).forEach(({ name, section }) => {
      if (section) {
        next[name] = applyPresetSection(
          section.schema,
          doc.defaults[name],
          edits[name],
          preset.Sections[name],
          settings,
        );
      }
    });
    setEdits((current) => ({ ...current, ...next }));
    setComment((current) => current || `Applied the preset "${preset.Title}"`.slice(0, MAX_COMMENT_LENGTH));
    const replacedEdits = Object.keys(next).some((name) => !deepEqual(edits[name], loaded[name]));
    setStatus({
      type: replacedEdits ? 'warning' : 'info',
      text: [
        `Applied "${preset.Title}" to the page. Nothing is saved yet: review the marked changes, then save.`,
        replacedEdits ? 'It was applied over unsaved edits you had made in those sections.' : '',
      ]
        .filter(Boolean)
        .join(' '),
    });
    setPreset(null);
  };

  const openSave = () => {
    setSaveError(null);
    setSaveOpen(true);
  };

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    const names = changed.map((section) => section.name);
    try {
      const result = await client.graphql({
        query: updateConfigurationMutation,
        variables: {
          input: {
            Profile: profile,
            Sections: JSON.stringify(Object.fromEntries(names.map((name) => [name, stored[name]]))),
            ExpectedVersion: doc.version,
            Comment: comment.trim() || null,
          },
        },
      });
      const outcome = result.data?.updateConfiguration || {};
      let labelProblem = null;
      if (label.trim() && outcome.Revision) {
        try {
          await client.graphql({
            query: labelRevisionMutation,
            variables: { input: { Profile: profile, Revision: outcome.Revision, Label: label.trim() } },
          });
        } catch (err) {
          labelProblem = errorText(err);
        }
      }
      const timing = [...new Set(changed.map((section) => appliesText(section.tier, section.schema)))].join(' ');
      let text;
      if (outcome.Unchanged) {
        text = `Nothing was saved: the configuration already holds these values${
          label.trim() ? ', so there is no new revision to label' : ''
        }.`;
      } else if (labelProblem) {
        text = `Saved as revision ${outcome.Revision}, but the label could not be set: ${labelProblem}`;
      } else {
        text = `Saved as revision ${outcome.Revision}. ${timing}`;
      }
      setStatus({ type: labelProblem ? 'warning' : 'success', text, revision: outcome.Revision });
      setSaveOpen(false);
      setComment('');
      setLabel('');
      await load();
    } catch (err) {
      const invalid = invalidMessage(err);
      if (isConflict(err)) {
        setSaveOpen(false);
        setConflict(true);
      } else if (invalid) {
        setSaveOpen(false);
        setStatus({ type: 'error', text: `The configuration was refused: ${invalid}` });
      } else {
        setSaveError(`Could not save the configuration: ${errorText(err)}`);
      }
    } finally {
      setSaving(false);
    }
  };

  // Someone saved after this page loaded. Re-apply the changes made here over
  // what they saved, and say where both changed the same setting.
  const keepMine = async () => {
    setLoading(true);
    try {
      const parsed = await read();
      const theirs = effectiveOf(parsed);
      const next = { ...theirs };
      const overlaps = [];
      changed.forEach((section) => {
        const { merged, conflicts } = rebaseSection(
          section.schema,
          loaded[section.name],
          edits[section.name],
          theirs[section.name],
        );
        next[section.name] = merged;
        conflicts.forEach((path) =>
          overlaps.push(path.length ? `${sectionLabel(section)} › ${formatPath(path)}` : sectionLabel(section)),
        );
      });
      setDoc(parsed);
      setLoaded(theirs);
      setEdits(next);
      setConflict(false);
      setStatus({
        type: overlaps.length ? 'warning' : 'info',
        text: overlaps.length
          ? `Your changes are applied over the configuration as it is now saved. These settings were also changed in ` +
            `the other save, and now hold your value: ${overlaps.join('; ')}. Review them, then save.`
          : 'Your changes are applied over the configuration as it is now saved. Review them, then save.',
      });
    } catch (err) {
      setStatus({ type: 'error', text: `Could not load the configuration: ${errorText(err)}` });
    } finally {
      setLoading(false);
    }
  };

  if (loading && !doc) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" /> Loading the configuration...
      </Box>
    );
  }

  // The default and Managed profiles are rewritten by every stack update, so
  // they are shown, compared and activated, and copied to be changed.
  const readOnly = !doc?.exists || doc?.managed;
  const historyHref = `#${CONFIGURATION_HISTORY_PATH}?profile=${encodeURIComponent(profile || '')}`;
  // A link to a section this deployment does not offer says so, rather than
  // opening a page without it.
  const unavailable = (doc?.sections || []).find(
    (section) => section.name === focus && !isAvailable(section.schema, settings),
  );
  const provenance = doc?.updatedBy
    ? `Last saved by ${doc.updatedBy}${doc.updatedAt ? ` on ${whenText(doc.updatedAt)}` : ''}` +
      `${doc.publishedRevision ? ` (revision ${doc.publishedRevision})` : ''}.`
    : '';

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={
          `Every setting LMA reads at run time, in one place. The active profile is what meetings use; a value ` +
          `that differs from the deployment default is marked. ${provenance}`
        }
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Select
              ariaLabel="Configuration profile"
              selectedOption={
                selected
                  ? { value: selected.name, label: profileText(selected), tags: profileTags(selected) }
                  : { value: profile || '', label: profile || '' }
              }
              options={profiles.map((entry) => ({
                value: entry.name,
                label: profileText(entry),
                description: entry.description || undefined,
                tags: profileTags(entry),
              }))}
              onChange={({ detail }) => chooseProfile(detail.selectedOption.value)}
              disabled={saving || profileBusy}
            />
            <ButtonDropdown
              disabled={!doc || saving || profileBusy}
              items={[
                { id: 'activate', text: 'Activate', disabled: !doc?.exists || doc?.active },
                { id: 'copy', text: 'Copy to a new profile', disabled: !doc?.exists },
                {
                  id: 'delete',
                  text: 'Delete',
                  disabled: !doc?.exists || doc?.managed || doc?.active,
                  disabledReason: doc?.active
                    ? 'Activate another profile first.'
                    : 'The deployment manages this profile.',
                },
              ]}
              onItemClick={({ detail }) => {
                setProfileError(null);
                if (detail.id === 'activate') activate();
                else if (detail.id === 'copy') openCopy();
                else if (detail.id === 'delete') setDeleteOpen(true);
              }}
            >
              Profile
            </ButtonDropdown>
            <Button href={historyHref} onFollow={confirmLeaving}>
              View history
            </Button>
            <ButtonDropdown
              disabled={!doc || readOnly || saving}
              items={[
                { id: 'export', text: 'Export to a file', disabled: !doc },
                { id: 'import', text: 'Import from a file' },
                ...(doc?.presets?.length
                  ? [
                      {
                        id: 'presets',
                        text: 'Apply a preset',
                        items: doc.presets.map((entry) => ({
                          id: `preset:${entry.Id}`,
                          text: entry.Title,
                          disabled: !isPresetOffered(entry, sections, settings),
                          disabledReason: 'It sets something this deployment does not include.',
                        })),
                      },
                    ]
                  : []),
              ]}
              onItemClick={({ detail }) => {
                if (detail.id === 'export') exportConfiguration();
                else if (detail.id === 'import') fileInput.current?.click();
                else if (detail.id.startsWith('preset:')) {
                  setPreset(doc.presets.find((entry) => `preset:${entry.Id}` === detail.id));
                }
              }}
            >
              Import, export and presets
            </ButtonDropdown>
            <Button iconName="refresh" onClick={load} disabled={loading || saving} ariaLabel="Reload" />
            <Button onClick={() => setEdits(loaded)} disabled={!dirty || saving}>
              Discard changes
            </Button>
            <Button variant="primary" onClick={openSave} disabled={!dirty || problemCount > 0 || saving || readOnly}>
              Save changes
            </Button>
          </SpaceBetween>
        }
      >
        Configuration
      </Header>

      {profileError && (
        <Alert type="error" dismissible onDismiss={() => setProfileError(null)}>
          {profileError}
        </Alert>
      )}

      {doc && !doc.exists && (
        <Alert type="error" header={`There is no configuration profile named ${profile}`}>
          Choose another profile.
        </Alert>
      )}

      {doc?.exists && doc.managed && (
        <Alert
          type="info"
          header={
            profile === DEFAULT_PROFILE
              ? 'The deployment default is read-only'
              : `The ${profile} profile is managed by the deployment`
          }
          action={<Button onClick={openCopy}>Copy to a new profile</Button>}
        >
          {`Every stack update rewrites it${
            profile === DEFAULT_PROFILE ? ' with the release defaults and the stack parameters' : ''
          }, so it cannot be saved. Copy it to a Custom profile to change a setting, then activate the copy.`}
        </Alert>
      )}

      {doc?.exists && !doc.managed && !doc.active && (
        <Alert type="info" action={<Button onClick={activate}>Activate</Button>}>
          {`The ${profile} profile is not the active one, so meetings do not use it. Saving changes it; activate it to ` +
            'have meetings use it.'}
        </Alert>
      )}

      {unavailable && (
        <Alert type="info" header={`${sectionLabel(unavailable)} is not available in this deployment`}>
          {UNAVAILABLE_REASONS[unavailable.name] || 'It depends on a part of LMA this deployment does not include.'}
        </Alert>
      )}

      {conflict && (
        <Alert
          type="warning"
          header="Someone else saved the configuration after this page loaded it"
          action={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={keepMine}>Reload and keep my changes</Button>
              <Button onClick={load}>Discard my changes and reload</Button>
            </SpaceBetween>
          }
        >
          Your save was not made, so nothing of theirs was replaced. Keep your changes to apply them over what they
          saved and review the result, or discard yours to see the configuration as it is now saved.
        </Alert>
      )}

      {status && (
        <Alert
          type={status.type}
          dismissible
          onDismiss={() => setStatus(null)}
          action={
            status.revision ? (
              <Button href={historyHref} onFollow={confirmLeaving}>
                View history
              </Button>
            ) : undefined
          }
        >
          {status.text}
        </Alert>
      )}

      {problemCount > 0 && (
        <Alert type="error">
          {`${problemCount} ${problemCount === 1 ? 'setting needs' : 'settings need'} correcting before the ` +
            'configuration can be saved. Each is marked in its section.'}
        </Alert>
      )}

      {sections.map((section) => (
        <ConfigurationSection
          key={section.name}
          section={section}
          values={edits[section.name] || {}}
          loaded={loaded[section.name] || {}}
          defaults={doc.defaults[section.name] || {}}
          problems={problems[section.name] || []}
          settings={settings}
          saved={loaded}
          readOnly={readOnly}
          expanded={focus ? focus === section.name : section.schema.defaultExpanded === true}
          onChange={setSection(section.name)}
        />
      ))}

      <input
        ref={fileInput}
        type="file"
        accept="application/json,.json"
        style={{ display: 'none' }}
        aria-label="Configuration file to import"
        data-testid="configuration-import"
        onChange={(event) => {
          importConfiguration(event.target.files?.[0]);
          // So choosing the same file again imports it again.
          // eslint-disable-next-line no-param-reassign -- resetting the picker
          event.target.value = '';
        }}
      />

      <Modal
        visible={Boolean(preset)}
        onDismiss={() => setPreset(null)}
        header={preset ? `Apply "${preset.Title}"?` : ''}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setPreset(null)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={applyPreset}>
                Apply to the page
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>{preset?.Description}</Box>
          <ul>
            {presetSections(preset).map(({ name, section }) => (
              <li key={name}>
                {section
                  ? `${sectionLabel(section)}: ${
                      preset.Sections[name].mode === 'replace'
                        ? 'replaced by the preset'
                        : 'the settings the preset names change, and the others stay'
                    }`
                  : `${name}: not in this deployment, so left out`}
              </li>
            ))}
          </ul>
          <Box>Nothing is saved until you review the changes and save them.</Box>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={copyOpen}
        onDismiss={() => !profileBusy && setCopyOpen(false)}
        header={`Copy ${profile} to a new profile`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setCopyOpen(false)} disabled={profileBusy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={copy} loading={profileBusy} disabled={Boolean(copyProblem)}>
                Create
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {profileError && <Alert type="error">{profileError}</Alert>}
          <Box>
            The new profile is a Custom copy of every setting, which you can change and save. It is not active until you
            activate it.
          </Box>
          <FormField label="Name" errorText={copyName ? copyProblem : null}>
            <Input value={copyName} ariaLabel="Profile name" onChange={({ detail }) => setCopyName(detail.value)} />
          </FormField>
          <FormField label="Description" description="Optional.">
            <Input
              value={copyDescription}
              ariaLabel="Profile description"
              onChange={({ detail }) => setCopyDescription(detail.value.slice(0, MAX_COMMENT_LENGTH))}
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={deleteOpen}
        onDismiss={() => !profileBusy && setDeleteOpen(false)}
        header={`Delete the ${profile} profile?`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteOpen(false)} disabled={profileBusy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={remove} loading={profileBusy}>
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {profileError && <Alert type="error">{profileError}</Alert>}
          <Box>The profile is deleted. Its revisions stay in its history.</Box>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={saveOpen}
        onDismiss={() => !saving && setSaveOpen(false)}
        header="Save the configuration?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setSaveOpen(false)} disabled={saving}>
                Cancel
              </Button>
              <Button variant="primary" onClick={save} loading={saving}>
                Save
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          {saveError && <Alert type="error">{saveError}</Alert>}
          <Box>The save is recorded as one revision in the configuration history. It changes:</Box>
          <ul>
            {changed.map((section) => {
              const count = changedCount(loaded[section.name], edits[section.name]);
              return (
                <li key={section.name}>
                  {`${sectionLabel(section)}: ${count} ${count === 1 ? 'setting' : 'settings'}. ${appliesText(
                    section.tier,
                    section.schema,
                  )}`}
                </li>
              );
            })}
          </ul>
          <FormField
            label="Comment"
            description="Optional. Recorded with the revision, so the history says why it was saved."
            constraintText={`${comment.length}/${MAX_COMMENT_LENGTH} characters`}
          >
            <Textarea
              value={comment}
              rows={2}
              ariaLabel="Comment"
              onChange={({ detail }) => setComment(detail.value.slice(0, MAX_COMMENT_LENGTH))}
            />
          </FormField>
          <FormField
            label="Label"
            description="Optional. A labelled revision is kept however many revisions follow it."
            constraintText={`${label.length}/${MAX_LABEL_LENGTH} characters`}
          >
            <Input
              value={label}
              ariaLabel="Label"
              onChange={({ detail }) => setLabel(detail.value.slice(0, MAX_LABEL_LENGTH))}
            />
          </FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

// Every setting here applies to every meeting in the deployment, so the page is
// for Admin users; everyone else is told so instead of being shown a form whose
// save would be refused.
const ConfigurationPage = () => (
  <AdminOnly feature="the configuration">
    <ConfigurationContent />
  </AdminOnly>
);

export default ConfigurationPage;
