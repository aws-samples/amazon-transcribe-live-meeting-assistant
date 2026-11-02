/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

// The form controls of the configuration editor, chosen from each value's
// schema: text, long text, a list of choices, a number, a switch, a list, a
// map of named entries, and the "N#Label" lists of the summary prompts and the
// chat buttons. Every control marks a value that differs from the shipped one
// and offers to put the shipped one back.

import React, { createContext, useContext, useEffect, useMemo, useState } from 'react';
import PropTypes from 'prop-types';
import {
  AttributeEditor,
  Autosuggest,
  Badge,
  Box,
  Button,
  Container,
  ExpandableSection,
  FormField,
  Header,
  Input,
  RadioGroup,
  Select,
  SpaceBetween,
  Textarea,
  Toggle,
} from '@cloudscape-design/components';

import {
  canRemoveEntry,
  differsAt,
  getAt,
  hasAt,
  isObject,
  isShown,
  memberSchema,
  nextNumber,
  numberedEntries,
  numberedKey,
  orderedProperties,
  pathKey,
  removeAt,
  renameEntry,
  renumbered,
  resetAt,
  setAt,
} from './config-model';

const PATH = PropTypes.arrayOf(PropTypes.oneOfType([PropTypes.string, PropTypes.number]));

// The parts of a section schema the controls read. JSON Schema nests, so a
// nested schema is checked one level down only.
const SCHEMA = PropTypes.shape({
  type: PropTypes.oneOfType([PropTypes.string, PropTypes.arrayOf(PropTypes.string)]),
  title: PropTypes.string,
  description: PropTypes.string,
  enum: PropTypes.arrayOf(PropTypes.oneOfType([PropTypes.string, PropTypes.number, PropTypes.bool])),
  enumLabels: PropTypes.objectOf(PropTypes.string),
  booleanLabels: PropTypes.shape({ true: PropTypes.string, false: PropTypes.string }),
  widget: PropTypes.string,
  format: PropTypes.string,
  rows: PropTypes.number,
  examples: PropTypes.arrayOf(PropTypes.oneOfType([PropTypes.string, PropTypes.number])),
  itemLabel: PropTypes.string,
  keyLabel: PropTypes.string,
  mergeStrategy: PropTypes.string,
  disabledValue: PropTypes.string,
  numberedKeys: PropTypes.bool,
  maximumIsDefault: PropTypes.bool,
  propertyNames: PropTypes.shape({ pattern: PropTypes.string }),
  properties: PropTypes.objectOf(PropTypes.shape({})),
  items: PropTypes.shape({}),
});

/**
 * What every control in one section needs: the section's values and the
 * shipped ones, the deployment settings some fields are shown for, the
 * problems the validator found, and the three ways to change a value.
 */
export const SectionContext = createContext(null);

const useSection = () => useContext(SectionContext);

const typeOf = (schema) => (Array.isArray(schema?.type) ? schema.type.find((t) => t !== 'null') : schema?.type);

const displayValue = (value) => {
  if (value === undefined) return '(none)';
  if (typeof value === 'string') return value === '' ? '(empty)' : value;
  return JSON.stringify(value);
};

// The label, the "Changed" marker, the reset link and the problems of one value.
const FieldFrame = ({
  label,
  description = undefined,
  path,
  children,
  constraintText = undefined,
  secondaryControl = null,
}) => {
  const { values, defaults, reset, problemsAt, readOnly } = useSection();
  const changed = differsAt(defaults, values, path);
  const shipped = hasAt(defaults, path);
  const problems = problemsAt(path);
  const shippedValue = getAt(defaults, path);
  const showDefault = changed && shipped && typeof shippedValue !== 'object' && `${shippedValue}`.length <= 120;
  return (
    <div className={changed ? 'lma-config-field lma-config-field-changed' : 'lma-config-field'}>
      <FormField
        label={
          <SpaceBetween direction="horizontal" size="xs">
            <span>{label}</span>
            {changed && <Badge color="blue">{shipped ? 'Changed' : 'Added'}</Badge>}
          </SpaceBetween>
        }
        description={description}
        info={
          changed && shipped && !readOnly ? (
            <Button variant="inline-link" onClick={() => reset(path)} ariaLabel={`Reset ${label} to the default`}>
              Reset to default
            </Button>
          ) : undefined
        }
        constraintText={constraintText || (showDefault ? `Default: ${displayValue(shippedValue)}` : undefined)}
        errorText={problems.length ? problems.join(' ') : undefined}
        secondaryControl={secondaryControl}
        stretch
      >
        {children}
      </FormField>
    </div>
  );
};

FieldFrame.propTypes = {
  label: PropTypes.node.isRequired,
  description: PropTypes.node,
  path: PATH.isRequired,
  children: PropTypes.node.isRequired,
  constraintText: PropTypes.node,
  secondaryControl: PropTypes.node,
};

const NUMBER_TEXT = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;

// A number typed as text. The text is kept while it is being typed, so "0.0"
// on the way to "0.05" is not rewritten to "0"; a value that is not a number
// is kept as text, which the validator reports.
const NumberInput = ({ value = undefined, onChange, ariaLabel, disabled = false }) => {
  const [draft, setDraft] = useState(value === undefined ? '' : `${value}`);
  useEffect(() => {
    setDraft((current) => (Number(current) === value && current.trim() !== '' ? current : `${value ?? ''}`));
  }, [value]);
  return (
    <Input
      value={draft}
      inputMode="decimal"
      ariaLabel={ariaLabel}
      disabled={disabled}
      onChange={({ detail }) => {
        setDraft(detail.value);
        const text = detail.value.trim();
        onChange(NUMBER_TEXT.test(text) && Number.isFinite(Number(text)) ? Number(text) : detail.value);
      }}
    />
  );
};

NumberInput.propTypes = {
  value: PropTypes.oneOfType([PropTypes.number, PropTypes.string]),
  onChange: PropTypes.func.isRequired,
  ariaLabel: PropTypes.string.isRequired,
  disabled: PropTypes.bool,
};

// The control for one value that is not an object or a list.
const ScalarControl = ({ schema, value = undefined, onChange, ariaLabel }) => {
  const { readOnly } = useSection();
  const type = typeOf(schema);
  if (type === 'boolean') {
    const labels = schema.booleanLabels;
    if (schema.widget === 'radio' && labels) {
      return (
        <RadioGroup
          value={value ? 'true' : 'false'}
          onChange={({ detail }) => onChange(detail.value === 'true')}
          ariaLabel={ariaLabel}
          readOnly={readOnly}
          items={[
            { value: 'false', label: labels.false },
            { value: 'true', label: labels.true },
          ]}
        />
      );
    }
    return (
      <Toggle checked={value === true} onChange={({ detail }) => onChange(detail.checked)} disabled={readOnly}>
        {value === true ? 'On' : 'Off'}
      </Toggle>
    );
  }
  if (Array.isArray(schema.enum)) {
    const options = schema.enum.map((option) => ({
      value: `${option}`,
      label: schema.enumLabels?.[option] || `${option}`,
    }));
    const selected = options.find((option) => option.value === `${value}`) || {
      value: `${value ?? ''}`,
      label: value === undefined ? 'Not set' : `${value} (not one of the choices)`,
    };
    return (
      <Select
        selectedOption={selected}
        options={options}
        ariaLabel={ariaLabel}
        disabled={readOnly}
        onChange={({ detail }) => {
          const chosen = schema.enum.find((option) => `${option}` === detail.selectedOption.value);
          onChange(chosen);
        }}
      />
    );
  }
  if (type === 'number' || type === 'integer') {
    return <NumberInput value={value} onChange={onChange} ariaLabel={ariaLabel} disabled={readOnly} />;
  }
  const text = typeof value === 'string' ? value : `${value ?? ''}`;
  if (schema.format === 'textarea') {
    return (
      <Textarea
        value={text}
        rows={schema.rows || 4}
        ariaLabel={ariaLabel}
        disabled={readOnly}
        onChange={({ detail }) => onChange(detail.value)}
      />
    );
  }
  if (Array.isArray(schema.examples) && schema.examples.length) {
    return (
      <Autosuggest
        value={text}
        options={schema.examples.map((example) => ({ value: `${example}` }))}
        ariaLabel={ariaLabel}
        disabled={readOnly}
        enteredTextLabel={(entered) => `Use: "${entered}"`}
        onChange={({ detail }) => onChange(detail.value)}
      />
    );
  }
  return (
    <Input value={text} ariaLabel={ariaLabel} disabled={readOnly} onChange={({ detail }) => onChange(detail.value)} />
  );
};

ScalarControl.propTypes = {
  schema: SCHEMA.isRequired,
  value: PropTypes.oneOfType([PropTypes.string, PropTypes.number, PropTypes.bool]),
  onChange: PropTypes.func.isRequired,
  ariaLabel: PropTypes.string.isRequired,
};

const ScalarField = ({ schema, path, label, secondaryControl = null }) => {
  const { values, defaults, set } = useSection();
  // A setting whose default is also its limit says so, whatever its value.
  const limit = schema.maximumIsDefault === true ? getAt(defaults, path) : undefined;
  const constraintText =
    typeof limit === 'number'
      ? `At most ${limit}, the limit this deployment sets, which is also the default`
      : undefined;
  return (
    <FieldFrame
      label={label}
      description={schema.description}
      path={path}
      secondaryControl={secondaryControl}
      constraintText={constraintText}
    >
      <ScalarControl
        schema={schema}
        value={getAt(values, path)}
        ariaLabel={typeof label === 'string' ? label : path.join(' ')}
        onChange={(next) => set(path, next)}
      />
    </FieldFrame>
  );
};

ScalarField.propTypes = {
  schema: SCHEMA.isRequired,
  path: PATH.isRequired,
  label: PropTypes.node.isRequired,
  secondaryControl: PropTypes.node,
};

// A list of values, compared and reset as one value, as the store merges it.
const ListField = ({ schema, path, label }) => {
  const { values, set, problemsAt, readOnly } = useSection();
  const items = Array.isArray(getAt(values, path)) ? getAt(values, path) : [];
  const itemSchema = isObject(schema.items) ? schema.items : { type: 'string' };
  const blank = typeOf(itemSchema) === 'number' ? 0 : '';
  return (
    <FieldFrame label={label} description={schema.description} path={path}>
      <AttributeEditor
        items={items.map((item, index) => ({ item, index }))}
        definition={[
          {
            label: schema.itemLabel || 'Item',
            // A render callback AttributeEditor calls for each row, not a component.
            // eslint-disable-next-line react/no-unstable-nested-components
            control: ({ item, index }) => (
              <ScalarControl
                schema={itemSchema}
                value={item}
                ariaLabel={`${schema.itemLabel || 'Item'} ${index + 1}`}
                onChange={(next) =>
                  set(
                    path,
                    items.map((existing, i) => (i === index ? next : existing)),
                  )
                }
              />
            ),
            errorText: ({ index }) => {
              const problems = problemsAt([...path, `item ${index + 1}`]);
              return problems.length ? problems.join(' ') : undefined;
            },
          },
        ]}
        onAddButtonClick={() => set(path, [...items, blank])}
        onRemoveButtonClick={({ detail }) =>
          set(
            path,
            items.filter((_item, i) => i !== detail.itemIndex),
          )
        }
        addButtonText={`Add ${(schema.itemLabel || 'item').toLowerCase()}`}
        removeButtonText="Remove"
        disableAddButton={readOnly}
        isItemRemovable={() => !readOnly}
        empty="Nothing listed."
      />
    </FieldFrame>
  );
};

ListField.propTypes = {
  schema: SCHEMA.isRequired,
  path: PATH.isRequired,
  label: PropTypes.node.isRequired,
};

const blankFor = (schema) => {
  switch (typeOf(schema)) {
    case 'object':
      return {};
    case 'array':
      return [];
    case 'number':
    case 'integer':
      return 0;
    case 'boolean':
      return false;
    default:
      return '';
  }
};

// Typing a name for a new entry of a map, checked before it is added.
const AddEntry = ({ schema, existing, onAdd }) => {
  const { readOnly } = useSection();
  const [name, setName] = useState('');
  const [error, setError] = useState(null);
  const noun = (schema.keyLabel || 'entry').toLowerCase();
  const add = () => {
    const key = name.trim();
    const pattern = schema.propertyNames?.pattern;
    if (!key) {
      setError(`Enter a ${noun} name.`);
    } else if (existing.includes(key)) {
      setError(`There is already a ${noun} named ${key}.`);
    } else if (pattern && !new RegExp(pattern, 'u').test(key)) {
      setError(`A ${noun} name must match ${pattern}.`);
    } else {
      onAdd(key);
      setName('');
      setError(null);
    }
  };
  if (readOnly) return null;
  return (
    <FormField errorText={error} label={`Add a ${noun}`}>
      <SpaceBetween direction="horizontal" size="xs">
        <Input value={name} onChange={({ detail }) => setName(detail.value)} ariaLabel={`New ${noun} name`} />
        <Button onClick={add} ariaLabel={`Add the new ${noun}`}>
          Add
        </Button>
      </SpaceBetween>
    </FormField>
  );
};

AddEntry.propTypes = {
  schema: SCHEMA.isRequired,
  existing: PropTypes.arrayOf(PropTypes.string).isRequired,
  onAdd: PropTypes.func.isRequired,
};

const ChangedBadges = ({ path }) => {
  const { values, defaults } = useSection();
  if (!differsAt(defaults, values, path)) return null;
  return <Badge color="blue">{hasAt(defaults, path) ? 'Changed' : 'Added'}</Badge>;
};

ChangedBadges.propTypes = {
  path: PATH.isRequired,
};

// A group of settings under a heading: an object with named properties, or
// one entry of a map whose entries are objects.
const Group = ({ label, description = undefined, path, actions = null, children }) => {
  const { values, defaults, reset, readOnly } = useSection();
  const changed = differsAt(defaults, values, path);
  return (
    <div className={changed ? 'lma-config-field lma-config-field-changed' : 'lma-config-field'}>
      <ExpandableSection
        // The default variant has no info slot, so the marker sits beside the
        // heading text.
        headerText={
          <SpaceBetween direction="horizontal" size="xs">
            <span>{label}</span>
            <ChangedBadges path={path} />
          </SpaceBetween>
        }
        headerDescription={description}
        headerActions={
          <SpaceBetween direction="horizontal" size="xs">
            {changed && hasAt(defaults, path) && !readOnly && (
              <Button variant="inline-link" onClick={() => reset(path)} ariaLabel={`Reset ${label} to the default`}>
                Reset to default
              </Button>
            )}
            {actions}
          </SpaceBetween>
        }
      >
        <SpaceBetween size="m">{children}</SpaceBetween>
      </ExpandableSection>
    </div>
  );
};

Group.propTypes = {
  label: PropTypes.string.isRequired,
  description: PropTypes.string,
  path: PATH.isRequired,
  actions: PropTypes.node,
  children: PropTypes.node.isRequired,
};

// The editor's controls are mutually recursive.
// eslint-disable-next-line no-use-before-define
const renderNode = ({ schema, path, label, nested = false }) => (
  // eslint-disable-next-line no-use-before-define
  <SchemaNode schema={schema} path={path} label={label} nested={nested} />
);

// An object whose entries are named by the administrator: prices by region,
// notes by service. An entry the release ships can be reset, not removed,
// because the profile is a delta and leaving the entry out would let the
// baseline supply it again.
const MapField = ({ schema, path }) => {
  const { values, defaults, set, remove, readOnly } = useSection();
  const value = getAt(values, path);
  const keys = Object.keys(isObject(value) ? value : {}).sort();
  const removeButton = (key) =>
    !hasAt(defaults, [...path, key]) && !readOnly ? (
      <Button variant="icon" iconName="remove" ariaLabel={`Remove ${key}`} onClick={() => remove([...path, key])} />
    ) : null;
  return (
    <SpaceBetween size="s">
      {keys.length === 0 && <Box color="text-status-inactive">Nothing listed.</Box>}
      {keys.map((key) => {
        const childSchema = memberSchema(schema, key);
        const childPath = [...path, key];
        if (typeOf(childSchema) === 'object') {
          return (
            <Group key={key} label={key} path={childPath} actions={removeButton(key)}>
              {renderNode({ schema: childSchema, path: childPath, label: key, nested: true })}
            </Group>
          );
        }
        return (
          <ScalarField
            key={key}
            schema={childSchema}
            path={childPath}
            label={key}
            secondaryControl={removeButton(key)}
          />
        );
      })}
      <AddEntry
        schema={schema}
        existing={keys}
        onAdd={(key) => set([...path, key], blankFor(memberSchema(schema, key)))}
      />
    </SpaceBetween>
  );
};

MapField.propTypes = {
  schema: SCHEMA.isRequired,
  path: PATH.isRequired,
};

// An object with named properties, in their declared order, each shown only
// while what it depends on holds.
const ObjectFields = ({ schema, path }) => {
  const { values, settings } = useSection();
  const value = getAt(values, path);
  return (
    <SpaceBetween size="l">
      {orderedProperties(schema)
        .filter(([, child]) => isShown(child, value, settings))
        .map(([name, child]) => (
          <React.Fragment key={name}>
            {renderNode({ schema: child, path: [...path, name], label: child.title || name })}
          </React.Fragment>
        ))}
    </SpaceBetween>
  );
};

ObjectFields.propTypes = {
  schema: SCHEMA.isRequired,
  path: PATH.isRequired,
};

// The label of a numbered entry, applied when the field is left rather than
// on every keystroke: the label is part of the entry's key, and renaming a
// shipped entry turns the old key off, which should happen once.
const EntryLabel = ({ entry, label, existing, onRename }) => {
  const { readOnly, values } = useSection();
  const [draft, setDraft] = useState(entry.label);
  const [error, setError] = useState(null);
  // Also on any change to the section, so Undo changes and Discard changes
  // take a half-typed label back too. Typing elsewhere first leaves this field,
  // which applies what was typed.
  useEffect(() => {
    setDraft(entry.label);
    setError(null);
  }, [entry.label, values]);
  const commit = () => {
    const next = draft.trim();
    if (next === entry.label) {
      setError(null);
      return;
    }
    if (!next) {
      setError('A label cannot be empty.');
      return;
    }
    if (existing.includes(numberedKey(entry.number, next))) {
      setError('Another entry already has this label.');
      return;
    }
    setError(null);
    onRename(next);
  };
  return (
    <FormField label={label} errorText={error}>
      <Input
        value={draft}
        disabled={readOnly}
        ariaLabel={`${label} of ${entry.label}`}
        onChange={({ detail }) => setDraft(detail.value)}
        onBlur={commit}
        onKeyDown={({ detail }) => detail.key === 'Enter' && commit()}
      />
    </FormField>
  );
};

EntryLabel.propTypes = {
  entry: PropTypes.shape({ label: PropTypes.string, number: PropTypes.number }).isRequired,
  label: PropTypes.string.isRequired,
  existing: PropTypes.arrayOf(PropTypes.string).isRequired,
  onRename: PropTypes.func.isRequired,
};

/**
 * The "N#Label" lists: the summary prompts and the chat buttons.
 *
 * A shipped summary section that is turned off stays listed, marked, with a way
 * to turn it back on -- which brings back that one section without discarding
 * every other edit. In a "replace" list (the chat buttons) an entry can be
 * removed and the list reordered, and the list is renumbered from one.
 */
const NumberedMap = ({ schema, path }) => {
  const { values, defaults, set, setAll, reset, remove, problemsAt, readOnly } = useSection();
  const value = getAt(values, path) || {};
  const shipped = getAt(defaults, path) || {};
  const entries = numberedEntries(value);
  const replace = schema.mergeStrategy === 'replace';
  const valueSchema = memberSchema(schema, entries[0]?.key || '1#x');
  const keyLabel = schema.keyLabel || 'Label';
  const itemLabel = schema.itemLabel || 'Entry';
  // The keys a label cannot be changed to. A shipped entry that is turned off
  // is not one of them: renaming an entry back to it turns it back on.
  const keys = entries
    .filter((entry) => schema.disabledValue === undefined || entry.value !== schema.disabledValue)
    .map((entry) => entry.key);
  const unique = (base) => {
    const labels = new Set(entries.map((entry) => entry.label));
    let label = base;
    let n = 2;
    while (labels.has(label)) {
      label = `${base} ${n}`;
      n += 1;
    }
    return label;
  };
  const move = (index, offset) => {
    const order = [...entries];
    const [moved] = order.splice(index, 1);
    order.splice(index + offset, 0, moved);
    setAll(path, renumbered(order));
  };
  return (
    <SpaceBetween size="m">
      {entries.length === 0 && <Box color="text-status-inactive">Nothing listed; the shipped entries apply.</Box>}
      {entries.map((entry, index) => {
        const entryPath = [...path, entry.key];
        const isShipped = Object.prototype.hasOwnProperty.call(shipped, entry.key);
        const off = schema.disabledValue !== undefined && entry.value === schema.disabledValue;
        const changed = differsAt(defaults, values, entryPath);
        const problems = problemsAt(entryPath);
        return (
          <div key={entry.key} className={changed ? 'lma-config-field lma-config-field-changed' : 'lma-config-field'}>
            <Container
              header={
                <Header
                  variant="h3"
                  info={
                    <SpaceBetween direction="horizontal" size="xxs">
                      {off && <Badge color="grey">Turned off</Badge>}
                      {!off && changed && <Badge color="blue">{isShipped ? 'Changed' : 'Added'}</Badge>}
                    </SpaceBetween>
                  }
                  actions={
                    readOnly ? null : (
                      <SpaceBetween direction="horizontal" size="xs">
                        {replace && (
                          <>
                            <Button
                              variant="icon"
                              iconName="angle-up"
                              ariaLabel={`Move ${entry.label} up`}
                              disabled={index === 0}
                              onClick={() => move(index, -1)}
                            />
                            <Button
                              variant="icon"
                              iconName="angle-down"
                              ariaLabel={`Move ${entry.label} down`}
                              disabled={index === entries.length - 1}
                              onClick={() => move(index, 1)}
                            />
                          </>
                        )}
                        {isShipped && changed && !replace && (
                          <Button onClick={() => reset(entryPath)}>{off ? 'Turn on' : 'Reset to default'}</Button>
                        )}
                        {isShipped && !off && !replace && schema.disabledValue !== undefined && (
                          <Button onClick={() => set(entryPath, schema.disabledValue)}>Turn off</Button>
                        )}
                        {canRemoveEntry(schema, shipped, entry.key) && (
                          <Button onClick={() => remove(entryPath)}>Remove</Button>
                        )}
                      </SpaceBetween>
                    )
                  }
                >
                  {entry.label}
                </Header>
              }
            >
              {off ? (
                <Box color="text-status-inactive">
                  Turned off: the summary skips this section. Turn it on to bring back the shipped prompt.
                </Box>
              ) : (
                <SpaceBetween size="m">
                  <EntryLabel
                    entry={entry}
                    label={keyLabel}
                    existing={keys}
                    onRename={(label) => setAll(path, renameEntry(schema, shipped, value, entry.key, label))}
                  />
                  <FormField
                    label={valueSchema.title || 'Value'}
                    description={valueSchema.description}
                    errorText={problems.length ? problems.join(' ') : undefined}
                    stretch
                  >
                    <ScalarControl
                      schema={valueSchema}
                      value={entry.value}
                      ariaLabel={`${valueSchema.title || 'Value'} of ${entry.label}`}
                      onChange={(next) => set(entryPath, next)}
                    />
                  </FormField>
                </SpaceBetween>
              )}
            </Container>
          </div>
        );
      })}
      {!readOnly && (
        <Button
          iconName="add-plus"
          onClick={() => set([...path, numberedKey(nextNumber(value, shipped), unique(`New ${itemLabel}`))], '')}
        >
          {`Add ${itemLabel.toLowerCase()}`}
        </Button>
      )}
    </SpaceBetween>
  );
};

NumberedMap.propTypes = {
  schema: SCHEMA.isRequired,
  path: PATH.isRequired,
};

/** The control for any value, chosen from its schema. */
export const SchemaNode = ({ schema, path, label = '', nested = false }) => {
  if (schema.numberedKeys) return <NumberedMap schema={schema} path={path} />;
  const type = typeOf(schema);
  if (type === 'object') {
    const body = schema.properties ? (
      <ObjectFields schema={schema} path={path} />
    ) : (
      <MapField schema={schema} path={path} />
    );
    // The section itself, and a map entry already headed by its Group, need
    // no heading of their own.
    if (path.length === 0 || nested) return body;
    return (
      <Group label={label} description={schema.description} path={path}>
        {body}
      </Group>
    );
  }
  if (type === 'array') return <ListField schema={schema} path={path} label={label} />;
  return <ScalarField schema={schema} path={path} label={label} />;
};

SchemaNode.propTypes = {
  schema: SCHEMA.isRequired,
  path: PATH.isRequired,
  label: PropTypes.string,
  nested: PropTypes.bool,
};

/**
 * The provider every control of one section reads. `onChange` receives the
 * section's whole new values.
 */
export const SectionProvider = ({
  values,
  defaults,
  settings = {},
  problems,
  readOnly = false,
  onChange,
  children,
}) => {
  const context = useMemo(() => {
    const byPath = new Map();
    problems.forEach((problem) => {
      const key = pathKey(problem.path);
      byPath.set(key, [...(byPath.get(key) || []), problem.message]);
    });
    return {
      values,
      defaults,
      settings,
      readOnly,
      problemsAt: (path) => byPath.get(pathKey(path)) || [],
      set: (path, next) => onChange(setAt(values, path, next)),
      setAll: (path, next) => onChange(setAt(values, path, next)),
      reset: (path) => onChange(resetAt(defaults, values, path)),
      remove: (path) => onChange(removeAt(values, path)),
    };
  }, [values, defaults, settings, problems, readOnly, onChange]);
  return <SectionContext.Provider value={context}>{children}</SectionContext.Provider>;
};

SectionProvider.propTypes = {
  values: PropTypes.shape({}).isRequired,
  defaults: PropTypes.shape({}).isRequired,
  settings: PropTypes.shape({}),
  problems: PropTypes.arrayOf(PropTypes.shape({ path: PATH, message: PropTypes.string })).isRequired,
  readOnly: PropTypes.bool,
  onChange: PropTypes.func.isRequired,
  children: PropTypes.node.isRequired,
};
