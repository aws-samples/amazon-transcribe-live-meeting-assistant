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
  FormField,
  Header,
  Input,
  Modal,
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
  changedCount,
  deepEqual,
  effectiveSection,
  isAvailable,
  rebase,
  storedSection,
} from './config-model';
import {
  PROFILE,
  errorText,
  getConfigurationQuery,
  invalidMessage,
  parseConfiguration,
  updateConfigurationMutation,
} from './configuration-api';
import ConfigurationSection, { sectionElementId } from './ConfigurationSection';
import { checkDetailed, formatPath } from './schema-validation';
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

const whenText = (iso) => {
  const parsed = iso ? new Date(iso) : null;
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString() : iso || '';
};

export const ConfigurationContent = () => {
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
  const scrolled = useRef(false);

  const read = useCallback(async () => {
    const result = await client.graphql({ query: getConfigurationQuery, variables: { Profile: PROFILE } });
    return parseConfiguration(result.data?.getConfiguration);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const parsed = await read();
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
  }, [read]);

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
  const problems = Object.fromEntries(
    changed.map((section) => [section.name, checkDetailed(section.schema, stored[section.name])]),
  );
  const problemCount = Object.values(problems).reduce((total, list) => total + list.length, 0);

  // Leaving with unsaved changes asks first.
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

  const setSection = (name) => (values) => setEdits((current) => ({ ...current, [name]: values }));

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
            Profile: PROFILE,
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
            variables: { input: { Profile: PROFILE, Revision: outcome.Revision, Label: label.trim() } },
          });
        } catch (err) {
          labelProblem = errorText(err);
        }
      }
      const timing = [...new Set(changed.map((section) => appliesText(section.tier)))].join(' ');
      let text;
      if (outcome.Unchanged) {
        text = 'Nothing was saved: the configuration already holds these values.';
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
        const { merged, conflicts } = rebase(loaded[section.name], edits[section.name], theirs[section.name]);
        next[section.name] = merged;
        conflicts.forEach((path) => overlaps.push(`${sectionLabel(section)} › ${formatPath(path)}`));
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

  const readOnly = !doc?.exists;
  const provenance = doc?.updatedBy
    ? `Last saved by ${doc.updatedBy}${doc.updatedAt ? ` on ${whenText(doc.updatedAt)}` : ''}` +
      `${doc.publishedRevision ? ` (revision ${doc.publishedRevision})` : ''}.`
    : '';

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description={
          'Every setting LMA reads at run time, in one place. A value that differs from the shipped default is ' +
          'marked, and only those are stored, so a later release keeps improving the rest. ' +
          provenance
        }
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button href={`#${CONFIGURATION_HISTORY_PATH}`}>View history</Button>
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

      {readOnly && doc && (
        <Alert type="error" header="There is no configuration profile to save into">
          The configuration profile is created when the stack is deployed. Deploy or update the stack, then reload this
          page.
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
          action={status.revision ? <Button href={`#${CONFIGURATION_HISTORY_PATH}`}>View history</Button> : undefined}
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
          readOnly={readOnly}
          expanded={focus ? focus === section.name : section.schema.defaultExpanded === true}
          onChange={setSection(section.name)}
        />
      ))}

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
