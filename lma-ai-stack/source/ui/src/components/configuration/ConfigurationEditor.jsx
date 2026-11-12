/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import { generateClient } from 'aws-amplify/api';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
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
  RadioGroup,
  SegmentedControl,
  SpaceBetween,
  Spinner,
  Textarea,
} from '@cloudscape-design/components';

import useSettingsContext from '../../contexts/settings';
import { isConflict } from '../common/config-conflict';
import { MAX_LABEL_LENGTH, labelRevisionMutation } from '../configuration-history/history';
import {
  appliesText,
  applyPresetSection,
  changedCount,
  clone,
  deepEqual,
  effectiveSection,
  exportDocument,
  isAvailable,
  isPresetOffered,
  orderedPresets,
  readExport,
  rebaseSection,
  storedSection,
} from './config-model';
import {
  DEFAULT_PROFILE,
  createProfileMutation,
  errorText,
  getConfigurationQuery,
  invalidMessage,
  parseConfiguration,
  profileNameProblem,
  refusedMessage,
  updateConfigurationMutation,
} from './configuration-api';
import { configurationText, readConfigurationText } from './config-text';
import ConfigurationSection, { sectionElementId } from './ConfigurationSection';
import { profileText } from './ProfilesTable';
import { UNAVAILABLE_REASONS } from './section-notices';
import { checkDetailed, formatPath, limitProblems } from './schema-validation';
import './configuration.css';

const client = generateClient();

// The limit the store applies to a revision comment.
export const MAX_COMMENT_LENGTH = 500;

const sectionLabel = (section) => section.schema.sectionLabel || section.schema.title || section.name;

// Each section's effective values: the baseline with what the profile stores over it.
export const effectiveOf = (doc) =>
  Object.fromEntries(
    doc.sections.map((section) => [
      section.name,
      effectiveSection(section.schema, doc.defaults[section.name], doc.custom[section.name]),
    ]),
  );

const PRESETS_DESCRIPTION = [
  'Defaults for consent, retention and privacy. Each one sets the same settings,',
  'so applying one replaces what another set; every other setting stays.',
].join(' ');

const COPY_TO_CHANGE = 'This profile is read-only: copy it to a Custom profile to change it.';

const VIEW_OPTIONS = [
  { id: 'form', text: 'Form' },
  { id: 'json', text: 'JSON' },
  { id: 'yaml', text: 'YAML' },
];

const whenText = (iso) => {
  const parsed = iso ? new Date(iso) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString() : iso || '';
};

/**
 * The editor for one configuration profile: every section as a form, or the
 * whole profile as JSON or YAML, with validation, the save dialog and the
 * handling of a save someone else made first. Which profile it shows, and what
 * is done to whole profiles, is the workspace's, which renders it.
 */
const ConfigurationEditor = ({
  profile,
  profiles = [],
  reloadKey = 0,
  onDirtyChange = () => {},
  onOpenHistory = () => {},
  onCopy = () => {},
  onActivate = () => {},
  onProfileCreated = () => {},
}) => {
  const { settings } = useSettingsContext() || {};
  const location = useLocation();
  const focus = new URLSearchParams(location.search).get('section');

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
  // The Apply a preset dialog, and the preset chosen in it.
  const [presetOpen, setPresetOpen] = useState(false);
  const [presetChoice, setPresetChoice] = useState(null);
  // Saving the editor's contents, unsaved edits included, as a new profile.
  const [saveAsOpen, setSaveAsOpen] = useState(false);
  const [saveAsName, setSaveAsName] = useState('');
  const [saveAsDescription, setSaveAsDescription] = useState('');
  const [saveAsBusy, setSaveAsBusy] = useState(false);
  const [saveAsError, setSaveAsError] = useState(null);
  // The form, or the whole configuration as JSON or YAML text. The text is
  // read into the page's edits as it is typed, so it is checked and saved like
  // any other edit; `textError` is what stops a text that does not read.
  const [view, setView] = useState('form');
  const [configText, setConfigText] = useState('');
  const [textError, setTextError] = useState(null);
  // Settings named in the text that this deployment does not offer, so not used.
  const [textWithheld, setTextWithheld] = useState([]);
  const scrolled = useRef(false);
  const fileInput = useRef(null);
  // The edits the text view last produced, so typing does not rewrite the text.
  const fromText = useRef(null);

  const read = useCallback(async () => {
    const result = await client.graphql({ query: getConfigurationQuery, variables: { Profile: profile } });
    return parseConfiguration(result.data?.getConfiguration);
  }, [profile]);

  // Another profile is another document: nothing of the last one's is shown
  // while it loads, nor said about it afterwards.
  useEffect(() => {
    setDoc(null);
    setStatus(null);
    setConflict(false);
    setTextError(null);
    setTextWithheld([]);
    fromText.current = null;
  }, [profile]);

  // The profile last asked for, so a read that finishes after another profile
  // was opened is not shown as that one.
  const wanted = useRef(profile);
  wanted.current = profile;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const parsed = await read();
      if (wanted.current !== profile) return;
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
  }, [read, profile]);

  // Loaded again when the workspace says the profile changed under it, as a
  // restore does.
  useEffect(() => {
    load();
  }, [load, reloadKey]);

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

  // The workspace asks before opening another profile over unsaved changes.
  useEffect(() => {
    onDirtyChange(dirty);
  }, [dirty, onDirtyChange]);

  // The text follows every change made elsewhere -- a load, a discard, an
  // import, a preset -- but not the one it made itself.
  useEffect(() => {
    if (view === 'form' || edits === fromText.current) return;
    setConfigText(configurationText(sections, edits, view, settings));
    setTextError(null);
    setTextWithheld([]);
  }, [view, edits, sections, settings]);

  const editText = (value) => {
    setConfigText(value);
    try {
      const parsed = readConfigurationText(value, view, sections, doc.defaults, settings, edits);
      const next = { ...edits, ...parsed.values };
      fromText.current = next;
      setEdits(next);
      setTextError(null);
      setTextWithheld(parsed.withheld);
    } catch (err) {
      setTextError(err.message);
    }
  };

  // Discarding also puts back a text that did not read, which changed no edit.
  const discard = () => {
    fromText.current = null;
    setTextError(null);
    setEdits(loaded);
    setTextWithheld([]);
    if (view !== 'form') setConfigText(configurationText(sections, loaded, view, settings));
  };

  const chooseView = (next) => {
    // A text that does not read would be lost by leaving it, so it has to be
    // corrected or discarded first.
    if (textError) return;
    fromText.current = null;
    setView(next);
  };

  const listed = profiles.find((entry) => entry.name === profile);
  // Activating is done from the profile list, so the list says which is active.
  const active = listed ? listed.active : Boolean(doc?.active);

  // Every section, as a save would store it, to make a new profile of: what
  // the editor holds now, unsaved edits included, and the sections this
  // deployment does not show as they are stored.
  const saveAsBody = () =>
    Object.fromEntries(
      (doc?.sections || []).map((section) => [
        section.name,
        storedSection(section.schema, doc.defaults[section.name], edits[section.name]),
      ]),
    );

  const openSaveAs = () => {
    setSaveAsError(null);
    setSaveAsName('');
    setSaveAsDescription('');
    setSaveAsOpen(true);
  };

  const saveAsProblem = profileNameProblem(saveAsName, profiles);

  const saveAs = async () => {
    const name = saveAsName;
    setSaveAsBusy(true);
    setSaveAsError(null);
    try {
      await client.graphql({
        query: createProfileMutation,
        variables: {
          input: {
            Name: name,
            Body: JSON.stringify(saveAsBody()),
            Description: saveAsDescription.trim() || null,
            Comment: `Saved from the ${profile} profile${dirty ? ', with edits not saved there' : ''}`.slice(
              0,
              MAX_COMMENT_LENGTH,
            ),
          },
        },
      });
      setSaveAsOpen(false);
      onProfileCreated(
        name,
        `Created the ${name} profile from what the editor held for ${profile}${
          dirty ? ', including the unsaved edits' : ''
        }. ${profile} itself is unchanged. Activate ${name} to have meetings use it.`,
      );
    } catch (err) {
      setSaveAsError(refusedMessage(err) || invalidMessage(err) || errorText(err));
    } finally {
      setSaveAsBusy(false);
    }
  };

  const allDefaults = sections.every((section) => deepEqual(edits[section.name], doc?.defaults[section.name] || {}));

  // Reset section to defaults, for every section shown, as unsaved changes.
  const resetAll = () => {
    const next = Object.fromEntries(sections.map((section) => [section.name, clone(doc.defaults[section.name] || {})]));
    setEdits((current) => ({ ...current, ...next }));
    setStatus({
      type: 'info',
      text:
        'Every section is set to its defaults on the page. Nothing is saved yet: review the marked changes, then ' +
        'save them, or discard them.',
    });
  };

  // Closing or reloading the page with unsaved changes asks first; so does
  // opening another profile, which the workspace asks about.
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

  // The header, which holds Save changes, is sticky so it stays in view down a
  // long page: below the application's top navigation, which is sticky itself,
  // so it is measured as it changes (its height settles once its fonts load).
  // The header's own height is kept too, so a section brought into view is not
  // left beneath it.
  const editorRef = useRef(null);
  const headerRef = useRef(null);
  const shown = Boolean(doc) || !loading;
  useEffect(() => {
    const editor = editorRef.current;
    const header = headerRef.current;
    if (!editor || !header) return undefined;
    const nav = document.getElementById('top-navigation');
    const place = () => {
      editor.style.setProperty('--lma-config-header-top', `${nav ? nav.offsetHeight : 0}px`);
      editor.style.setProperty('--lma-config-header-height', `${header.offsetHeight}px`);
    };
    place();
    window.addEventListener('resize', place);
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place);
    if (nav) observer?.observe(nav);
    observer?.observe(header);
    return () => {
      window.removeEventListener('resize', place);
      observer?.disconnect();
    };
  }, [shown]);

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

  const preset = (presetOpen && doc?.presets?.find((entry) => entry.Id === presetChoice)) || null;

  const openPresets = () => {
    setPresetChoice(null);
    setPresetOpen(true);
  };

  // A preset, like an import, only changes the page.
  const applyPreset = () => {
    if (!preset) return;
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
    setPresetOpen(false);
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
    <div ref={editorRef} className="lma-config-editor">
      <div ref={headerRef} className="lma-config-editor-header">
        <Header
          variant="h2"
          description={
            `${doc?.description ? `${doc.description} ` : ''}A value that differs from the deployment default is ` +
            `marked. ${provenance}`
          }
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button iconName="history" onClick={onOpenHistory} disabled={!doc?.exists}>
                Revisions…
              </Button>
              <ButtonDropdown
                disabled={!doc?.exists || saving}
                items={[
                  { id: 'export', text: 'Export to a file' },
                  { id: 'import', text: 'Import from a file', disabled: readOnly, disabledReason: COPY_TO_CHANGE },
                  ...(doc?.presets?.length
                    ? [
                        {
                          id: 'presets',
                          text: 'Apply a preset…',
                          disabled: readOnly,
                          disabledReason: COPY_TO_CHANGE,
                        },
                      ]
                    : []),
                  {
                    id: 'save-as',
                    text: 'Save current edits as new profile…',
                    disabled: problemCount > 0 || Boolean(textError),
                    disabledReason: 'Correct the settings marked first.',
                  },
                  {
                    id: 'reset-all',
                    text: 'Reset all sections to defaults',
                    disabled: readOnly || allDefaults,
                    disabledReason: readOnly ? COPY_TO_CHANGE : 'Every section already holds its defaults.',
                  },
                ]}
                onItemClick={({ detail }) => {
                  if (detail.id === 'export') exportConfiguration();
                  else if (detail.id === 'import') fileInput.current?.click();
                  else if (detail.id === 'save-as') openSaveAs();
                  else if (detail.id === 'reset-all') resetAll();
                  else if (detail.id === 'presets') openPresets();
                }}
              >
                Actions
              </ButtonDropdown>
              <Button iconName="refresh" onClick={load} disabled={loading || saving} ariaLabel="Reload" />
              <Button onClick={discard} disabled={(!dirty && !textError) || saving}>
                Discard changes
              </Button>
              <Button
                variant="primary"
                onClick={openSave}
                disabled={!dirty || problemCount > 0 || Boolean(textError) || saving || readOnly}
              >
                Save changes
              </Button>
            </SpaceBetween>
          }
        >
          {profileText({ name: profile })}
        </Header>
      </div>

      <SpaceBetween size="l">
        {doc && !doc.exists && (
          <Alert type="error" header={`There is no configuration profile named ${profile}`}>
            Open another profile from the list above.
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
            action={<Button onClick={onCopy}>Copy to a new profile</Button>}
          >
            {`Every stack update rewrites it${
              profile === DEFAULT_PROFILE
                ? ' with the release defaults and what the ConfigurationPreset and CustomConfigPath ' +
                  'stack parameters name'
                : ''
            }, so it cannot be saved. Copy it to a Custom profile to change a setting, then activate the copy.`}
          </Alert>
        )}

        {doc?.exists && !doc.managed && !active && (
          <Alert type="info" action={<Button onClick={onActivate}>Activate</Button>}>
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
            action={status.revision ? <Button onClick={onOpenHistory}>Revisions…</Button> : undefined}
          >
            {status.text}
          </Alert>
        )}

        {problemCount > 0 && view === 'form' && (
          <Alert type="error">
            {`${problemCount} ${problemCount === 1 ? 'setting needs' : 'settings need'} correcting before the ` +
              'configuration can be saved. Each is marked in its section.'}
          </Alert>
        )}

        <SegmentedControl
          label="View"
          selectedId={view}
          options={VIEW_OPTIONS.map((option) => ({ ...option, disabled: Boolean(textError) && option.id !== view }))}
          onChange={({ detail }) => chooseView(detail.selectedId)}
        />

        {view !== 'form' && (
          <FormField
            label={`The configuration as ${view.toUpperCase()}`}
            description={
              readOnly
                ? 'Read-only: copy the profile to a Custom one to change it.'
                : 'Every section, keyed by its name. What you type is checked as you type and saved like an edit ' +
                  'in the form; a section you leave out keeps its values.'
            }
            errorText={textError || undefined}
            stretch
          >
            <Textarea
              value={configText}
              rows={32}
              readOnly={readOnly}
              spellcheck={false}
              ariaLabel={`Configuration ${view.toUpperCase()}`}
              className="lma-config-text"
              onChange={({ detail }) => editText(detail.value)}
            />
          </FormField>
        )}

        {view !== 'form' && textWithheld.length > 0 && (
          <Alert type="warning">
            {`Not used, because this deployment does not offer them: ${textWithheld
              .map(([name, key]) => {
                const section = sections.find((candidate) => candidate.name === name);
                return `${sectionLabel(section)} › ${section.schema.properties?.[key]?.title || key}`;
              })
              .join(', ')}.`}
          </Alert>
        )}

        {view !== 'form' && problemCount > 0 && (
          <Alert
            type="error"
            header={`${problemCount} ${problemCount === 1 ? 'setting needs' : 'settings need'} correcting`}
          >
            <ul>
              {changed.flatMap((section) =>
                (problems[section.name] || []).map((problem) => (
                  <li key={`${section.name}:${formatPath(problem.path)}:${problem.message}`}>
                    {`${sectionLabel(section)} › ${formatPath(problem.path)}: ${problem.message}`}
                  </li>
                )),
              )}
            </ul>
          </Alert>
        )}

        {view === 'form' &&
          sections.map((section) => (
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
          visible={presetOpen}
          onDismiss={() => setPresetOpen(false)}
          header="Apply a preset"
          size="large"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setPresetOpen(false)}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={applyPreset} disabled={!preset}>
                  Apply to the page
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="l">
            <FormField label="Configuration presets" description={PRESETS_DESCRIPTION}>
              <RadioGroup
                ariaLabel="Configuration presets"
                value={preset ? preset.Id : null}
                onChange={({ detail }) => setPresetChoice(detail.value)}
                items={orderedPresets(doc?.presets).map((entry) => {
                  const offeredHere = isPresetOffered(entry, sections, settings);
                  return {
                    value: entry.Id,
                    label: entry.Title,
                    description: offeredHere
                      ? entry.Description
                      : `${entry.Description} Not offered here: it sets something this deployment does not include.`,
                    disabled: !offeredHere,
                  };
                })}
              />
            </FormField>
            {preset && (
              <SpaceBetween size="xs">
                <Box variant="h4">{`What "${preset.Title}" changes`}</Box>
                <ul>
                  {presetSections(preset).map(({ name, section }) => (
                    <li key={name}>
                      {section
                        ? `${sectionLabel(section)}: ${Object.keys(preset.Sections[name].values || {})
                            .map((key) => section.schema?.properties?.[key]?.title || key)
                            .join(', ')} change, and the others stay`
                        : `${name}: not in this deployment, so left out`}
                    </li>
                  ))}
                </ul>
              </SpaceBetween>
            )}
            <Box>Nothing is saved until you review the changes and save them.</Box>
          </SpaceBetween>
        </Modal>

        <Modal
          visible={saveAsOpen}
          onDismiss={() => !saveAsBusy && setSaveAsOpen(false)}
          header="Save current edits as a new profile"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button variant="link" onClick={() => setSaveAsOpen(false)} disabled={saveAsBusy}>
                  Cancel
                </Button>
                <Button variant="primary" onClick={saveAs} loading={saveAsBusy} disabled={Boolean(saveAsProblem)}>
                  Save as new profile
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            {saveAsError && <Alert type="error">{saveAsError}</Alert>}
            <Box>
              {`The new profile is a Custom one holding every setting as the editor shows it now${
                dirty ? ', including your unsaved edits' : ''
              }. ${profile} itself is left as it is saved. The new profile is not active until you activate it. To ` +
                "copy a profile's last saved state instead, use Create profile in the list of profiles."}
            </Box>
            <FormField label="Name" errorText={saveAsName ? saveAsProblem : null}>
              <Input
                value={saveAsName}
                ariaLabel="New profile name"
                placeholder="e.g. trial"
                onChange={({ detail }) => setSaveAsName(detail.value)}
              />
            </FormField>
            <FormField label="Description" description="Optional.">
              <Input
                value={saveAsDescription}
                ariaLabel="New profile description"
                onChange={({ detail }) => setSaveAsDescription(detail.value.slice(0, MAX_COMMENT_LENGTH))}
              />
            </FormField>
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
    </div>
  );
};

ConfigurationEditor.propTypes = {
  // The profile shown.
  profile: PropTypes.string.isRequired,
  // Every profile, as the workspace lists them: for the names a new profile
  // cannot take, and which profile is active.
  profiles: PropTypes.arrayOf(PropTypes.shape({ name: PropTypes.string, active: PropTypes.bool })),
  // Changed by the workspace to have the editor read the profile again.
  reloadKey: PropTypes.number,
  onDirtyChange: PropTypes.func,
  onOpenHistory: PropTypes.func,
  // Copy the profile shown to a new one; activate it.
  onCopy: PropTypes.func,
  onActivate: PropTypes.func,
  // Told the name of a profile Save as new profile created, and what to say about it.
  onProfileCreated: PropTypes.func,
};

export default ConfigurationEditor;
