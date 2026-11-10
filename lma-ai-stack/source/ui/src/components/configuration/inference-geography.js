/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */

// Which Amazon Bedrock id each model setting runs as under the Model inference
// geography setting: a port of resolve_for_geography in the configuration layer
// (lma_config/inference_geography.py), for the Amazon Bedrock Models section's
// preview. The catalogue is a byte-identical copy of the layer's, and
// inference-geography-cases.json holds the Python resolver's answers, which the
// tests beside this file hold this port to; lma-ai-stack/test/test_inference_geography.py
// keeps both files current.

import CATALOG from './bedrock-inference-catalog.json';

export const GLOBAL = 'global';
export const GEOGRAPHY = 'geography';
export const IN_REGION = 'in_region';

/** [profile prefix or null, bare model id]. */
export const splitModelId = (modelId, catalog = CATALOG) => {
  const text = modelId || '';
  const dot = text.indexOf('.');
  if (dot > 0) {
    const head = text.slice(0, dot);
    const rest = text.slice(dot + 1);
    if (rest && Object.prototype.hasOwnProperty.call(catalog.profilePrefixes, head)) return [head, rest];
  }
  return [null, text];
};

/** The profile prefixes within the region's geography, narrowest first. */
export const geographyPrefixes = (region, catalog = CATALOG) => {
  const entry = catalog.geographies.find(
    ({ match }) => region === match || (match.endsWith('-') && `${region || ''}`.startsWith(match)),
  );
  return entry ? entry.prefixes : [];
};

/** Where an id runs inference, in words. */
export const describeScope = (modelId, region, catalog = CATALOG) => {
  const [prefix] = splitModelId(modelId, catalog);
  if (prefix === null) return `${region} only`;
  return catalog.profilePrefixes[prefix] || prefix;
};

/**
 * How modelId is run under geography from region:
 * { modelId, verified, unavailable }. unavailable is true where the
 * Python resolver raises ModelUnavailableInGeography; modelId is then null.
 */
export const resolveForGeography = (modelId, geography, region, catalog = CATALOG) => {
  const keepAs = (id, verified) => ({ modelId: id, verified, unavailable: false });
  if ((geography !== GEOGRAPHY && geography !== IN_REGION) || !modelId) return keepAs(modelId, false);

  const [prefix, base] = splitModelId(modelId, catalog);
  const listed = catalog.regions[region];
  const offered = listed ? new Set([...(listed.profiles || []), ...(listed.inRegion || [])]) : null;
  const known = offered !== null && catalog.models.includes(base);
  const allowed = geography === GEOGRAPHY ? geographyPrefixes(region, catalog) : [];
  const keep = () => keepAs(modelId, Boolean(known && offered.has(modelId)));

  if (prefix === null) {
    if (geography === IN_REGION || !known || offered.has(modelId)) return keep();
    const candidate = allowed.map((p) => `${p}.${base}`).find((id) => offered.has(id));
    return candidate ? keepAs(candidate, true) : keep();
  }
  if (allowed.includes(prefix)) return keep();

  const candidates = [...allowed.map((p) => `${p}.${base}`), base];
  if (!known) return keepAs(candidates[0], false);
  const found = candidates.find((id) => offered.has(id));
  return found ? keepAs(found, true) : { modelId: null, verified: true, unavailable: true };
};

export const CATALOG_LISTED_ON = CATALOG.listedOn;
