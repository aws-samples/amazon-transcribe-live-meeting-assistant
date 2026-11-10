/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The rules of the Meeting Cost Prices editor, kept free of React so they are
 * tested directly: which prices a region uses, which of them differ from the
 * shipped ones, where each shipped price came from, and what an exported or
 * imported price file holds.
 *
 * The section stores prices as regions › service › unit, where "default" holds
 * the base region's prices and every other region lists only its own. The
 * editor shows one region at a time as a flat table, as the IDP Accelerator's
 * Pricing page does, with each price's source beside it.
 */
import { CORE_SCHEMA, dump, load } from 'js-yaml';

import { SERVICE_NAMES, unitLabel } from '../common/call-cost';
import { deepMerge, getAt, isObject } from './config-model';

export const DEFAULT_REGION = 'default';
export const PRICING_FORMAT = 'lma-call-pricing';
export const PRICING_FORMAT_VERSION = 1;

const SERVICE_ORDER = Object.keys(SERVICE_NAMES);

/** The AWS service a price-table key belongs to, as a heading. */
export const familyOf = (service) => {
  const prefix = `${service}`.split('/')[0];
  return SERVICE_NAMES[prefix] || prefix;
};

const familyRank = (service) => {
  const index = SERVICE_ORDER.indexOf(`${service}`.split('/')[0]);
  return index === -1 ? SERVICE_ORDER.length : index;
};

const layer = (table, region) => (isObject(getAt(table, [region])) ? getAt(table, [region]) : {});

/** ``default`` with ``region`` laid over it: the prices or sources ``region`` uses. */
export const regionLayer = (table, region) =>
  region === DEFAULT_REGION
    ? layer(table, DEFAULT_REGION)
    : deepMerge(layer(table, DEFAULT_REGION), layer(table, region));

/**
 * The regions the editor offers, "default" first, the deployment's own region
 * marked. The deployment's region is offered even when the table lists no
 * prices of its own for it, because that is the region its estimates use.
 */
export const regionOptions = (values, defaults, deploymentRegion = '') => {
  const base = values?.baseRegion || defaults?.baseRegion || 'us-east-1';
  const listed = new Set([...Object.keys(values?.regions || {}), ...Object.keys(defaults?.regions || {})]);
  listed.delete(DEFAULT_REGION);
  if (deploymentRegion && deploymentRegion !== base) listed.add(deploymentRegion);
  const mark = (region) => (region === deploymentRegion ? ' (this deployment)' : '');
  return [
    {
      value: DEFAULT_REGION,
      label: `${base}${mark(base)}`,
      description: 'The base region; its prices apply wherever a region lists none of its own',
    },
    ...[...listed].sort().map((region) => ({
      value: region,
      label: `${region}${mark(region)}`,
      description: Object.keys(layer(defaults?.regions, region)).length
        ? 'Prices read for this region, over the base region’s'
        : 'No shipped prices of its own: the base region’s apply',
    })),
  ];
};

/** The region the editor opens on: the deployment's, as its estimates use it. */
export const initialRegion = (values, deploymentRegion = '') => {
  const base = values?.baseRegion || 'us-east-1';
  if (!deploymentRegion || deploymentRegion === base) return DEFAULT_REGION;
  return deploymentRegion;
};

/**
 * One row per price ``region`` uses. Each row knows the path a change to it is
 * stored at (always under the region shown, so a change made while viewing a
 * region applies to that region only), the shipped price, and its source.
 */
export const priceRows = (values, defaults, region) => {
  const effective = regionLayer(values?.regions, region);
  const shipped = regionLayer(defaults?.regions, region);
  const sources = regionLayer(defaults?.sources, region);
  const own = layer(values?.regions, region);
  const rows = [];
  Object.keys(effective).forEach((service) => {
    const units = isObject(effective[service]) ? effective[service] : {};
    Object.keys(units).forEach((unit) => {
      const path = ['regions', region, service, unit];
      const shippedPrice = getAt(shipped, [service, unit]);
      const price = units[unit];
      const inherited = region !== DEFAULT_REGION && getAt(own, [service, unit]) === undefined;
      rows.push({
        id: `${region}|${service}|${unit}`,
        family: familyOf(service),
        service,
        model: `${service}`.split('/').slice(1).join('/'),
        unit,
        unitText: unitLabel(unit),
        price,
        path,
        shippedPrice: typeof shippedPrice === 'number' ? shippedPrice : undefined,
        source: isObject(sources[service]) ? sources[service] : undefined,
        // Inherited: shown for this region, stored under the base region, so a
        // change to it there is put back there.
        inherited,
        resetPath: inherited ? ['regions', DEFAULT_REGION, service, unit] : path,
        changed: price !== shippedPrice,
        // A price the release does not ship at this path: one an administrator added.
        added: getAt(defaults, path) === undefined && getAt(values, path) !== undefined,
      });
    });
  });
  return rows.sort(
    (a, b) =>
      familyRank(a.service) - familyRank(b.service) ||
      a.service.localeCompare(b.service) ||
      a.unit.localeCompare(b.unit),
  );
};

/** The rows whose service, model, unit or source mention ``text``. */
export const filterRows = (rows, text) => {
  const needle = `${text || ''}`.trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter((row) =>
    [row.family, row.service, row.unit, row.unitText, row.source?.usageType, row.source?.offer]
      .filter(Boolean)
      .some((value) => `${value}`.toLowerCase().includes(needle)),
  );
};

/** The rows grouped under their service's heading, in the price table's order. */
export const groupRows = (rows) => {
  const groups = new Map();
  rows.forEach((row) => {
    if (!groups.has(row.family)) groups.set(row.family, []);
    groups.get(row.family).push(row);
  });
  return [...groups.entries()].map(([family, items]) => ({
    family,
    rows: items,
    changed: items.filter((row) => row.changed).length,
  }));
};

/** How a source's basis is named in the table. */
export const BASIS_LABELS = {
  'price-list-api': 'AWS Price List API',
  'pricing-page': 'AWS pricing page',
  estimate: 'Estimate',
};

/** The source of a shipped price in one line, for the table and its popover. */
export const sourceSummary = (source) => {
  if (!isObject(source)) return '';
  const parts = [];
  if (source.offer) parts.push(`offer ${source.offer}${source.offerVersion ? ` version ${source.offerVersion}` : ''}`);
  if (source.usageType) parts.push(`usage type ${source.usageType}`);
  if (source.retrieved) parts.push(`read ${source.retrieved}`);
  const head = BASIS_LABELS[source.basis] || 'Source';
  return `${head}${parts.length ? ` (${parts.join(', ')})` : ''}${source.detail ? `. ${source.detail}` : ''}`;
};

/** The file Export prices downloads: the whole price table as the page holds it. */
export const pricingDocument = ({ values, stackName, now }) => ({
  format: PRICING_FORMAT,
  formatVersion: PRICING_FORMAT_VERSION,
  exportedAt: now,
  ...(stackName ? { stackName } : {}),
  pricing: values,
});

export const pricingText = (document, format) =>
  format === 'yaml'
    ? dump(document, { noRefs: true, lineWidth: -1, sortKeys: false, schema: CORE_SCHEMA })
    : `${JSON.stringify(document, null, 2)}\n`;

/**
 * The section values an imported price file sets: what the file holds, laid
 * over the shipped table so anything the file leaves out keeps its shipped
 * value. Reads a file Export prices wrote, in JSON or YAML, or a bare price
 * table. Nothing is saved: the result becomes unsaved changes on the page,
 * checked like any other edit.
 */
export const readPricingFile = (text, defaults) => {
  let parsed;
  try {
    parsed = `${text}`.trim().startsWith('{') ? JSON.parse(text) : load(text, { schema: CORE_SCHEMA });
  } catch (err) {
    throw new Error(`It is not a JSON or YAML file: ${err.reason || err.message}`);
  }
  if (isObject(parsed) && parsed.format !== undefined && parsed.format !== PRICING_FORMAT) {
    throw new Error(`It is a ${parsed.format} file, not an exported price table.`);
  }
  const table = isObject(parsed) && parsed.format === PRICING_FORMAT ? parsed.pricing : parsed;
  if (!isObject(table) || !isObject(table.regions)) {
    throw new Error('It holds no price table: there is no "regions" object of prices in it.');
  }
  return deepMerge(defaults || {}, table);
};
