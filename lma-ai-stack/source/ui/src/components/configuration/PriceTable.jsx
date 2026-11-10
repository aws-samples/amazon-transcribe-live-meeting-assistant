/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The Meeting Cost Prices section of the Configuration page, as a price table.
 *
 * Modelled on the IDP Accelerator's Pricing page: every price of one region in
 * a table grouped by service, each editable, with the shipped price and its
 * source beside it, a search, per-price reset, and export and import of the
 * whole table. It edits the section's values through the same SectionContext
 * every other control on the page uses, so a change is an unsaved change of the
 * page -- marked, checked against the section's schema, and saved, with a
 * comment, as one revision of the active profile, like any other.
 */
import React, { useContext, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import {
  Alert,
  Badge,
  Box,
  Button,
  ButtonDropdown,
  ExpandableSection,
  FormField,
  Input,
  Link,
  Popover,
  Select,
  SpaceBetween,
  Table,
  TextFilter,
} from '@cloudscape-design/components';

import awsExports from '../../aws-exports';
import { formatUnitPrice, readablePrice } from '../common/call-cost';
import { SchemaNode, NumberInput, SectionContext } from './fields';
import {
  DEFAULT_REGION,
  filterRows,
  groupRows,
  initialRegion,
  priceRows,
  pricingDocument,
  pricingText,
  readPricingFile,
  regionOptions,
  sourceSummary,
} from './price-table';

const SERVICE_PATTERN = /^[a-z0-9-]+\/.+$/u;
const UNIT_PATTERN = /^[A-Za-z][A-Za-z0-9]*$/u;
const NUMBER_TEXT = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;

// The section's settings other than the prices and their sources, shown below
// the table as the rest of the page shows settings.
const otherSettingsSchema = (schema) => {
  const { regions: _regions, sources: _sources, ...rest } = schema.properties || {};
  return { type: 'object', properties: rest };
};

const SourceCell = ({ row }) => {
  if (row.changed) {
    const shipped =
      row.shippedPrice === undefined
        ? 'LMA ships no price for this unit'
        : `Shipped: ${formatUnitPrice(row.shippedPrice)}`;
    return (
      <SpaceBetween direction="horizontal" size="xxs">
        <Badge color="blue">{row.added && row.shippedPrice === undefined ? 'Added' : 'Your price'}</Badge>
        <Box variant="small">{shipped}</Box>
      </SpaceBetween>
    );
  }
  if (!row.source) return <Box color="text-status-inactive">No source recorded</Box>;
  return (
    <Popover
      dismissButton={false}
      position="top"
      size="large"
      triggerType="text"
      header="Where this price comes from"
      content={
        <SpaceBetween size="xs">
          <Box>{sourceSummary(row.source)}</Box>
          {row.source.url && (
            <Link external href={row.source.url}>
              Pricing page
            </Link>
          )}
        </SpaceBetween>
      }
    >
      {`${sourceSummary(row.source).split(' (')[0]}${row.source.retrieved ? `, ${row.source.retrieved}` : ''}`}
    </Popover>
  );
};

SourceCell.propTypes = {
  row: PropTypes.shape({
    changed: PropTypes.bool,
    added: PropTypes.bool,
    shippedPrice: PropTypes.number,
    source: PropTypes.shape({ url: PropTypes.string, retrieved: PropTypes.string }),
  }).isRequired,
};

// Adding a price the shipped table does not have, for the region shown.
const AddPrice = ({ region, onAdd, onCancel }) => {
  const [service, setService] = useState('');
  const [unit, setUnit] = useState('');
  const [price, setPrice] = useState('');
  const [error, setError] = useState(null);
  const add = () => {
    if (!SERVICE_PATTERN.test(service.trim())) {
      setError('A service is written <service>/<api-or-model>, for example bedrock/us.amazon.nova-pro-v1:0.');
    } else if (!UNIT_PATTERN.test(unit.trim())) {
      setError('A unit is one word of letters and digits, for example inputTokens or seconds.');
    } else if (!NUMBER_TEXT.test(price.trim()) || Number(price) < 0) {
      setError('A price is a number of US dollars per unit, at least 0.');
    } else {
      onAdd(service.trim(), unit.trim(), Number(price));
    }
  };
  return (
    <FormField
      label={`Add a price for ${region === DEFAULT_REGION ? 'the base region' : region}`}
      errorText={error}
      stretch
    >
      <SpaceBetween direction="horizontal" size="xs">
        <Input
          value={service}
          onChange={({ detail }) => setService(detail.value)}
          ariaLabel="Service"
          placeholder="service/api-or-model"
        />
        <Input value={unit} onChange={({ detail }) => setUnit(detail.value)} ariaLabel="Unit" placeholder="unit" />
        <Input
          value={price}
          onChange={({ detail }) => setPrice(detail.value)}
          ariaLabel="Price"
          placeholder="USD per unit"
        />
        <Button onClick={add}>Add</Button>
        <Button variant="link" onClick={onCancel}>
          Cancel
        </Button>
      </SpaceBetween>
    </FormField>
  );
};

AddPrice.propTypes = {
  region: PropTypes.string.isRequired,
  onAdd: PropTypes.func.isRequired,
  onCancel: PropTypes.func.isRequired,
};

// The table's columns. Cell renderers are plain functions of a row, defined at
// module scope so the columns are not rebuilt as new component types each render.
const renderModel = (row) => (
  <SpaceBetween size="xxs">
    <span>{row.model || row.service}</span>
    {row.inherited && (
      <Box variant="small" color="text-body-secondary">
        Base region price
      </Box>
    )}
  </SpaceBetween>
);

const renderPrice = (row, { set, problemsAt, readOnly }) => {
  const problems = problemsAt(row.path);
  return (
    <SpaceBetween size="xxs">
      <NumberInput
        value={row.price}
        ariaLabel={`Price of ${row.unitText} of ${row.service}`}
        disabled={readOnly}
        onChange={(next) => set(row.path, next)}
      />
      {problems.length > 0 && <Box color="text-status-error">{problems.join(' ')}</Box>}
    </SpaceBetween>
  );
};

const renderReset = (row, { reset, readOnly }) =>
  row.changed && !readOnly ? (
    <Button
      variant="inline-link"
      onClick={() => reset(row.resetPath)}
      ariaLabel={`Reset the price of ${row.unitText} of ${row.service} to the default`}
    >
      {row.added && row.shippedPrice === undefined ? 'Remove' : 'Reset to default'}
    </Button>
  ) : null;

const renderSource = (row) => <SourceCell row={row} />;

const priceColumns = (handlers) => [
  { id: 'model', header: 'API or model', cell: renderModel },
  { id: 'unit', header: 'Unit', cell: (row) => row.unitText },
  { id: 'price', header: 'Price (USD per unit)', cell: (row) => renderPrice(row, handlers) },
  { id: 'readable', header: 'As listed', cell: (row) => readablePrice(row.unit, row.price) },
  { id: 'source', header: 'Source', cell: renderSource },
  { id: 'actions', header: '', cell: (row) => renderReset(row, handlers) },
];

const download = (text, name, type) => {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = window.document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
};

/** The price table for the call_pricing section. */
const PriceTable = ({ schema, deploymentRegion = awsExports.aws_project_region || '' }) => {
  const { values, defaults, settings, set, reset, problemsAt, readOnly } = useContext(SectionContext);
  const [region, setRegion] = useState(() => initialRegion(defaults, deploymentRegion));
  const [filter, setFilter] = useState('');
  const [open, setOpen] = useState({});
  const [adding, setAdding] = useState(false);
  const [message, setMessage] = useState(null);
  const fileInput = useRef(null);

  const options = useMemo(
    () => regionOptions(values, defaults, deploymentRegion),
    [values, defaults, deploymentRegion],
  );
  const rows = useMemo(() => priceRows(values, defaults, region), [values, defaults, region]);
  const groups = groupRows(filterRows(rows, filter));
  const changed = rows.filter((row) => row.changed).length;

  const exportPrices = (format) => {
    const now = new Date().toISOString();
    const document = pricingDocument({ values, stackName: settings?.StackName, now });
    const name = `lma-prices-${settings?.StackName || 'export'}-${now.slice(0, 10)}.${format}`;
    download(pricingText(document, format), name, format === 'yaml' ? 'text/yaml' : 'application/json');
    setMessage({
      type: 'info',
      text: `Exported every price as this page shows it, unsaved changes included, to ${name}.`,
    });
  };

  const importPrices = async (file) => {
    if (!file) return;
    try {
      set([], readPricingFile(await file.text(), defaults));
      setMessage({
        type: 'info',
        text:
          `Read the prices in ${file.name}. Nothing is saved yet: the prices that differ from the shipped ones are ` +
          'marked; review them, then save the configuration, or undo the changes.',
      });
    } catch (err) {
      setMessage({ type: 'error', text: `Could not import ${file.name}: ${err.message}` });
    }
  };

  const columns = priceColumns({ set, reset, problemsAt, readOnly });

  return (
    <SpaceBetween size="l">
      <Box variant="p">
        Each meeting&apos;s estimated cost multiplies the usage recorded for it by these prices. The shipped prices are
        public on-demand list prices; each says where it was read. Change one to price your meetings at your own rate. A
        change is saved with the rest of this page, into the active profile, and can be reset to the shipped price.
      </Box>
      {message && (
        <Alert type={message.type} dismissible onDismiss={() => setMessage(null)}>
          {message.text}
        </Alert>
      )}
      <SpaceBetween direction="horizontal" size="s" alignItems="end">
        <FormField label="Region" description="The prices an estimate uses for usage in this region">
          <Select
            selectedOption={options.find((option) => option.value === region) || options[0]}
            options={options}
            onChange={({ detail }) => setRegion(detail.selectedOption.value)}
            ariaLabel="Region"
          />
        </FormField>
        <FormField label="Find a price">
          <TextFilter
            filteringText={filter}
            filteringPlaceholder="Service, model or unit"
            filteringAriaLabel="Find a price"
            onChange={({ detail }) => setFilter(detail.filteringText)}
            countText={filter ? `${groups.reduce((n, g) => n + g.rows.length, 0)} matches` : undefined}
          />
        </FormField>
        <ButtonDropdown
          items={[
            { id: 'json', text: 'As JSON' },
            { id: 'yaml', text: 'As YAML' },
          ]}
          onItemClick={({ detail }) => exportPrices(detail.id)}
        >
          Export prices
        </ButtonDropdown>
        {!readOnly && (
          <>
            <Button onClick={() => fileInput.current?.click()}>Import prices</Button>
            <input
              ref={fileInput}
              type="file"
              accept=".json,.yaml,.yml,application/json"
              aria-label="Price file to import"
              style={{ display: 'none' }}
              onChange={(event) => {
                importPrices(event.target.files?.[0]);
                // eslint-disable-next-line no-param-reassign -- lets the same file be chosen again
                event.target.value = '';
              }}
            />
            <Button iconName="add-plus" onClick={() => setAdding(true)} disabled={adding}>
              Add a price
            </Button>
          </>
        )}
      </SpaceBetween>
      {adding && (
        <AddPrice
          region={region}
          onCancel={() => setAdding(false)}
          onAdd={(service, unit, price) => {
            set(['regions', region, service, unit], price);
            setAdding(false);
            setFilter(service);
          }}
        />
      )}
      <Box variant="small" color="text-body-secondary">
        {changed
          ? `${changed} price${changed === 1 ? ' differs' : 's differ'} from the shipped ones in this region.`
          : 'Every price in this region is the shipped one.'}
      </Box>
      {groups.length === 0 && <Box color="text-status-inactive">No price matches.</Box>}
      {groups.map((group) => (
        <ExpandableSection
          key={group.family}
          expanded={Boolean(filter) || open[group.family] !== false}
          onChange={({ detail }) => setOpen((current) => ({ ...current, [group.family]: detail.expanded }))}
          headerText={
            <SpaceBetween direction="horizontal" size="xs">
              <span>{`${group.family} prices`}</span>
              {group.changed > 0 && <Badge color="blue">{`${group.changed} changed`}</Badge>}
            </SpaceBetween>
          }
          headerDescription={`${group.rows.length} price${group.rows.length === 1 ? '' : 's'}`}
        >
          <Table
            variant="embedded"
            wrapLines
            trackBy="id"
            items={group.rows}
            columnDefinitions={columns}
            ariaLabels={{ tableLabel: `${group.family} prices` }}
          />
        </ExpandableSection>
      ))}
      <ExpandableSection headerText="Other settings of the estimate" variant="footer">
        <SchemaNode schema={otherSettingsSchema(schema)} path={[]} />
      </ExpandableSection>
    </SpaceBetween>
  );
};

PriceTable.propTypes = {
  schema: PropTypes.shape({ properties: PropTypes.shape({}) }).isRequired,
  deploymentRegion: PropTypes.string,
};

export default PriceTable;
