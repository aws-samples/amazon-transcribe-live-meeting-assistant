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

/**
 * The configuration table is configured, and reading the setting from it
 * failed. Thrown instead of reading the setting as Global, which would run the
 * call outside a geography an administrator may have chosen; as in the Python
 * half (InferenceGeographyUnreadable).
 */
export class InferenceGeographyUnreadable extends Error {
    constructor(cause: unknown) {
        super(
            'could not read the Model inference geography (the Amazon Bedrock Models section): ' +
                `${cause instanceof Error ? cause.message : String(cause)}. The Amazon Bedrock call is not made, ` +
                'so that it cannot run outside the geography the setting may hold.',
            { cause },
        );
        this.name = 'InferenceGeographyUnreadable';
    }
}

/** What configuredGeography reads the section through; injectable for tests. */
export type SectionReader = Pick<ConfigurationStore, 'getSection'>;

/**
 * The saved geography, as in the Python half: Global when there is no
 * configuration table or the setting is unset (or not a geography).
 *
 * @throws InferenceGeographyUnreadable the table is configured and reading the
 *     section from it failed.
 */
export async function configuredGeography(
    region: string,
    makeReader: (tableName: string) => SectionReader = (tableName) =>
        new ConfigurationStore({ tableName, client: new DynamoDBClient({ region }) }),
): Promise<string> {
    const tableName = process.env.CONFIGURATION_TABLE_NAME || '';
    if (!tableName) return GLOBAL;
    let value: unknown;
    try {
        const section = await makeReader(tableName).getSection<Record<string, unknown>>(LANGUAGE_MODELS);
        value = section.values.inferenceGeography;
    } catch (err) {
        log.warn('[inference-geography] could not read the Amazon Bedrock Models section', err);
        throw new InferenceGeographyUnreadable(err);
    }
    return value === GEOGRAPHY || value === IN_REGION ? value : GLOBAL;
}

/**
 * The id to call Amazon Bedrock with for modelId under the saved geography.
 * Throws when the geography has no id for the model, rather than calling
 * outside it, and InferenceGeographyUnreadable when the setting cannot be read.
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
