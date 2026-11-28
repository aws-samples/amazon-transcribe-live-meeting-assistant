/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * Meeting Assistant › Meeting playbooks › Default playbook: a choice among the
 * skills that are playbooks, or none. The list is the Skills page's; a stored
 * id that is no longer a playbook is shown as such, so it can be changed. See
 * docs/meeting-playbooks.md.
 */
import React, { useContext, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { FormField, Select } from '@cloudscape-design/components';

import { SectionContext } from './fields';
import { getAt } from './config-model';
import { isPlaybookSkill } from '../meeting-playbooks/playbook-model';

const LIST_SKILLS = /* GraphQL */ `
  query ListSkills {
    listSkills {
      SkillId
      Name
      DisplayName
      Metadata
    }
  }
`;

const defaultLoadPlaybooks = async () => {
  const response = await generateClient().graphql({ query: LIST_SKILLS });
  return (response.data.listSkills || []).filter(isPlaybookSkill);
};

export const NONE_OPTION = { value: '', label: 'None: no playbook unless one is chosen for the meeting' };

const PlaybookSelectField = ({ schema, path, label = '', loadPlaybooks = defaultLoadPlaybooks }) => {
  const { values, set, readOnly } = useContext(SectionContext);
  const [playbooks, setPlaybooks] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let live = true;
    loadPlaybooks()
      .then((found) => live && setPlaybooks(found))
      .catch((err) => live && setError(err?.message || 'The playbooks could not be listed.'));
    return () => {
      live = false;
    };
  }, [loadPlaybooks]);

  const value = getAt(values, path) || '';
  const options = [
    NONE_OPTION,
    ...(playbooks || []).map((p) => ({ value: p.SkillId, label: p.DisplayName || p.Name, description: p.SkillId })),
  ];
  const selected =
    options.find((o) => o.value === value) ||
    (value ? { value, label: `${value} (not a playbook now: no default is applied)` } : NONE_OPTION);

  return (
    <FormField label={label || schema.title} description={schema.description} errorText={error || undefined}>
      <Select
        selectedOption={selected}
        options={options}
        onChange={({ detail }) => set(path, detail.selectedOption.value)}
        statusType={playbooks === null && !error ? 'loading' : 'finished'}
        disabled={readOnly}
        ariaLabel={label || schema.title}
      />
    </FormField>
  );
};

PlaybookSelectField.propTypes = {
  schema: PropTypes.shape({ title: PropTypes.string, description: PropTypes.string }).isRequired,
  path: PropTypes.arrayOf(PropTypes.oneOfType([PropTypes.string, PropTypes.number])).isRequired,
  label: PropTypes.node,
  // Lists the playbooks; replaced in tests.
  loadPlaybooks: PropTypes.func,
};

export default PlaybookSelectField;
