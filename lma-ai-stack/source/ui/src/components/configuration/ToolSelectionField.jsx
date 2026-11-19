/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Meeting Assistant › Tool selection: per installed MCP server, whether the
// meeting assistant may use it at all, whether requests made in the meeting may,
// and the reserved always-on flag. The servers themselves, with their
// credentials, are installed on the MCP Servers page and stay in their own
// table; this edits only the active profile's flags, keyed by server id
// (meeting_assist.toolSelection.mcpServers). A server with no entry behaves as
// it did before the profile could name it, so the profile stores only what an
// administrator changed.

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

const defaultLoadServers = async () => {
  const response = await generateClient().graphql({ query: listServersQuery });
  return response.data.listInstalledMCPServers || [];
};

const IN_MEETING_OPTIONS = [
  { value: 'default', label: 'As In-meeting requests sets for every server' },
  { value: 'true', label: 'Allowed' },
  { value: 'false', label: 'Not allowed' },
];

/**
 * `entries` with one flag of one server set to `value`, or removed when `value`
 * is undefined; a server left with no flag is removed, so it behaves as an
 * unlisted one.
 */
export const withFlag = (entries, serverId, flag, value) => {
  const next = { ...(isObject(entries) ? entries : {}) };
  const flags = { ...(isObject(next[serverId]) ? next[serverId] : {}) };
  if (value === undefined) delete flags[flag];
  else flags[flag] = value;
  if (Object.keys(flags).length) next[serverId] = flags;
  else delete next[serverId];
  return next;
};

const ToolSelectionField = ({ schema, path, label = '', loadServers = defaultLoadServers }) => {
  const { values, defaults, set, readOnly, problemsAt } = useContext(SectionContext);
  const [servers, setServers] = useState(null);
  const [error, setError] = useState(null);
  const serversPath = [...path, 'mcpServers'];
  const entries = getAt(values, serversPath) || {};

  useEffect(() => {
    let cancelled = false;
    loadServers()
      .then((found) => !cancelled && setServers(found))
      .catch((err) => {
        logger.error('Could not list the installed MCP servers', err);
        if (!cancelled) setError(err.message || 'Could not list the installed MCP servers');
      });
    return () => {
      cancelled = true;
    };
  }, [loadServers]);

  const setFlag = (serverId, flag, value) => set(serversPath, withFlag(entries, serverId, flag, value));
  const installedIds = new Set((servers || []).map((server) => server.ServerId));
  const rows = [
    ...(servers || []).map((server) => ({ ...server, installed: true })),
    // Named in the profile but not installed here, as after an import from
    // another deployment: kept until removed, and unused.
    ...Object.keys(entries)
      .filter((id) => !installedIds.has(id))
      .sort()
      .map((id) => ({ ServerId: id, Name: id, installed: false })),
  ];
  const mcpSchema = schema.properties?.mcpServers || {};
  const problems = problemsAt(serversPath);
  const changed = differsAt(defaults, values, serversPath);

  return (
    <div className={changed ? 'lma-config-field lma-config-field-changed' : 'lma-config-field'}>
      <FormField
        label={
          <SpaceBetween direction="horizontal" size="xs">
            <span>{label || schema.title}</span>
            {changed && <Badge color="blue">Changed</Badge>}
          </SpaceBetween>
        }
        description={`${schema.description || ''} ${mcpSchema.description || ''}`.trim()}
        errorText={problems.length ? problems.join(' ') : error || undefined}
        stretch
      >
        <Table
          variant="embedded"
          loading={servers === null && !error}
          loadingText="Loading the installed MCP servers"
          items={rows}
          trackBy="ServerId"
          empty={
            <Box color="text-status-inactive">No MCP servers are installed. Install them on the MCP Servers page.</Box>
          }
          columnDefinitions={[
            {
              id: 'name',
              header: 'Server',
              // A render callback the table calls for each row, not a component.
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <span>{row.Name}</span>
                  {!row.installed && <Badge color="grey">Not installed</Badge>}
                  {row.CredentialsRequired && <Badge color="red">Needs credentials</Badge>}
                </SpaceBetween>
              ),
            },
            {
              id: 'enabled',
              header: 'Enabled',
              // A render callback the table calls for each row, not a component.
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) => {
                const on = entries[row.ServerId]?.enabled !== false;
                return (
                  <Toggle
                    checked={on}
                    disabled={readOnly}
                    ariaLabel={`Enabled: ${row.Name}`}
                    // On is the default, so it is stored by leaving the flag out.
                    onChange={({ detail }) => setFlag(row.ServerId, 'enabled', detail.checked ? undefined : false)}
                  >
                    {on ? 'On' : 'Off'}
                  </Toggle>
                );
              },
            },
            {
              id: 'inMeetingRequests',
              header: 'In-meeting requests',
              // A render callback the table calls for each row, not a component.
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) => {
                const flag = entries[row.ServerId]?.inMeetingRequests;
                const selected = IN_MEETING_OPTIONS.find(
                  (o) => o.value === (flag === undefined ? 'default' : `${flag}`),
                );
                return (
                  <Select
                    selectedOption={selected}
                    options={IN_MEETING_OPTIONS}
                    disabled={readOnly}
                    ariaLabel={`In-meeting requests: ${row.Name}`}
                    onChange={({ detail }) => {
                      const chosen = detail.selectedOption.value;
                      setFlag(row.ServerId, 'inMeetingRequests', chosen === 'default' ? undefined : chosen === 'true');
                    }}
                  />
                );
              },
            },
            {
              id: 'alwaysOn',
              header: 'Always on (not yet used)',
              // A render callback the table calls for each row, not a component.
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) => (
                <Toggle
                  checked={entries[row.ServerId]?.alwaysOn === true}
                  disabled={readOnly}
                  ariaLabel={`Always on: ${row.Name}`}
                  onChange={({ detail }) => setFlag(row.ServerId, 'alwaysOn', detail.checked ? true : undefined)}
                >
                  {entries[row.ServerId]?.alwaysOn === true ? 'On' : 'Off'}
                </Toggle>
              ),
            },
            {
              id: 'actions',
              header: '',
              // eslint-disable-next-line react/no-unstable-nested-components
              cell: (row) =>
                !readOnly && Object.prototype.hasOwnProperty.call(entries, row.ServerId) ? (
                  <Button
                    variant="inline-link"
                    ariaLabel={`${row.installed ? 'Reset' : 'Remove'} ${row.Name}`}
                    onClick={() => {
                      const next = { ...entries };
                      delete next[row.ServerId];
                      set(serversPath, next);
                    }}
                  >
                    {row.installed ? 'Reset' : 'Remove'}
                  </Button>
                ) : null,
            },
          ]}
        />
      </FormField>
    </div>
  );
};

ToolSelectionField.propTypes = {
  schema: PropTypes.shape({
    title: PropTypes.string,
    description: PropTypes.string,
    properties: PropTypes.shape({ mcpServers: PropTypes.shape({ description: PropTypes.string }) }),
  }).isRequired,
  path: PropTypes.arrayOf(PropTypes.oneOfType([PropTypes.string, PropTypes.number])).isRequired,
  label: PropTypes.string,
  // Lists the installed servers; replaced in tests.
  loadServers: PropTypes.func,
};

export default ToolSelectionField;
