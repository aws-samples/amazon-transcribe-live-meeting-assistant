/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
import React from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Box,
  Button,
  ButtonDropdown,
  Header,
  Modal,
  SpaceBetween,
  Spinner,
  Table,
} from '@cloudscape-design/components';

import { comparedText, comparisonCsv, comparisonDocument } from './config-text';
import { formatPath } from './schema-validation';

const sectionLabel = (section) => section.schema.sectionLabel || section.schema.title || section.name;

/** What a difference's setting is called: its section, then its title or its path. */
export const settingLabel = (sections, item) => {
  const section = sections.find((candidate) => candidate.name === item.section);
  const title = item.path.length === 1 ? section?.schema.properties?.[item.path[0]]?.title : null;
  return `${section ? sectionLabel(section) : item.section} › ${title || formatPath(item.path)}`;
};

// A column for the setting, then one for each profile compared.
const comparisonColumns = (names, sections) => [
  { id: 'setting', header: 'Setting', cell: (item) => settingLabel(sections, item) },
  ...names.map((name, index) => ({
    id: `profile-${index}`,
    header: name,
    cell: (item) => <Box variant="code">{comparedText(item.values[index])}</Box>,
  })),
];

const download = (text, type, name) => {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = window.document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
};

/**
 * Two or more profiles as saved, setting by setting: a row for each setting
 * they do not all hold alike, and a column for each profile. The rows can be
 * downloaded as CSV or JSON.
 */
const ProfileComparisonModal = ({
  visible,
  names,
  sections,
  comparison = null,
  loading = false,
  error = null,
  note = '',
  onDismiss,
}) => {
  const fileName = `lma-configuration-comparison-${names.join('-')}`.slice(0, 120);
  const exportAs = (format) => {
    if (format === 'csv') {
      download(
        comparisonCsv(names, comparison, (item) => settingLabel(sections, item)),
        'text/csv',
        `${fileName}.csv`,
      );
    } else {
      download(
        `${JSON.stringify(comparisonDocument(names, comparison), null, 2)}\n`,
        'application/json',
        `${fileName}.json`,
      );
    }
  };

  return (
    <Modal
      visible={visible}
      size="max"
      onDismiss={onDismiss}
      header={`Compare ${names.join(', ')}`}
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>
            Close
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Box>{`The profiles as saved, setting by setting.${note ? ` ${note}` : ''}`}</Box>
        {error && <Alert type="error">{error}</Alert>}
        {loading && (
          <Box>
            <Spinner /> Reading the profiles...
          </Box>
        )}
        {comparison && !loading && (
          <Table
            variant="embedded"
            wrapLines
            resizableColumns
            items={comparison}
            trackBy={(item) => `${item.section}:${JSON.stringify(item.path)}`}
            empty={<Box textAlign="center">The profiles hold the same value for every setting.</Box>}
            header={
              <Header
                counter={`(${comparison.length})`}
                actions={
                  <ButtonDropdown
                    disabled={!comparison.length}
                    items={[
                      { id: 'csv', text: 'Export as CSV' },
                      { id: 'json', text: 'Export as JSON' },
                    ]}
                    onItemClick={({ detail }) => exportAs(detail.id)}
                  >
                    Export
                  </ButtonDropdown>
                }
              >
                {comparison.length === 1 ? 'Setting that differs' : 'Settings that differ'}
              </Header>
            }
            columnDefinitions={comparisonColumns(names, sections)}
          />
        )}
      </SpaceBetween>
    </Modal>
  );
};

ProfileComparisonModal.propTypes = {
  visible: PropTypes.bool.isRequired,
  // The profiles compared, in the order of each difference's values.
  names: PropTypes.arrayOf(PropTypes.string).isRequired,
  sections: PropTypes.arrayOf(PropTypes.shape({ name: PropTypes.string, schema: PropTypes.shape({}) })).isRequired,
  comparison: PropTypes.arrayOf(
    PropTypes.shape({ section: PropTypes.string, path: PropTypes.arrayOf(PropTypes.string) }),
  ),
  loading: PropTypes.bool,
  error: PropTypes.string,
  // A sentence said about the comparison, such as that unsaved changes are not in it.
  note: PropTypes.string,
  onDismiss: PropTypes.func.isRequired,
};

export default ProfileComparisonModal;
