/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

import React from 'react';
import PropTypes from 'prop-types';
import { Alert, Badge, Button, ExpandableSection, SpaceBetween } from '@cloudscape-design/components';

import { appliesText, changedCount, clone, deepEqual } from './config-model';
import { SchemaNode, SectionProvider } from './fields';
import SectionNotice from './section-notices';
import { formatPath } from './schema-validation';

export const sectionElementId = (name) => `configuration-section-${name}`;

/** One section of the configuration: its heading, its markers and its form. */
const ConfigurationSection = ({
  section,
  values,
  loaded,
  defaults,
  problems,
  settings = {},
  saved = {},
  readOnly = false,
  expanded = false,
  onChange,
}) => {
  const { name, schema, tier } = section;
  const dirty = !deepEqual(values, loaded);
  const differing = changedCount(defaults, values);
  const isDefault = deepEqual(values, defaults);
  const description = [schema.description, appliesText(tier, schema)].filter(Boolean).join(' ');
  return (
    <div id={sectionElementId(name)}>
      <ExpandableSection
        variant="container"
        defaultExpanded={expanded}
        headerText={schema.sectionLabel || schema.title || name}
        headerDescription={description}
        headerInfo={
          <SpaceBetween direction="horizontal" size="xxs">
            {dirty && <Badge color="blue">Unsaved changes</Badge>}
            {differing > 0 && (
              <Badge>{`${differing} ${differing === 1 ? 'setting differs' : 'settings differ'} from default`}</Badge>
            )}
          </SpaceBetween>
        }
        headerActions={
          readOnly ? null : (
            <SpaceBetween direction="horizontal" size="xs">
              <Button onClick={() => onChange(clone(loaded))} disabled={!dirty}>
                Undo changes
              </Button>
              <Button onClick={() => onChange(clone(defaults))} disabled={isDefault}>
                Reset section to defaults
              </Button>
            </SpaceBetween>
          )
        }
      >
        <SpaceBetween size="l">
          <SectionNotice section={name} settings={settings} configuration={saved} />
          {problems.length > 0 && (
            <Alert type="error" header="This section cannot be saved as it is">
              <ul>
                {problems.map((problem) => (
                  <li key={`${formatPath(problem.path)}: ${problem.message}`}>
                    {`${formatPath(problem.path)}: ${problem.message}`}
                  </li>
                ))}
              </ul>
            </Alert>
          )}
          <SectionProvider
            values={values}
            defaults={defaults}
            settings={settings}
            problems={problems}
            readOnly={readOnly}
            onChange={onChange}
          >
            <SchemaNode schema={schema} path={[]} />
          </SectionProvider>
        </SpaceBetween>
      </ExpandableSection>
    </div>
  );
};

ConfigurationSection.propTypes = {
  section: PropTypes.shape({
    name: PropTypes.string.isRequired,
    tier: PropTypes.string,
    schema: PropTypes.shape({
      description: PropTypes.string,
      sectionLabel: PropTypes.string,
      title: PropTypes.string,
    }).isRequired,
  }).isRequired,
  values: PropTypes.shape({}).isRequired,
  loaded: PropTypes.shape({}).isRequired,
  defaults: PropTypes.shape({}).isRequired,
  problems: PropTypes.arrayOf(PropTypes.shape({ message: PropTypes.string })).isRequired,
  settings: PropTypes.shape({}),
  // Every section's values as saved; see SectionNotice.
  saved: PropTypes.shape({}),
  readOnly: PropTypes.bool,
  expanded: PropTypes.bool,
  onChange: PropTypes.func.isRequired,
};

export default ConfigurationSection;
