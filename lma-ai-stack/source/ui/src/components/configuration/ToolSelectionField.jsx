/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Meeting Assistant › Tool selection: per installed MCP server and per skill,
// whether the meeting assistant may use it at all, whether requests made in the
// meeting may, and the reserved always-on flag. The servers, with their
// credentials, are installed on the MCP Servers page, and the skills are written
// on the Skills page; each stays in its own table. This edits only the active
// profile's flags, keyed by server id (meeting_assist.toolSelection.mcpServers)
// and by skill id (meeting_assist.toolSelection.skills). An item with no entry
// takes its default (an MCP server as it did before the profile could name it; a
// skill as enabled for the owner's own requests, and for in-meeting requests as
// the In-meeting requests setting decides, inMeetingSkills under Selected tools),
// so the profile stores only what an administrator changed.

import React, { useContext, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import { Badge, Box, Button, FormField, Select, SpaceBetween, Table, Toggle } from '@cloudscape-design/components';

import { SectionContext } from './fields';
import { differsAt, getAt, isObject } from './config-model';

const logger = new ConsoleLogger('ToolSelectionField');

export const listServersQuery = `
  query ListInstalledMCPServers {
    listInstalledMCPServers {
      ServerId
      Name
      Status
      CredentialsRequired
    }
  }
`;

export const listSkillsQuery = `
  query ListSkills {
    listSkills {
      SkillId
      Name
      DisplayName
    }
  }
`;

const defaultLoadServers = async () => {
  const response = await generateClient().graphql({ query: listServersQuery });
  return response.data.listInstalledMCPServers || [];
};

const defaultLoadSkills = async () => {
  const response = await generateClient().graphql({ query: listSkillsQuery });
  return response.data.listSkills || [];
};

/**
 * `entries` with one flag of one item set to `value`, or removed when `value`
 * is undefined; an item left with no flag is removed, so it behaves as an
 * unlisted one.
 */
export const withFlag = (entries, itemId, flag, value) => {
  const next = { ...(isObject(entries) ? entries : {}) };
  const flags = { ...(isObject(next[itemId]) ? next[itemId] : {}) };
  if (value === undefined) delete flags[flag];
  else flags[flag] = value;
  if (Object.keys(flags).length) next[itemId] = flags;
  else delete next[itemId];
  return next;
};

// The two kinds of item the profile names, and how each is listed.
const KINDS = {
  mcpServers: {
    noun: 'server',
    column: 'Server',
    idOf: (item) => item.ServerId,
    nameOf: (item) => item.Name,
    loadingText: 'Loading the installed MCP servers',
    loadError: 'Could not list the installed MCP servers',
    empty: 'No MCP servers are installed. Install them on the MCP Servers page.',
    missing: 'Not installed',
  },
  skills: {
    noun: 'skill',
    column: 'Skill',
    idOf: (item) => item.SkillId,
    nameOf: (item) => (item.DisplayName ? `${item.DisplayName} (${item.Name})` : item.Name),
    loadingText: 'Loading the skills',
    loadError: 'Could not list the skills',
    empty: 'No skills are defined. Write or import them on the Skills page.',
    missing: 'No such skill',
  },
};

const inMeetingOptions = (noun) => [
  { value: 'default', label: `As In-meeting requests sets for every ${noun}` },
  { value: 'true', label: 'Allowed' },
  { value: 'false', label: 'Not allowed' },
];

/** The flags of one kind of item, as a table of the items the stack lists. */
export const ItemSelectionTable = ({ kind, path, schema, load }) => {
  const { values, defaults, set, readOnly, problemsAt } = useContext(SectionContext);
  const spec = KINDS[kind];
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const itemsPath = [...path, kind];
  const entries = getAt(values, itemsPath) || {};
  const options = inMeetingOptions(spec.noun);

  useEffect(() => {
    let cancelled = false;
    load()
      .then((found) => !cancelled && setItems(found))
      .catch((err) => {
        logger.error(spec.loadError, err);
        if (!cancelled) setError(err.message || spec.loadError);
      });
    return () => {
      cancelled = true;
    };
  }, [load, spec.loadError]);

  const setFlag = (itemId, flag, value) => set(itemsPath, withFlag(entries, itemId, flag, value));
  const knownIds = new Set((items || []).map(spec.idOf));
  const rows = [
    ...(items || []).map((item) => ({ id: spec.idOf(item), name: spec.nameOf(item), item, known: true })),
    // Named in the profile but not on this deployment, as after an import from
    // another one: kept until removed, and unused.
    ...Object.keys(entries)
      .filter((id) => !knownIds.has(id))
      .sort()
      .map((id) => ({ id, name: id, item: {}, known: false })),
  ];
  const problems = problemsAt(itemsPath);
  const changed = differsAt(defaults, values, itemsPath);

  return (
    <div className={changed ? 'lma-config-field lma-config-field-changed' : 'lma-config-field'}>
      <FormField
        label={
          <SpaceBetween direction="horizontal" size="xs">
            <span>{schema.title}</span>
            {changed && <Badge color="blue">Changed</Badge>}
          </SpaceBetween>
        }
        description={schema.description}
        errorText={problems.length ? problems.join(' ') : error || undefined}
        stretch
      >
        <Table
          variant="embedded"
          loading={items === null && !error}
          loadingText={spec.loadingText}
          items={rows}
          trackBy="id"
          empty={<Box color="text-status-inactive">{spec.empty}</Box>}
          columnDefinitions={[
            {
              id: 'name',
              header: spec.column,
              // A render callback the table calls for each row, not a component.
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <span>{row.name}</span>
                  {!row.known && <Badge color="grey">{spec.missing}</Badge>}
                  {row.item.CredentialsRequired && <Badge color="red">Needs credentials</Badge>}
                </SpaceBetween>
              ),
            },
            {
              id: 'enabled',
              header: 'Enabled',
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) => {
                const on = entries[row.id]?.enabled !== false;
                return (
                  <Toggle
                    checked={on}
                    disabled={readOnly}
                    ariaLabel={`Enabled: ${row.name}`}
                    // On is the default, so it is stored by leaving the flag out.
                    onChange={({ detail }) => setFlag(row.id, 'enabled', detail.checked ? undefined : false)}
                  >
                    {on ? 'On' : 'Off'}
                  </Toggle>
                );
              },
            },
            {
              id: 'inMeetingRequests',
              header: 'In-meeting requests',
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) => {
                const flag = entries[row.id]?.inMeetingRequests;
                const selected = options.find((o) => o.value === (flag === undefined ? 'default' : `${flag}`));
                return (
                  <Select
                    selectedOption={selected}
                    options={options}
                    disabled={readOnly}
                    ariaLabel={`In-meeting requests: ${row.name}`}
                    onChange={({ detail }) => {
                      const chosen = detail.selectedOption.value;
                      setFlag(row.id, 'inMeetingRequests', chosen === 'default' ? undefined : chosen === 'true');
                    }}
                  />
                );
              },
            },
            {
              id: 'alwaysOn',
              header: 'Always on (not yet used)',
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) => (
                <Toggle
                  checked={entries[row.id]?.alwaysOn === true}
                  disabled={readOnly}
                  ariaLabel={`Always on: ${row.name}`}
                  onChange={({ detail }) => setFlag(row.id, 'alwaysOn', detail.checked ? true : undefined)}
                >
                  {entries[row.id]?.alwaysOn === true ? 'On' : 'Off'}
                </Toggle>
              ),
            },
            {
              id: 'actions',
              header: '',
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) =>
                !readOnly && Object.prototype.hasOwnProperty.call(entries, row.id) ? (
                  <Button
                    variant="inline-link"
                    ariaLabel={`${row.known ? 'Reset' : 'Remove'} ${row.name}`}
                    onClick={() => {
                      const next = { ...entries };
                      delete next[row.id];
                      set(itemsPath, next);
                    }}
                  >
                    {row.known ? 'Reset' : 'Remove'}
                  </Button>
                ) : null,
            },
          ]}
        />
      </FormField>
    </div>
  );
};

ItemSelectionTable.propTypes = {
  kind: PropTypes.oneOf(Object.keys(KINDS)).isRequired,
  path: PropTypes.arrayOf(PropTypes.oneOfType([PropTypes.string, PropTypes.number])).isRequired,
  schema: PropTypes.shape({ title: PropTypes.string, description: PropTypes.string }).isRequired,
  load: PropTypes.func.isRequired,
};

const ToolSelectionField = ({
  schema,
  path,
  label = '',
  loadServers = defaultLoadServers,
  loadSkills = defaultLoadSkills,
}) => (
  <SpaceBetween size="m">
    <Box>
      <Box variant="awsui-key-label">{label || schema.title}</Box>
      {schema.description && <Box color="text-body-secondary">{schema.description}</Box>}
    </Box>
    <ItemSelectionTable
      kind="mcpServers"
      path={path}
      schema={schema.properties?.mcpServers || { title: 'MCP servers' }}
      load={loadServers}
    />
    <ItemSelectionTable
      kind="skills"
      path={path}
      schema={schema.properties?.skills || { title: 'Skills' }}
      load={loadSkills}
    />
  </SpaceBetween>
);

ToolSelectionField.propTypes = {
  schema: PropTypes.shape({
    title: PropTypes.string,
    description: PropTypes.string,
    properties: PropTypes.shape({
      mcpServers: PropTypes.shape({ title: PropTypes.string, description: PropTypes.string }),
      skills: PropTypes.shape({ title: PropTypes.string, description: PropTypes.string }),
    }),
  }).isRequired,
  path: PropTypes.arrayOf(PropTypes.oneOfType([PropTypes.string, PropTypes.number])).isRequired,
  label: PropTypes.string,
  // List the installed servers and the skills; replaced in tests.
  loadServers: PropTypes.func,
  loadSkills: PropTypes.func,
};

export default ToolSelectionField;
