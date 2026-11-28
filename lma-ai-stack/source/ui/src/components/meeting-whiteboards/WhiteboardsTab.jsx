/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/* eslint-disable react/prop-types */
import React, { useEffect, useState } from 'react';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  ColumnLayout,
  Container,
  FormField,
  Header,
  Input,
  Modal,
  Select,
  SegmentedControl,
  SpaceBetween,
  Textarea,
  Toggle,
} from '@cloudscape-design/components';

import WhiteboardPanel from './WhiteboardPanel';
import WhiteboardVersions from './WhiteboardVersions';
import './WhiteboardsTab.css';
import {
  DEFAULT_TEMPLATE,
  KIND_MARKDOWN,
  KIND_MERMAID,
  LIMITS,
  TEMPLATES,
  withPanelAdded,
  withPanelEdited,
  withPanelHandedBack,
  withPanelMoved,
  withPanelRemoved,
} from './whiteboard-model';

const INSTRUCTIONS_HINT =
  'What the assistant should keep on this whiteboard, for example: track the pricing options discussed.';
const TAB_DESCRIPTION =
  'Named panels the assistant keeps up to date during the meeting, and anyone who can change the meeting can ' +
  'edit. Shown in LMA only.';
const ENDED_HINT =
  'This meeting has ended, so the assistant no longer updates its whiteboards by itself. While the switch is on, ' +
  'Update now updates them from the transcript.';

const templateOption = (template) => ({
  value: template.name,
  label: template.label,
  description: template.description,
});

/** The modal that makes a whiteboard. */
const NewWhiteboardModal = ({ visible, onDismiss, onCreate, assistantAvailable }) => {
  const [title, setTitle] = useState('');
  const [template, setTemplate] = useState(templateOption(TEMPLATES[0]));
  const [instructions, setInstructions] = useState('');
  const [assistant, setAssistant] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!visible) return;
    setTitle('');
    setTemplate(templateOption(TEMPLATES.find((t) => t.name === DEFAULT_TEMPLATE)));
    setInstructions('');
    setAssistant(true);
    setError(null);
  }, [visible]);

  const create = async () => {
    if (title.length > LIMITS.title) {
      setError(`A title is at most ${LIMITS.title} characters.`);
      return;
    }
    if (instructions.length > LIMITS.instructions) {
      setError(`Instructions are at most ${LIMITS.instructions} characters.`);
      return;
    }
    setSaving(true);
    const result = await onCreate({
      Title: title.trim(),
      Template: template.value,
      Instructions: instructions.trim(),
      AssistantUpdates: assistant,
    });
    setSaving(false);
    if (result?.error) setError(result.error);
    else onDismiss(result?.board);
  };

  return (
    <Modal
      visible={visible}
      onDismiss={() => onDismiss()}
      header="New whiteboard"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={() => onDismiss()} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={create} loading={saving}>
              Create
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <FormField label="Title" description="Left empty, the template's name is used.">
          <Input value={title} onChange={({ detail }) => setTitle(detail.value)} ariaLabel="Whiteboard title" />
        </FormField>
        <FormField label="Template">
          <Select
            selectedOption={template}
            onChange={({ detail }) => setTemplate(detail.selectedOption)}
            options={TEMPLATES.map(templateOption)}
            ariaLabel="Template"
          />
        </FormField>
        <FormField
          label="Instructions for the assistant (optional)"
          description={INSTRUCTIONS_HINT}
          constraintText={`At most ${LIMITS.instructions} characters.`}
        >
          <Textarea value={instructions} rows={3} onChange={({ detail }) => setInstructions(detail.value)} />
        </FormField>
        <Toggle checked={assistant} onChange={({ detail }) => setAssistant(detail.checked)}>
          Assistant keeps this whiteboard updated
        </Toggle>
        {!assistantAvailable && (
          <Box variant="small" color="text-status-inactive">
            The assistant&apos;s updates are turned off for this deployment, so the whiteboard is edited by hand.
          </Box>
        )}
        {error && <Alert type="error">{error}</Alert>}
      </SpaceBetween>
    </Modal>
  );
};

/** A modal that asks for one text: a title, instructions or a new panel's title. */
const TextModal = ({ visible, header, label, initial, limit, multiline, confirmText, onDismiss, onConfirm }) => {
  const [value, setValue] = useState(initial || '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  useEffect(() => {
    if (visible) {
      setValue(initial || '');
      setError(null);
    }
  }, [visible, initial]);
  const confirm = async () => {
    if (value.length > limit) {
      setError(`At most ${limit} characters.`);
      return;
    }
    setSaving(true);
    const result = await onConfirm(value.trim());
    setSaving(false);
    if (result?.error) setError(result.error);
    else onDismiss();
  };
  const Field = multiline ? Textarea : Input;
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={header}
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={onDismiss} disabled={saving}>
              Cancel
            </Button>
            <Button variant="primary" onClick={confirm} loading={saving}>
              {confirmText || 'Save'}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="s">
        <FormField label={label} constraintText={`At most ${limit} characters.`}>
          <Field value={value} onChange={({ detail }) => setValue(detail.value)} ariaLabel={label} />
        </FormField>
        {error && <Alert type="error">{error}</Alert>}
      </SpaceBetween>
    </Modal>
  );
};

/** One whiteboard: its header and menu, and its panels in two columns. */
const WhiteboardView = ({ board, readOnly, whiteboards, onAskAboutPanel = undefined, highlightPanelId = null }) => {
  const [dialog, setDialog] = useState(null);
  const [error, setError] = useState(null);
  const [conflict, setConflict] = useState(false);
  const panels = board.Panels || [];
  const assistantOn = board.AssistantUpdates !== false;

  const apply = async (change) => {
    const result = await whiteboards.update(board, change);
    setError(result.error);
    setConflict(result.conflict);
    return result;
  };
  const reload = async () => {
    const message = await whiteboards.reloadBoard(board.WhiteboardId);
    setError(message);
    setConflict(false);
  };

  const onMenu = ({ detail }) => {
    if (detail.id === 'assistant') {
      apply({ AssistantUpdates: !assistantOn });
      return;
    }
    setDialog(detail.id);
  };

  const close = () => setDialog(null);
  const panelLimitReached = panels.length >= LIMITS.panels;

  return (
    <Container
      header={
        <Header
          variant="h2"
          description={
            <SpaceBetween direction="horizontal" size="xs">
              <span>Version {board.Version}</span>
              {board.UpdatedAt && <span>updated {new Date(board.UpdatedAt).toLocaleString()}</span>}
              {board.UpdatedBy && <span>by {board.UpdatedBy}</span>}
              {!assistantOn && <Badge color="grey">Assistant updates off</Badge>}
            </SpaceBetween>
          }
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => setDialog('versions')}>Versions</Button>
              {!readOnly && (
                <ButtonDropdown
                  onItemClick={onMenu}
                  expandToViewport
                  items={[
                    { id: 'addMarkdown', text: 'Add a Markdown panel', disabled: panelLimitReached },
                    { id: 'addDiagram', text: 'Add a diagram panel', disabled: panelLimitReached },
                    { id: 'rename', text: 'Rename' },
                    { id: 'instructions', text: 'Instructions for the assistant' },
                    {
                      id: 'assistant',
                      text: assistantOn ? 'Stop the assistant updating it' : 'Let the assistant update it',
                    },
                    { id: 'delete', text: 'Delete whiteboard' },
                  ]}
                >
                  Actions
                </ButtonDropdown>
              )}
            </SpaceBetween>
          }
        >
          {board.Title}
        </Header>
      }
    >
      <SpaceBetween size="m">
        {board.Instructions && (
          <Box variant="small" color="text-status-inactive">
            Instructions: {board.Instructions}
          </Box>
        )}
        {error && (
          <Alert
            type={conflict ? 'warning' : 'error'}
            action={conflict ? <Button onClick={reload}>Reload</Button> : null}
            dismissible
            onDismiss={() => setError(null)}
          >
            {error}
          </Alert>
        )}
        {panels.length === 0 ? (
          <Box color="text-status-inactive">This whiteboard has no panels.</Box>
        ) : (
          <ColumnLayout columns={2}>
            {panels.map((panel, index) => (
              <WhiteboardPanel
                key={panel.PanelId}
                panel={panel}
                isFirst={index === 0}
                isLast={index === panels.length - 1}
                readOnly={readOnly}
                assistantOn={assistantOn}
                onSave={(change) => apply({ Panels: withPanelEdited(panels, panel.PanelId, change) })}
                onHandBack={() => apply({ Panels: withPanelHandedBack(panels, panel.PanelId) })}
                onMove={(delta) => apply({ Panels: withPanelMoved(panels, panel.PanelId, delta) })}
                onRemove={() => apply({ Panels: withPanelRemoved(panels, panel.PanelId) })}
                onAsk={onAskAboutPanel ? () => onAskAboutPanel(board, panel) : undefined}
                highlighted={highlightPanelId === panel.PanelId}
              />
            ))}
          </ColumnLayout>
        )}
      </SpaceBetween>

      {dialog === 'rename' && (
        <TextModal
          visible
          header="Rename whiteboard"
          label="Title"
          initial={board.Title}
          limit={LIMITS.title}
          onDismiss={close}
          onConfirm={(value) => apply({ Title: value })}
        />
      )}
      {dialog === 'instructions' && (
        <TextModal
          visible
          header="Instructions for the assistant"
          label="What the assistant should keep on this whiteboard"
          initial={board.Instructions}
          limit={LIMITS.instructions}
          multiline
          onDismiss={close}
          onConfirm={(value) => apply({ Instructions: value })}
        />
      )}
      {['addMarkdown', 'addDiagram']
        .filter((id) => dialog === id)
        .map((id) => (
          <TextModal
            key={id}
            visible
            header={id === 'addDiagram' ? 'Add a diagram panel' : 'Add a Markdown panel'}
            label="Panel title"
            initial=""
            limit={LIMITS.panelTitle}
            confirmText="Add"
            onDismiss={close}
            onConfirm={(value) =>
              value
                ? apply({
                    Panels: withPanelAdded(panels, {
                      Title: value,
                      Kind: id === 'addDiagram' ? KIND_MERMAID : KIND_MARKDOWN,
                    }),
                  })
                : { error: 'A panel needs a title.' }
            }
          />
        ))}
      {dialog === 'delete' && (
        <Modal
          visible
          onDismiss={close}
          header="Delete whiteboard"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={close}>Cancel</Button>
                <Button
                  variant="primary"
                  onClick={async () => {
                    const result = await whiteboards.remove(board);
                    setError(result.error);
                    close();
                  }}
                >
                  Delete
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          Delete &quot;{board.Title}&quot; and every earlier version of it, for everyone who can see this meeting? This
          cannot be undone.
        </Modal>
      )}
      {dialog === 'versions' && (
        <WhiteboardVersions
          board={board}
          visible
          readOnly={readOnly}
          onDismiss={close}
          listVersions={whiteboards.listVersions}
          readVersion={whiteboards.readVersion}
          onRestore={(version) => apply({ RestoreVersion: version })}
        />
      )}
    </Container>
  );
};

/**
 * The meeting page's Whiteboards tab. `whiteboards` is use-meeting-whiteboards'
 * state, held by the page so the tab's label can count them; `readOnly` is the
 * page's own rule for who may change the meeting, which the server enforces.
 * `assistantEnabled` is the meeting's switch as the page last read it; a change
 * made here is shown from the moment it is stored, and the page's copy is
 * followed again once it moves. `meetingEnded` says the meeting has ended.
 */
const WhiteboardsTab = ({
  whiteboards,
  readOnly = false,
  assistantEnabled = undefined,
  meetingEnded = false,
  selectRequest = null,
  onAskAboutPanel = undefined,
}) => {
  const [selectedId, setSelectedId] = useState(null);
  // A whiteboard the page asked to show (a pinned answer, the assistant's
  // change): selected, and its panel picked out for a few seconds.
  const [highlight, setHighlight] = useState(null);
  useEffect(() => {
    if (!selectRequest?.whiteboardId) return undefined;
    setSelectedId(selectRequest.whiteboardId);
    setHighlight(selectRequest.panelId || null);
    const timer = setTimeout(() => setHighlight(null), 4000);
    return () => clearTimeout(timer);
  }, [selectRequest]);
  useEffect(() => {
    if (!highlight) return;
    const element = document.querySelector(`.whiteboard-panel[data-panel-id="${CSS.escape(highlight)}"]`);
    element?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
  }, [highlight, selectedId]);
  const [creating, setCreating] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [notice, setNotice] = useState(null);
  const [chosen, setChosen] = useState(undefined);
  useEffect(() => setChosen(undefined), [assistantEnabled]);
  const { items, writable, assistantAvailable, maxWhiteboards } = whiteboards;
  const enabled = chosen ?? assistantEnabled ?? whiteboards.whiteboardsEnabled;
  const canChange = !readOnly && writable;
  const selected = items.find((b) => b.WhiteboardId === selectedId) || items[0] || null;

  const toggle = async (checked) => {
    const message = await whiteboards.setAssistantEnabled(checked);
    if (!message) setChosen(checked);
    setNotice(message ? { type: 'error', text: message } : null);
  };
  const updateNow = async () => {
    setUpdating(true);
    const result = await whiteboards.requestUpdate();
    setUpdating(false);
    let shown = { type: 'success', text: result.message || 'The whiteboards were updated.' };
    if (result.error) shown = { type: 'error', text: result.error };
    else if (result.pending) shown = { type: 'info', text: result.message };
    setNotice(shown);
  };

  return (
    <SpaceBetween size="m">
      <Header
        variant="h2"
        counter={items.length ? `(${items.length})` : undefined}
        description={TAB_DESCRIPTION}
        actions={
          canChange && (
            <SpaceBetween direction="horizontal" size="xs">
              {assistantAvailable && enabled && (
                <Button onClick={updateNow} loading={updating}>
                  Update now
                </Button>
              )}
              <Button variant="primary" onClick={() => setCreating(true)} disabled={items.length >= maxWhiteboards}>
                New whiteboard
              </Button>
            </SpaceBetween>
          )
        }
      >
        Whiteboards
      </Header>
      {!writable && (
        <Alert type="info">
          This meeting&apos;s transcript has been deleted. Whiteboards are drawn from the transcript, so they were
          deleted with it.
        </Alert>
      )}
      {canChange && assistantAvailable && (
        <Toggle checked={Boolean(enabled)} onChange={({ detail }) => toggle(detail.checked)}>
          Assistant keeps whiteboards updated (each update is a model call billed to the meeting)
        </Toggle>
      )}
      {canChange && assistantAvailable && meetingEnded && (
        <Box variant="small" color="text-status-inactive">
          {ENDED_HINT}
        </Box>
      )}
      {notice && (
        <Alert type={notice.type} dismissible onDismiss={() => setNotice(null)}>
          {notice.text}
        </Alert>
      )}
      {whiteboards.error && <Alert type="error">{whiteboards.error}</Alert>}
      {items.length === 0 && writable && !whiteboards.loading && (
        <Box color="text-status-inactive" textAlign="center" padding="l">
          {canChange
            ? 'No whiteboards yet. Make one, or turn on the assistant and it makes one from the meeting.'
            : 'No whiteboards yet.'}
        </Box>
      )}
      {items.length > 1 && (
        <SegmentedControl
          selectedId={selected?.WhiteboardId}
          onChange={({ detail }) => setSelectedId(detail.selectedId)}
          label="Whiteboard"
          options={items.map((b) => ({ id: b.WhiteboardId, text: b.Title }))}
        />
      )}
      {selected && (
        <WhiteboardView
          key={selected.WhiteboardId}
          board={selected}
          readOnly={!canChange}
          whiteboards={whiteboards}
          onAskAboutPanel={onAskAboutPanel}
          highlightPanelId={highlight}
        />
      )}
      {creating && (
        <NewWhiteboardModal
          visible
          assistantAvailable={assistantAvailable}
          onCreate={whiteboards.create}
          onDismiss={(board) => {
            setCreating(false);
            if (board?.WhiteboardId) setSelectedId(board.WhiteboardId);
          }}
        />
      )}
    </SpaceBetween>
  );
};

export default WhiteboardsTab;
