/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Create or edit one skill: its name, description, Markdown instructions (with a
// preview) and its text reference files, and, for a meeting playbook, its
// playbook section (docs/meeting-playbooks.md). Metadata kept from an imported
// file (an Amazon Quick skill's trigger, icon and inputs, for example) is shown
// and saved unchanged.

import React, { useEffect, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import ReactMarkdown from 'react-markdown';
import {
  Alert,
  Box,
  Button,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Modal,
  SpaceBetween,
  Tabs,
  Textarea,
  Toggle,
} from '@cloudscape-design/components';

import PlaybookSectionEditor from '../meeting-playbooks/PlaybookSectionEditor';
import { PLAYBOOK_KEY, emptyPlaybook, playbookForm } from '../meeting-playbooks/playbook-model';

import { byteSize, formProblems, MAX_FILE_BYTES } from './skills-api';

const PLAYBOOK_TEXT =
  'A playbook sets a meeting up when it is chosen for it: whiteboards, live guidance checks and quick actions ' +
  'in the assistant chat. Its instructions above are what the assistant follows in that meeting.';

const EMPTY_FORM = {
  name: '',
  displayName: '',
  description: '',
  instructions: '',
  files: [],
  metadata: {},
  playbook: null,
};

const MetadataSummary = ({ metadata = {} }) => {
  // The playbook section has its own editor.
  const entries = Object.entries(metadata || {}).filter(([key]) => key !== PLAYBOOK_KEY);
  if (!entries.length) return null;
  return (
    <FormField label="Metadata kept from the imported file" description="Saved unchanged with the skill.">
      <ColumnLayout columns={2} variant="text-grid">
        {entries.map(([key, value]) => (
          <div key={key}>
            <Box variant="awsui-key-label">{key}</Box>
            <div>{typeof value === 'string' ? value : JSON.stringify(value)}</div>
          </div>
        ))}
      </ColumnLayout>
    </FormField>
  );
};

MetadataSummary.propTypes = { metadata: PropTypes.shape({}) };

const SkillEditorModal = ({ visible, initial = null, saving = false, error = null, onCancel, onSave }) => {
  const [form, setForm] = useState(initial || EMPTY_FORM);
  const [tab, setTab] = useState('write');
  const [touched, setTouched] = useState(false);
  const fileInput = useRef(null);

  useEffect(() => {
    if (visible) {
      setForm(initial || EMPTY_FORM);
      setTab('write');
      setTouched(false);
    }
  }, [visible, initial]);

  const set = (key) => (value) => setForm((prev) => ({ ...prev, [key]: value }));
  const setFile = (index, key, value) =>
    setForm((prev) => ({
      ...prev,
      files: prev.files.map((file, i) => (i === index ? { ...file, [key]: value } : file)),
    }));
  const removeFile = (index) => setForm((prev) => ({ ...prev, files: prev.files.filter((_, i) => i !== index) }));
  const addFile = (file = { path: '', content: '' }) => setForm((prev) => ({ ...prev, files: [...prev.files, file] }));

  const addFromDisk = async (file) => {
    if (!file) return;
    addFile({ path: file.name, content: await file.text() });
    if (fileInput.current) fileInput.current.value = '';
  };

  const problems = formProblems(form);
  const editing = Boolean(initial?.name);

  return (
    <Modal
      visible={visible}
      size="max"
      onDismiss={onCancel}
      header={editing ? `Edit skill ${initial.name}` : 'New skill'}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onCancel}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={saving}
              onClick={() => {
                setTouched(true);
                if (!problems.length) onSave(form);
              }}
            >
              Save
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && <Alert type="error">{error}</Alert>}
        {touched && problems.length > 0 && (
          <Alert type="error" header="The skill cannot be saved yet">
            <ul style={{ margin: 0 }}>
              {problems.map((problem) => (
                <li key={problem}>{problem}</li>
              ))}
            </ul>
          </Alert>
        )}
        <ColumnLayout columns={2}>
          <FormField label="Name" description="Lowercase letters, digits and hyphens; the assistant loads it by name.">
            <Input value={form.name} ariaLabel="Name" onChange={({ detail }) => set('name')(detail.value)} />
          </FormField>
          <FormField label="Display name" description="Optional.">
            <Input
              value={form.displayName}
              ariaLabel="Display name"
              onChange={({ detail }) => set('displayName')(detail.value)}
            />
          </FormField>
        </ColumnLayout>
        <FormField
          label="Description"
          description="When the skill applies. The assistant reads this to decide whether to load the skill."
          stretch
        >
          <Textarea
            value={form.description}
            rows={2}
            ariaLabel="Description"
            onChange={({ detail }) => set('description')(detail.value)}
          />
        </FormField>
        <FormField label="Instructions" description="Markdown. Sent to the assistant when it loads the skill." stretch>
          <Tabs
            activeTabId={tab}
            onChange={({ detail }) => setTab(detail.activeTabId)}
            tabs={[
              {
                id: 'write',
                label: 'Write',
                content: (
                  <Textarea
                    value={form.instructions}
                    rows={18}
                    ariaLabel="Instructions"
                    spellcheck
                    onChange={({ detail }) => set('instructions')(detail.value)}
                  />
                ),
              },
              {
                id: 'preview',
                label: 'Preview',
                content: (
                  <Container>
                    {form.instructions ? (
                      <ReactMarkdown>{form.instructions}</ReactMarkdown>
                    ) : (
                      <Box color="text-status-inactive">Nothing to preview.</Box>
                    )}
                  </Container>
                ),
              },
            ]}
          />
        </FormField>
        <Container
          header={
            <Header
              variant="h3"
              description={`Text and Markdown files the instructions can name, up to ${
                MAX_FILE_BYTES / 1024
              } KB each. The assistant reads one only when the instructions call for it.`}
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button onClick={() => addFile()} iconName="add-plus">
                    Add file
                  </Button>
                  <Button onClick={() => fileInput.current?.click()} iconName="upload">
                    Add from disk
                  </Button>
                  <input
                    ref={fileInput}
                    type="file"
                    accept=".md,.markdown,.txt,.csv,.json,.yaml,.yml"
                    style={{ display: 'none' }}
                    data-testid="skill-reference-file"
                    onChange={(event) => addFromDisk(event.target.files?.[0])}
                  />
                </SpaceBetween>
              }
            >
              Reference files
            </Header>
          }
        >
          <SpaceBetween size="m">
            {form.files.length === 0 && <Box color="text-status-inactive">No reference files.</Box>}
            {form.files.map((file, index) => (
              // Files are edited in place, so their position is their identity here.
              // eslint-disable-next-line react/no-array-index-key
              <SpaceBetween size="xs" key={index}>
                <SpaceBetween direction="horizontal" size="xs" alignItems="end">
                  <FormField label="Path" description={`${byteSize(file.content)} bytes`}>
                    <Input
                      value={file.path}
                      placeholder="reference/checklist.md"
                      ariaLabel={`Path of reference file ${index + 1}`}
                      onChange={({ detail }) => setFile(index, 'path', detail.value)}
                    />
                  </FormField>
                  <Button
                    variant="icon"
                    iconName="remove"
                    ariaLabel={`Remove reference file ${file.path || index + 1}`}
                    onClick={() => removeFile(index)}
                  />
                </SpaceBetween>
                <Textarea
                  value={file.content}
                  rows={6}
                  ariaLabel={`Content of reference file ${file.path || index + 1}`}
                  onChange={({ detail }) => setFile(index, 'content', detail.value)}
                />
              </SpaceBetween>
            ))}
          </SpaceBetween>
        </Container>
        <Container
          header={
            <Header
              variant="h3"
              description={PLAYBOOK_TEXT}
              actions={
                <Toggle
                  checked={Boolean(form.playbook)}
                  onChange={({ detail }) => set('playbook')(detail.checked ? playbookForm(emptyPlaybook()) : null)}
                >
                  This skill is a meeting playbook
                </Toggle>
              }
            >
              Meeting playbook
            </Header>
          }
        >
          {form.playbook ? (
            <PlaybookSectionEditor
              value={form.playbook}
              onChange={set('playbook')}
              displayName={form.displayName || form.name}
              description={form.description}
            />
          ) : (
            <Box color="text-status-inactive">Not a playbook.</Box>
          )}
        </Container>
        <MetadataSummary metadata={form.metadata} />
      </SpaceBetween>
    </Modal>
  );
};

SkillEditorModal.propTypes = {
  visible: PropTypes.bool.isRequired,
  initial: PropTypes.shape({ name: PropTypes.string }),
  saving: PropTypes.bool,
  error: PropTypes.string,
  onCancel: PropTypes.func.isRequired,
  onSave: PropTypes.func.isRequired,
};

export default SkillEditorModal;
