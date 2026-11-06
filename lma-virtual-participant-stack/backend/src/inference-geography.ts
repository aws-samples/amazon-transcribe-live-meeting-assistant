// Copyright (c) 2025 Amazon.com
// This file is licensed under the MIT License.
// See the LICENSE file in the project root for full license information.

/**
 * The Model inference geography setting, for the one Amazon Bedrock model the
 * Virtual Participant calls itself: the AI DOM resolver's
 * (BEDROCK_DOM_RESOLVER_MODEL_ID), which reads the meeting page's structure.
 *
 * A port of resolve_for_geography in the configuration layer
 * (lma_config/inference_geography.py), where the rules are set out. The
 * catalogue beside this file is a byte-identical copy of the layer's, and
 * inference-geography.test.ts holds this port to the Python resolver's answers
 * (the Configuration page's inference-geography-cases.json);
 * lma-ai-stack/test/test_inference_geography.py keeps both current.
 *
 * The setting is read once per process, at the resolver's first model call:
 * a Virtual Participant process lives for one meeting.
 */
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';

import CATALOG from './bedrock-inference-catalog.json' with { type: 'json' };
import { ConfigurationStore, declareSection } from './configuration-store.js';
import { createLogger } from './logger.js';

const log = createLogger('inference-geography');

export const GLOBAL = 'global';
export const GEOGRAPHY = 'geography';
export const IN_REGION = 'in_region';

export interface InferenceCatalog {
    readonly profilePrefixes: Readonly<Record<string, string>>;
    readonly geographies: ReadonlyArray<{ readonly match: string; readonly prefixes: readonly string[] }>;
    readonly models: readonly string[];
    readonly regions: Readonly<Record<string, { readonly profiles?: readonly string[]; readonly inRegion?: readonly string[] }>>;
}

export interface ModelResolution {
    /** The id to call Amazon Bedrock with; null when the geography has none. */
    readonly modelId: string | null;
    readonly verified: boolean;
    readonly unavailable: boolean;
}

const SHIPPED: InferenceCatalog = CATALOG as InferenceCatalog;

/**
 * The Amazon Bedrock Models section, standard tier as in the Python half
 * (lma_config/sections.py).
 */
export const LANGUAGE_MODELS = declareSection({
    name: 'language_models',
    cacheTier: 'standard',
    description:
        'The Amazon Bedrock model used for summaries and meeting insights, for the meeting assistant, ' +
        'for its knowledge bases, and for reading key frames.',
});

export function splitModelId(modelId: string, catalog: InferenceCatalog = SHIPPED): [string | null, string] {
    const text = modelId || '';
    const dot = text.indexOf('.');
    if (dot > 0) {
        const head = text.slice(0, dot);
        const rest = text.slice(dot + 1);
        if (rest && Object.prototype.hasOwnProperty.call(catalog.profilePrefixes, head)) return [head, rest];
    }
    return [null, text];
}

export function geographyPrefixes(region: string, catalog: InferenceCatalog = SHIPPED): readonly string[] {
    const entry = catalog.geographies.find(
        ({ match }) => region === match || (match.endsWith('-') && (region || '').startsWith(match)),
    );
    return entry ? entry.prefixes : [];
}

export function resolveForGeography(
    modelId: string,
    geography: string,
    region: string,
    catalog: InferenceCatalog = SHIPPED,
): ModelResolution {
    const keepAs = (id: string | null, verified: boolean): ModelResolution => ({ modelId: id, verified, unavailable: false });
    if ((geography !== GEOGRAPHY && geography !== IN_REGION) || !modelId) return keepAs(modelId, false);

    const [prefix, base] = splitModelId(modelId, catalog);
    const listed = catalog.regions[region];
    const offered = listed ? new Set([...(listed.profiles || []), ...(listed.inRegion || [])]) : null;
    const known = offered !== null && catalog.models.includes(base);
    const allowed = geography === GEOGRAPHY ? geographyPrefixes(region, catalog) : [];
    const keep = (): ModelResolution => keepAs(modelId, Boolean(known && offered?.has(modelId)));

    if (prefix === null) {
        if (geography === IN_REGION || !known || offered?.has(modelId)) return keep();
        const candidate = allowed.map((p) => `${p}.${base}`).find((id) => offered?.has(id));
        return candidate ? keepAs(candidate, true) : keep();
    }
    if (allowed.includes(prefix)) return keep();

    const candidates = [...allowed.map((p) => `${p}.${base}`), base];
    if (!known) return keepAs(candidates[0], false);
    const found = candidates.find((id) => offered?.has(id));
    return found ? keepAs(found, true) : { modelId: null, verified: true, unavailable: true };
}

/** The saved geography; Global when unset or unreadable, as in the Python half. */
export async function configuredGeography(region: string): Promise<string> {
    const tableName = process.env.CONFIGURATION_TABLE_NAME || '';
    if (!tableName) return GLOBAL;
    try {
        const store = new ConfigurationStore({ tableName, client: new DynamoDBClient({ region }) });
        const section = await store.getSection<Record<string, unknown>>(LANGUAGE_MODELS);
        const value = section.values.inferenceGeography;
        return value === GEOGRAPHY || value === IN_REGION ? value : GLOBAL;
    } catch (err) {
        log.warn('[inference-geography] could not read the Amazon Bedrock Models section; using Global', err);
        return GLOBAL;
    }
}

/**
 * The id to call Amazon Bedrock with for modelId under the saved geography.
 * Throws when the geography has no id for the model, rather than calling
 * outside it.
 */
export async function resolveModelId(
    modelId: string,
    region: string,
    readGeography: (region: string) => Promise<string> = configuredGeography,
): Promise<string> {
    const geography = await readGeography(region);
    const resolution = resolveForGeography(modelId, geography, region);
    if (resolution.unavailable || resolution.modelId === null) {
        throw new Error(
            `${modelId} has no Amazon Bedrock id within the Model inference geography (${geography}) of ${region}`,
        );
    }
    if (resolution.modelId !== modelId) {
        log.info(`[inference-geography] ${geography}: ${modelId} is run as ${resolution.modelId} from ${region}` +
            (resolution.verified ? '' : ' (not checked against the shipped catalogue)'));
    }
    return resolution.modelId;
}
