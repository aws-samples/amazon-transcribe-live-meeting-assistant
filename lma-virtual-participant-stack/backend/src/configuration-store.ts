/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * The TypeScript half of LMA's shared runtime-configuration accessor.
 *
 * The Python half, used by the Lambda functions, lives in
 * `lma-ai-stack/source/lambda_layers/configuration_layer/lma_config/`.
 * Both read the same table, the same key layout and the same compressed body,
 * and both take their caching policy from the section being read rather than
 * from the call site. `docs/configuration-store.md` is the one description of
 * the design that covers both.
 *
 * Why a declared tier per section rather than one policy
 * ------------------------------------------------------
 * The two readers in the transcriber's `src/calleventdata/` disagree on caching, and both
 * are right. `asr-config.ts` caches for 30 seconds so that a burst of meetings
 * starting at once does not each issue its own read. `voice-enrollment.ts`
 * never caches, because a withdrawal of consent is promised to take effect for
 * every later meeting and a cache is exactly a window in which it would not.
 * Unifying them under one TTL would break one or the other, so the TTL is a
 * property of the section:
 *
 * | Tier         | TTL   | Read            | For                                  |
 * | ------------ | ----- | --------------- | ------------------------------------ |
 * | `immediate`  | none  | consistent      | Anything promised to apply at once,  |
 * |              |       |                 | and anything consent-bearing.        |
 * | `short`      | 30 s  | eventual        | Per-meeting switches read at start.  |
 * | `standard`   | 300 s | eventual        | Prompts, model ids, numeric limits.  |
 *
 * There is no TTL argument on any read: `getSection` takes a section
 * declaration, and the declaration carries the policy.
 *
 * One file, two packages
 * ----------------------
 * The Virtual Participant is a separate npm package and LMA has no shared
 * TypeScript package, so this file is copied verbatim to
 * `lma-virtual-participant-stack/backend/src/configuration-store.ts`.
 * `lma-ai-stack/test/test_configuration_store_contract.py` fails if the two
 * copies differ, so a change is made here and copied there. The file imports
 * nothing relative for that reason: it compiles unchanged under the
 * transcriber's CommonJS build and the Virtual Participant's ES module one.
 */
import { gunzipSync } from 'node:zlib';
import { createHash } from 'node:crypto';

import { DynamoDBClient, GetItemCommand } from '@aws-sdk/client-dynamodb';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';

/** The single environment variable naming the table, spelled this way everywhere. */
export const TABLE_NAME_ENV_VAR = 'CONFIGURATION_TABLE_NAME';

/** The partition key attribute. One HASH key, no sort key, no indexes. */
export const KEY_ATTRIBUTE = 'Configuration';

/** The compressed body attribute. */
export const BODY_ATTRIBUTE = 'Body';

export const CONFIG_PREFIX = 'Config#';

/**
 * What a profile may be named. The same set the Python half permits, and
 * asserted equal by the cross-language contract test: a name appears verbatim
 * in the partition key, so restricting it is what means a name can never
 * introduce structure into one.
 */
export const PROFILE_NAME = /^[A-Za-z0-9._-]{1,64}$/;

/** Names the store owns, which a profile may not be stored under. */
export const RESERVED_PROFILE_NAMES: ReadonlySet<string> = new Set([
    'default',
    '__active',
    'schema',
]);

/**
 * Whether `name` names a user profile.
 *
 * False for the names the store owns and for anything beginning `__`, matching
 * the Python half's `is_profile_key`. The baseline and the pointer are not
 * profiles, so a pointer naming either resolves to the baseline rather than
 * reading one of them as a profile.
 */
export const isProfileName = (name: string): boolean => {
    if (!PROFILE_NAME.test(name)) {
        return false;
    }
    const lowered = name.toLowerCase();
    return !lowered.startsWith('__') && !RESERVED_PROFILE_NAMES.has(lowered);
};
export const DEFAULT_KEY = `${CONFIG_PREFIX}default`;
export const ACTIVE_POINTER_KEY = `${CONFIG_PREFIX}__active`;

/**
 * Attributes that stay readable at the top level of the item. A closed list,
 * matching the Python half: anything not named here is part of the body.
 */
export const METADATA_ATTRIBUTES: ReadonlySet<string> = new Set([
    KEY_ATTRIBUTE,
    'CreatedAt',
    'UpdatedAt',
    'UpdatedBy',
    'Version',
    'IsActive',
    'ActiveProfile',
    'Description',
    'Managed',
    // How a profile came to exist, written once when it is created and carried
    // forward by every later write; see the Python half for why it is durable.
    'ProfileOrigin',
    'LatestRevision',
    'PublishedRevision',
]);

export type CacheTier = 'immediate' | 'short' | 'standard';

/** Milliseconds a resolved document may be reused when serving each tier. */
export const TIER_TTL_MS: Readonly<Record<CacheTier, number>> = {
    immediate: 0,
    short: 30_000,
    standard: 300_000,
};

/** Whether a refetch for a tier asks DynamoDB for a strongly consistent read. */
export const TIER_CONSISTENT_READ: Readonly<Record<CacheTier, boolean>> = {
    immediate: true,
    short: false,
    standard: false,
};

export interface SectionSpec {
    /** The key the section occupies in the configuration body. */
    readonly name: string;
    /** The declared caching policy. Not overridable at a call site. */
    readonly cacheTier: CacheTier;
    /** What the section holds. */
    readonly description: string;
}

export interface ResolvedSection<T> {
    readonly name: string;
    /** The section as resolved: the baseline with the active profile merged over it. */
    readonly values: T;
    /**
     * The two layers `values` was merged from: the section as the baseline
     * ships it, and as the active profile holds it (empty when the profile
     * holds nothing for it). For a reader whose settings do not simply
     * override -- the Nova Sonic prompt can be appended to the shipped one
     * rather than replace it -- and for telling "set to the shipped value" from
     * "not set at all".
     */
    readonly baselineValues: T;
    readonly profileValues: T;
    /** A short digest, so a caller can skip rebuilding derived state. */
    readonly fingerprint: string;
    readonly cacheTier: CacheTier;
    readonly servedFromCache: boolean;
    readonly activeProfile: string | undefined;
}

export interface ConfigurationDocument {
    readonly body: Record<string, unknown>;
    /** The baseline's body and the active profile's, before they were merged. */
    readonly baselineBody: Record<string, unknown>;
    readonly profileBody: Record<string, unknown>;
    readonly activeProfile: string | undefined;
    /**
     * Whether `Config#default` was there to read. False means the table has not
     * been seeded yet -- the window between a stack update putting new code live
     * and the seeding custom resource running -- and such a document is
     * deliberately not cached: caching it would hold the empty answer for the
     * whole of a tier's TTL after seeding had finished.
     */
    readonly baselineExists: boolean;
}

type PlainItem = Record<string, unknown>;

const SECTIONS = new Map<string, SectionSpec>();

/**
 * Declare a section and its caching policy.
 *
 * Throws if the name is already declared with a different policy: two
 * declarations would mean the applied policy depended on import order.
 */
export const declareSection = (spec: SectionSpec): SectionSpec => {
    const existing = SECTIONS.get(spec.name);
    if (existing && existing.cacheTier !== spec.cacheTier) {
        throw new Error(
            `the configuration section '${spec.name}' is already declared on the ` +
                `'${existing.cacheTier}' tier, which is a different caching policy`
        );
    }
    SECTIONS.set(spec.name, spec);
    return spec;
};

/** Every declared section, for tests and documentation. */
export const declaredSections = (): ReadonlyMap<string, SectionSpec> => new Map(SECTIONS);

/**
 * The summary prompt templates, declared here as well as in the Python half so
 * the two sides of the accessor describe one contract. Read by the
 * meeting-summary Lambda; no TypeScript reader uses it.
 */
export const SUMMARY_PROMPT_TEMPLATES = declareSection({
    name: 'summary_prompt_templates',
    cacheTier: 'standard',
    description:
        'Prompts used to generate each section of a meeting summary, keyed \'N#Title\' ' +
        'where N orders the sections.',
});

/**
 * The voice assistant's settings, read by the Virtual Participant when its
 * voice assistant starts. Immediate, as in the Python half: one read per
 * meeting, and the page promises the next meeting uses what was saved.
 */
export const NOVA_SONIC_CONFIG = declareSection({
    name: 'nova_sonic_config',
    cacheTier: 'immediate',
    description:
        'Voice assistant settings: the Nova Sonic system prompt and prompt mode, model, voice, ' +
        'endpointing sensitivity, meeting mode, and the translator languages and trigger phrases.',
});

/**
 * The on-demand ASR engine's runtime switches, read by the transcriber at the
 * start of each streaming meeting. Short, as in the Python half: a burst of
 * meetings starting together shares one read, and a change reaches every
 * meeting that starts more than 30 seconds after it was saved.
 */
export const ASR_ENGINE = declareSection({
    name: 'asr_engine',
    cacheTier: 'short',
    description:
        'On-demand ASR engine switches: whether streaming meetings and Virtual Participants ' +
        'use it, and whether Virtual Participants ask it for per-voice labels.',
});

/**
 * The Amazon Transcribe options of a meeting, read by the transcriber at the
 * start of each one. Immediate, as in the Python half: redaction is
 * privacy-bearing, so a saved change applies to the very next meeting.
 */
export const TRANSCRIPTION = declareSection({
    name: 'transcription',
    cacheTier: 'immediate',
    description:
        'Amazon Transcribe options for streaming meetings: language or language identification, ' +
        'content redaction, custom vocabulary and language model, and the default for speaker partitioning.',
});

/** Whether a meeting's audio is recorded when its client does not say. Immediate, as in the Python half. */
export const RECORDING = declareSection({
    name: 'recording',
    cacheTier: 'immediate',
    description: 'Whether the audio of a meeting is recorded when its client does not say.',
});

/**
 * The Virtual Participant's own settings -- its voice assistant and avatar, and
 * how a Teams Virtual Participant decides a meeting has ended -- read once when
 * it starts. Immediate, as in the Python half, like the Nova Sonic settings the
 * same reader takes at the same moment.
 */
export const VIRTUAL_PARTICIPANT = declareSection({
    name: 'virtual_participant',
    cacheTier: 'immediate',
    description:
        'Virtual Participant settings read when it starts: the voice assistant provider, wake phrases and ' +
        'activation duration, the ElevenLabs agent and Simli face, and Teams meeting-end detection.',
});

/** The Amazon Transcribe options whose combination is checked; see transcribeOptionsProblem. */
export interface TranscribeOptionCombination {
    readonly languageCode: string;
    readonly languageOptions: string;
    readonly preferredLanguage: string;
    readonly customVocabularyName: string;
    readonly customLanguageModelName: string;
}

/**
 * Why a set of Amazon Transcribe options cannot be used together, or undefined
 * when it can.
 *
 * The checks the stack makes of its parameters while it deploys
 * (ValidateParameters in lma-main.yaml), which a value saved on the
 * Configuration page has not been through: language identification needs
 * language options, at most one dialect per language, a preferred language
 * among them, and no custom vocabulary or language model. Here rather than in a
 * reader because both readers of these options, the WebSocket transcriber and
 * the Virtual Participant, make them.
 */
export const transcribeOptionsProblem = (options: TranscribeOptionCombination): string | undefined => {
    if (options.languageCode !== 'identify-language' && options.languageCode !== 'identify-multiple-languages') {
        return undefined;
    }
    if (options.customVocabularyName) {
        return `a custom vocabulary cannot be used with ${options.languageCode}`;
    }
    if (options.customLanguageModelName) {
        return `a custom language model cannot be used with ${options.languageCode}`;
    }
    const languages = options.languageOptions
        .split(',')
        .map((code) => code.trim())
        .filter((code) => code.length > 0);
    if (languages.length === 0) {
        return `${options.languageCode} needs language options`;
    }
    const seen = new Set<string>();
    for (const code of languages) {
        const language = code.split('-')[0];
        if (seen.has(language)) {
            return `the language options name more than one dialect of '${language}'`;
        }
        seen.add(language);
    }
    if (options.preferredLanguage !== 'None' && options.preferredLanguage !== '' && !languages.includes(options.preferredLanguage)) {
        return `the preferred language ${options.preferredLanguage} is not one of the language options`;
    }
    return undefined;
};

/**
 * The type of a setting whose default is a stack parameter, as the Python
 * half's `parameter_defaults.json` names it.
 */
export type SettingType = 'string' | 'integer' | 'boolean';

type SettingValue<K extends SettingType> = K extends 'string' ? string : K extends 'integer' ? number : boolean;

/**
 * `raw` read back into a setting's type, or undefined when it is not one.
 *
 * The same rules as the Python half's `coerce`, because a value reaches a
 * reader either from the configuration (JSON-typed) or from an environment
 * variable (text), and the two halves must agree on what a value is:
 * `true`/`false` in any case for a boolean, digits with an optional sign for an
 * integer, and anything for text.
 */
export const coerceSetting = <K extends SettingType>(type: K, raw: unknown): SettingValue<K> | undefined => {
    if (raw === undefined || raw === null) {
        return undefined;
    }
    if (type === 'string') {
        return String(raw) as SettingValue<K>;
    }
    if (type === 'boolean') {
        if (typeof raw === 'boolean') {
            return raw as SettingValue<K>;
        }
        const text = String(raw).trim().toLowerCase();
        return text === 'true' || text === 'false' ? ((text === 'true') as SettingValue<K>) : undefined;
    }
    if (typeof raw === 'number') {
        return Number.isInteger(raw) ? (raw as SettingValue<K>) : undefined;
    }
    if (typeof raw === 'boolean') {
        return undefined;
    }
    const text = String(raw).trim();
    return /^[+-]?[0-9]+$/.test(text) ? (Number.parseInt(text, 10) as SettingValue<K>) : undefined;
};

/**
 * The value of a setting whose default is a stack parameter.
 *
 * In order of preference, as the Python half's `read_setting`: the resolved
 * section's value (an administrator's change, or else the baseline the seeder
 * wrote from the parameter); the parameter's environment variable; `fallback`.
 * A value of the wrong type is passed over. An empty environment variable is a
 * value for a text setting and not for the others.
 */
export const parameterBackedSetting = <K extends SettingType>(
    values: Record<string, unknown>,
    name: string,
    type: K,
    envValue: string | undefined,
    fallback: SettingValue<K>
): SettingValue<K> => {
    const stored = coerceSetting(type, values[name]);
    if (stored !== undefined) {
        return stored;
    }
    if (envValue === undefined || (envValue === '' && type !== 'string')) {
        return fallback;
    }
    return coerceSetting(type, envValue) ?? fallback;
};

/**
 * Decompress a body read back from DynamoDB.
 *
 * The layout is detected from the item rather than configured: a `Body`
 * attribute means the compressed layout, and its absence means an *inline*
 * record whose remaining attributes are its body. Nothing writes one; the
 * detection is here so one can be read, which is what lets a later store
 * migration lift an existing table's items in as they stand, and what makes a
 * record repaired by hand in the console read as what it says rather than as
 * empty.
 */
const decodeBody = (raw: Uint8Array): Record<string, unknown> => {
    const parsed: unknown = JSON.parse(gunzipSync(Buffer.from(raw)).toString('utf-8'));
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('a configuration body must be a JSON object');
    }
    return parsed as Record<string, unknown>;
};

/** One AttributeValue -> plain JavaScript. */
const fromAttributeValue = (value: AttributeValue): unknown => {
    if (value.S !== undefined) {
        return value.S;
    }
    if (value.N !== undefined) {
        return Number(value.N);
    }
    if (value.BOOL !== undefined) {
        return value.BOOL;
    }
    if (value.NULL !== undefined) {
        return null;
    }
    if (value.B !== undefined) {
        return value.B;
    }
    if (value.L !== undefined) {
        return value.L.map(fromAttributeValue);
    }
    if (value.M !== undefined) {
        const result: PlainItem = {};
        Object.entries(value.M).forEach(([key, nested]) => {
            result[key] = fromAttributeValue(nested);
        });
        return result;
    }
    if (value.SS !== undefined) {
        return value.SS;
    }
    if (value.NS !== undefined) {
        return value.NS.map(Number);
    }
    return undefined;
};

export interface ConfigurationRecord {
    readonly exists: boolean;
    readonly metadata: PlainItem;
    readonly body: Record<string, unknown>;
}

const splitItem = (item: Record<string, AttributeValue>): ConfigurationRecord => {
    const metadata: PlainItem = {};
    const inline: PlainItem = {};
    let body: Record<string, unknown> | undefined;

    Object.entries(item).forEach(([name, value]) => {
        if (name === BODY_ATTRIBUTE) {
            body = value.B === undefined ? {} : decodeBody(value.B);
        } else if (METADATA_ATTRIBUTES.has(name)) {
            metadata[name] = fromAttributeValue(value);
        } else {
            inline[name] = fromAttributeValue(value);
        }
    });

    return { exists: true, metadata, body: body ?? inline };
};

/** A section of a body, or an empty object when it holds something else or nothing. */
const sectionOf = (body: Record<string, unknown>, name: string): Record<string, unknown> => {
    const raw = body[name];
    return raw !== null && typeof raw === 'object' && !Array.isArray(raw)
        ? (raw as Record<string, unknown>)
        : {};
};

/** `overlay` over `base`, recursing into nested objects, so a profile is a delta. */
export const deepMerge = (
    base: Record<string, unknown>,
    overlay: Record<string, unknown>
): Record<string, unknown> => {
    const merged: Record<string, unknown> = { ...base };
    Object.entries(overlay).forEach(([key, value]) => {
        const existing = merged[key];
        const bothObjects =
            existing !== null &&
            typeof existing === 'object' &&
            !Array.isArray(existing) &&
            value !== null &&
            typeof value === 'object' &&
            !Array.isArray(value);
        merged[key] = bothObjects
            ? deepMerge(existing as Record<string, unknown>, value as Record<string, unknown>)
            : value;
    });
    return merged;
};

/**
 * A short, stable digest of a resolved value.
 *
 * Both halves must produce the same digest for the same section, or a caller
 * comparing one taken here with one taken by the Python half would rebuild its
 * derived state forever, or never. `canonicalJson` below sorts object keys and
 * emits non-ASCII characters as themselves; the Python half passes
 * `ensure_ascii=False` for the same reason. Each suite pins the same input to
 * the same expected digest, which is what keeps that from drifting.
 */
const fingerprintOf = (value: unknown): string =>
    createHash('sha256').update(canonicalJson(value)).digest('hex').slice(0, 16);

/** JSON with object keys sorted, so the digest does not depend on key order. */
const canonicalJson = (value: unknown): string => {
    if (value === null || typeof value !== 'object') {
        return JSON.stringify(value) ?? 'null';
    }
    if (Array.isArray(value)) {
        return `[${value.map(canonicalJson).join(',')}]`;
    }
    const entries = Object.entries(value as PlainItem).sort(([a], [b]) => (a < b ? -1 : 1));
    return `{${entries.map(([key, nested]) => `${JSON.stringify(key)}:${canonicalJson(nested)}`).join(',')}}`;
};

export interface ConfigurationStoreOptions {
    readonly tableName?: string;
    readonly client?: DynamoDBClient;
    /** Injectable for tests; defaults to `Date.now`. */
    readonly clock?: () => number;
}

/**
 * Read runtime configuration.
 *
 * Build one per process. The cache lives on the instance, so a per-request
 * instance would mean a per-request read whatever tier a section declares.
 */
export class ConfigurationStore {
    private readonly tableName: string;

    private readonly client: DynamoDBClient;

    private readonly clock: () => number;

    private cached: ConfigurationDocument | undefined;

    private cachedAt = 0;

    /** Armed by `invalidate`; see it for why. */
    private readConsistentlyNext = false;

    constructor(options: ConfigurationStoreOptions = {}) {
        const tableName = options.tableName ?? process.env[TABLE_NAME_ENV_VAR] ?? '';
        if (!tableName) {
            throw new Error(
                `no configuration table name was supplied and ${TABLE_NAME_ENV_VAR} is unset`
            );
        }
        this.tableName = tableName;
        this.client =
            options.client ??
            new DynamoDBClient({ region: process.env['AWS_REGION'] || 'us-east-1' });
        this.clock = options.clock ?? Date.now;
    }

    /**
     * Forget the cached document, and read the next one consistently.
     *
     * For use after a write, so the writer's own next read sees what it saved,
     * and for a caller that must observe the latest -- the Python half's
     * Regenerate path is the live example. It arms a *one-shot* consistent read
     * rather than changing anything durable, because both callers have the same
     * problem: the read happens immediately after the thing it must observe,
     * which is the one situation the tiers' eventual-consistency choice does
     * not cover. That choice rests on a TTL measured in tens of seconds being
     * far longer than DynamoDB's replication lag, and there is no TTL here.
     */
    invalidate(): void {
        this.cached = undefined;
        this.cachedAt = 0;
        this.readConsistentlyNext = true;
    }

    /**
     * One section of the resolved document, honouring the tier it declares.
     *
     * The section is passed as its declaration rather than as a name, so there
     * is no way to read a section whose caching policy has not been decided.
     */
    async getSection<T extends Record<string, unknown>>(
        spec: SectionSpec
    ): Promise<ResolvedSection<T>> {
        const ttl = TIER_TTL_MS[spec.cacheTier];
        let servedFromCache = false;
        let document: ConfigurationDocument;

        if (ttl > 0 && this.cached && this.clock() - this.cachedAt < ttl) {
            document = this.cached;
            servedFromCache = true;
        } else {
            const consistent = TIER_CONSISTENT_READ[spec.cacheTier] || this.readConsistentlyNext;
            this.readConsistentlyNext = false;
            document = await this.fetch(consistent);
            if (document.baselineExists) {
                this.cached = document;
                this.cachedAt = this.clock();
            } else {
                // Nothing to cache yet; see ConfigurationDocument.baselineExists.
                this.invalidate();
            }
        }

        const values = sectionOf(document.body, spec.name);
        return {
            name: spec.name,
            values: values as T,
            baselineValues: sectionOf(document.baselineBody, spec.name) as T,
            profileValues: sectionOf(document.profileBody, spec.name) as T,
            fingerprint: fingerprintOf(values),
            cacheTier: spec.cacheTier,
            servedFromCache,
            activeProfile: document.activeProfile,
        };
    }

    /** The whole resolved document, read now and not cached. */
    async getDocument(consistentRead = false): Promise<ConfigurationDocument> {
        return this.fetch(consistentRead);
    }

    /** One stored record by its full partition key. */
    async readRecord(key: string, consistentRead = false): Promise<ConfigurationRecord> {
        const result = await this.client.send(
            new GetItemCommand({
                TableName: this.tableName,
                Key: { [KEY_ATTRIBUTE]: { S: key } },
                ConsistentRead: consistentRead,
            })
        );
        if (!result.Item) {
            return { exists: false, metadata: {}, body: {} };
        }
        return splitItem(result.Item);
    }

    private async fetch(consistentRead: boolean): Promise<ConfigurationDocument> {
        const pointer = await this.readRecord(ACTIVE_POINTER_KEY, consistentRead);
        const active = pointer.metadata['ActiveProfile'];
        // A pointer naming something that is not a profile name resolves to the
        // baseline, which is what the Python half does and what a pointer naming
        // a profile that is not stored already did. Only a hand edit can write
        // one, and raising would make a single bad character stop every read.
        const activeProfile =
            typeof active === 'string' && isProfileName(active) ? active : undefined;

        const baseline = await this.readRecord(DEFAULT_KEY, consistentRead);
        if (!activeProfile) {
            return {
                body: baseline.body,
                baselineBody: baseline.body,
                profileBody: {},
                activeProfile: undefined,
                baselineExists: baseline.exists,
            };
        }
        const profile = await this.readRecord(`${CONFIG_PREFIX}${activeProfile}`, consistentRead);
        return {
            body: deepMerge(baseline.body, profile.body),
            baselineBody: baseline.body,
            profileBody: profile.body,
            // A pointer naming a profile that is not stored resolves to the
            // baseline alone, rather than to nothing.
            activeProfile: profile.exists ? activeProfile : undefined,
            baselineExists: baseline.exists,
        };
    }
}
