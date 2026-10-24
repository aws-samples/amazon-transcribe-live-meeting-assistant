/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Tests for the TypeScript half of the shared configuration accessor.
 *
 * The properties asserted here are the ones that make the two halves one
 * accessor rather than two that look alike:
 *
 * - A body gzipped by the Python half decompresses to the same object here.
 *   `PYTHON_ENCODED_BODY` below is a committed fixture of real bytes from
 *   `lma_config.codec.encode_body`, so a change to that encoding fails here
 *   rather than at runtime. The bodies the other tests seed are built locally
 *   to the same spec, which is a round-trip and not a cross-language check.
 * - An inline record -- one with no compressed body -- is read as its own body.
 *   Nothing writes one; this is what lets a later store migration lift an
 *   existing table's items in as they stand.
 * - The declared tier, not the call site, decides staleness: the immediate tier
 *   never serves a cached value and reads consistently, and the short and
 *   standard tiers cache for exactly as long as the readers they replace did.
 * - The fingerprint is computed the same way as the Python half's, so the same
 *   section gives the same digest on both sides.
 *
 * No AWS calls: DynamoDB is a fake that records every request.
 */
process.env['AWS_REGION'] = process.env['AWS_REGION'] || 'us-east-1';

import assert from 'node:assert/strict';
import test from 'node:test';
import { gzipSync } from 'node:zlib';

import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';

import {
    ACTIVE_POINTER_KEY,
    ConfigurationStore,
    DEFAULT_KEY,
    METADATA_ATTRIBUTES,
    SUMMARY_PROMPT_TEMPLATES,
    TABLE_NAME_ENV_VAR,
    TIER_CONSISTENT_READ,
    TIER_TTL_MS,
    declareSection,
    declaredSections,
    deepMerge,
    isProfileName,
} from './configuration-store';

const TABLE = 'lma-configuration';
const SECTION = 'summary_prompt_templates';

/**
 * A body encoded to the same spec the Python half writes: gzipped, key-sorted,
 * compact JSON.
 *
 * Built here, so the bodies the tests below seed are a round-trip through this
 * file rather than a cross-language check -- `PYTHON_ENCODED_BODY` is the
 * cross-language one. The two sides' bytes are not identical anyway: Node's
 * gzip writes a different OS byte. What matters is that each can read the
 * other's, and only one direction of that is needed, because this half never
 * writes.
 */
const encodeBody = (body: unknown): Buffer =>
    gzipSync(Buffer.from(canonical(body), 'utf-8'), { level: 9 });

const canonical = (value: unknown): string => {
    if (value === null || typeof value !== 'object') {
        return JSON.stringify(value) ?? 'null';
    }
    if (Array.isArray(value)) {
        return `[${value.map(canonical).join(',')}]`;
    }
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) =>
        a < b ? -1 : 1
    );
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(',')}}`;
};

/**
 * A body encoded by the Python half, committed as base64 so this suite can read
 * real bytes rather than bytes it produced itself.
 *
 * Regenerate with:
 *
 *     python3 -c "import base64, sys; sys.path.insert(0, 'lma-ai-stack/source/\
 *     lambda_layers/configuration_layer'); from lma_config.codec import \
 *     encode_body; print(base64.b64encode(encode_body(BODY)).decode())"
 *
 * where BODY is `PYTHON_ENCODED_BODY_DECODED` below. The Python suite pins the
 * same pair, so neither side can change the encoding alone.
 */
const PYTHON_ENCODED_BODY =
    'H4sIAAAAAAAC/02O0QqCMBiFX2X83orNgRcNCaSrIAuyLgJBpq0cuCnbPyJk717YRd0dON/5ODMYr1tpHfAZ7lZ0qEYDnCYs' +
    'i+HZj4MEnqUsxOC81sK+msmOesIGpZ4GgXIZplF1KcvidAUO1cIpJ0mOVhjXWTXhZv7lkK/+CuKdMg9Se0bTNRHmRhitPaUt' +
    'TSAGFhXb8+54qD7ivXJIsJfke9JBCG8kvhxcvgAAAA==';

const PYTHON_ENCODED_BODY_DECODED = {
    summary_prompt_templates: {
        '1#SUMMARY': 'Summarise <transcript>{transcript}</transcript> using ’ and 20°.',
        '2#ACTIONS': 'List the actions',
    },
    numbers: { whole: 512, fraction: 0.25 },
};

interface Request {
    key: string;
    consistentRead: boolean;
}

class FakeTable {
    items = new Map<string, Record<string, AttributeValue>>();

    requests: Request[] = [];

    seed(key: string, body: unknown, version = 1): void {
        this.items.set(key, {
            Configuration: { S: key },
            Version: { N: String(version) },
            Body: { B: encodeBody(body) },
        });
    }

    pointAt(profile: string): void {
        this.items.set(ACTIVE_POINTER_KEY, {
            Configuration: { S: ACTIVE_POINTER_KEY },
            ActiveProfile: { S: profile },
        });
    }

    asClient(): DynamoDBClient {
        return {
            send: async (command: { input: Record<string, unknown> }) => {
                const input = command.input as {
                    Key: Record<string, { S: string }>;
                    ConsistentRead?: boolean;
                };
                const key = input.Key['Configuration']!.S;
                this.requests.push({ key, consistentRead: input.ConsistentRead === true });
                const item = this.items.get(key);
                return item ? { Item: item } : {};
            },
        } as unknown as DynamoDBClient;
    }
}

class Clock {
    now = 1_000_000;

    read = (): number => this.now;

    advance(ms: number): void {
        this.now += ms;
    }
}

const build = (): { table: FakeTable; clock: Clock; store: ConfigurationStore } => {
    const table = new FakeTable();
    const clock = new Clock();
    const store = new ConfigurationStore({
        tableName: TABLE,
        client: table.asClient(),
        clock: clock.read,
    });
    return { table, clock, store };
};

const seeded = (): { table: FakeTable; clock: Clock; store: ConfigurationStore } => {
    const built = build();
    built.table.seed(DEFAULT_KEY, {
        [SECTION]: { '1#SUMMARY': 'shipped summary', '2#ACTIONS': 'shipped actions' },
    });
    built.table.seed('Config#custom', { [SECTION]: { '1#SUMMARY': 'the admin edit' } });
    built.table.pointAt('custom');
    return built;
};

// ── the table name ─────────────────────────────────────────────────────────

test('the table name is required, so a store cannot read empty configuration forever', () => {
    const previous = process.env[TABLE_NAME_ENV_VAR];
    delete process.env[TABLE_NAME_ENV_VAR];
    assert.throws(() => new ConfigurationStore(), /CONFIGURATION_TABLE_NAME/);
    if (previous !== undefined) {
        process.env[TABLE_NAME_ENV_VAR] = previous;
    }
});

test('the table name comes from the one environment variable the Python half uses', () => {
    process.env[TABLE_NAME_ENV_VAR] = 'from-the-environment';
    const table = new FakeTable();
    const store = new ConfigurationStore({ client: table.asClient() });
    delete process.env[TABLE_NAME_ENV_VAR];
    assert.ok(store);
    assert.equal(TABLE_NAME_ENV_VAR, 'CONFIGURATION_TABLE_NAME');
});

// ── reading ────────────────────────────────────────────────────────────────

test('a profile is a delta over the shipped baseline', async () => {
    const { store } = seeded();
    const section = await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.deepEqual(section.values, {
        '1#SUMMARY': 'the admin edit',
        '2#ACTIONS': 'shipped actions',
    });
    assert.equal(section.activeProfile, 'custom');
});

test('the baseline alone is served when no profile has been written', async () => {
    const { table, store } = build();
    table.seed(DEFAULT_KEY, { [SECTION]: { '1#SUMMARY': 'shipped summary' } });
    const section = await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.deepEqual(section.values, { '1#SUMMARY': 'shipped summary' });
    assert.equal(section.activeProfile, undefined);
});

test('a pointer naming a profile that is not stored falls back to the baseline', async () => {
    const { table, store } = build();
    table.seed(DEFAULT_KEY, { [SECTION]: { '1#SUMMARY': 'shipped summary' } });
    table.pointAt('deleted-profile');
    const section = await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.deepEqual(section.values, { '1#SUMMARY': 'shipped summary' });
    assert.equal(section.activeProfile, undefined);
});

[
    'not a name',
    '../escape',
    'default',
    '__active',
    'schema',
    '',
    // A trailing newline: refused here, and refused by the Python half only
    // because it uses `fullmatch`. The contract test compares the patterns'
    // text, which cannot see that difference, so both suites pin the name.
    'custom\n',
].forEach((pointer) => {
    test(`a pointer naming '${pointer}' falls back to the baseline`, async () => {
        // One hand-edited attribute must not stop every read in the deployment.
        // Raising would be the natural thing for an invalid name and the wrong
        // thing here, and the Python half resolves it the same way -- which the
        // cross-language contract test asserts.
        const { table, store } = build();
        table.seed(DEFAULT_KEY, { [SECTION]: { '1#SUMMARY': 'shipped summary' } });
        table.items.set(ACTIVE_POINTER_KEY, {
            Configuration: { S: ACTIVE_POINTER_KEY },
            ActiveProfile: { S: pointer },
        });
        const section = await store.getSection(SUMMARY_PROMPT_TEMPLATES);
        assert.deepEqual(section.values, { '1#SUMMARY': 'shipped summary' });
        assert.equal(section.activeProfile, undefined);
    });
});

test('an ordinary profile name is accepted', () => {
    ['custom', 'production', 'teams-heavy', 'v1.2', 'a_b', 'A'].forEach((name) => {
        assert.equal(isProfileName(name), true, name);
    });
});

test('a body encoded by the Python half decodes to the same object', async () => {
    // Real bytes from `lma_config.codec.encode_body`, not bytes this suite
    // produced: the two halves agreeing about the compressed layout is the one
    // thing a round-trip in either language cannot establish.
    const { table, store } = build();
    table.items.set(DEFAULT_KEY, {
        Configuration: { S: DEFAULT_KEY },
        Version: { N: '1' },
        Body: { B: Buffer.from(PYTHON_ENCODED_BODY, 'base64') },
    });
    const document = await store.getDocument();
    assert.deepEqual(document.body, PYTHON_ENCODED_BODY_DECODED);
});

test('an inline record is read as its own body', async () => {
    const { table, store } = build();
    table.items.set(DEFAULT_KEY, {
        Configuration: { S: DEFAULT_KEY },
        UpdatedBy: { S: 'admin@example.com' },
        [SECTION]: { M: { '1#SUMMARY': { S: 'an inline prompt' } } },
    });
    const section = await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.deepEqual(section.values, { '1#SUMMARY': 'an inline prompt' });
});

test('a section the document does not hold reads as empty rather than undefined', async () => {
    const { table, store } = build();
    table.seed(DEFAULT_KEY, {});
    assert.deepEqual((await store.getSection(SUMMARY_PROMPT_TEMPLATES)).values, {});
});

test('a section holding something other than an object reads as empty', async () => {
    const { table, store } = build();
    table.seed(DEFAULT_KEY, { [SECTION]: 'not an object' });
    assert.deepEqual((await store.getSection(SUMMARY_PROMPT_TEMPLATES)).values, {});
});

test('the metadata list is closed and matches the Python half exactly', async () => {
    assert.deepEqual(
        [...METADATA_ATTRIBUTES],
        [
            'Configuration',
            'CreatedAt',
            'UpdatedAt',
            'UpdatedBy',
            'Version',
            'IsActive',
            'ActiveProfile',
            'Description',
            'Managed',
            'ProfileOrigin',
            'LatestRevision',
            'PublishedRevision',
        ]
    );
});

test('a metadata attribute does not leak into the body of an inline record', async () => {
    const { table, store } = build();
    table.items.set(DEFAULT_KEY, {
        Configuration: { S: DEFAULT_KEY },
        UpdatedBy: { S: 'admin@example.com' },
        '1#SUMMARY': { S: 'an inline prompt' },
    });
    const document = await store.getDocument();
    assert.deepEqual(document.body, { '1#SUMMARY': 'an inline prompt' });
});

// ── the declared cache tiers ───────────────────────────────────────────────

test('there are exactly three tiers, with the TTLs the readers they replace used', () => {
    assert.deepEqual(Object.keys(TIER_TTL_MS), ['immediate', 'short', 'standard']);
    assert.equal(TIER_TTL_MS.immediate, 0);
    assert.equal(TIER_TTL_MS.short, 30_000);
    assert.equal(TIER_TTL_MS.standard, 300_000);
});

test('only the immediate tier reads consistently', () => {
    assert.deepEqual(TIER_CONSISTENT_READ, {
        immediate: true,
        short: false,
        standard: false,
    });
});

test('a document with no baseline is never cached', async () => {
    // The window between a stack update putting new code live and the seeding
    // custom resource running. Caching the empty document that produces would
    // hold the empty answer for the whole of the tier's TTL after seeding had
    // finished, turning a few seconds of missing baseline into five minutes.
    const { table, clock, store } = build();
    await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    const reads = table.requests.length;
    await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.ok(table.requests.length > reads, 'the empty document was served from the cache');

    // The seeder lands. The next read picks it up with no TTL to wait out.
    table.seed(DEFAULT_KEY, { [SECTION]: { '1#SUMMARY': 'shipped summary' } });
    const found = await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.deepEqual(found.values, { '1#SUMMARY': 'shipped summary' });
    const settled = table.requests.length;
    clock.advance(1_000);
    assert.equal((await store.getSection(SUMMARY_PROMPT_TEMPLATES)).servedFromCache, true);
    assert.equal(table.requests.length, settled);
});

test('the read after an invalidate is consistent once', async () => {
    // The one case the tiers' eventual-consistency choice does not cover: that
    // choice rests on a TTL measured in tens of seconds being far longer than
    // DynamoDB's replication lag, and after an invalidate there is no TTL --
    // the read happens immediately after the thing it has to observe.
    const { table, clock, store } = seeded();
    await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    store.invalidate();
    table.requests = [];
    await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.ok(table.requests.length > 0, 'nothing was read');
    assert.ok(table.requests.every((request) => request.consistentRead === true));

    // One-shot: the next refetch of its own accord is the tier's policy again.
    clock.advance(301_000);
    table.requests = [];
    await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.ok(table.requests.length > 0, 'nothing was read');
    assert.ok(table.requests.every((request) => request.consistentRead === false));
});

test('the standard tier serves a cached document within its five minutes', async () => {
    const { table, clock, store } = seeded();
    const first = await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.equal(first.servedFromCache, false);
    const reads = table.requests.length;

    clock.advance(299_000);
    assert.equal((await store.getSection(SUMMARY_PROMPT_TEMPLATES)).servedFromCache, true);
    assert.equal(table.requests.length, reads, 'a cached read still went to DynamoDB');

    clock.advance(2_000);
    assert.equal((await store.getSection(SUMMARY_PROMPT_TEMPLATES)).servedFromCache, false);
    assert.ok(table.requests.length > reads);
});

test('the short tier caches for thirty seconds, which is the ASR switches’ policy', async () => {
    const shortSection = declareSection({
        name: 'per_meeting_switch',
        cacheTier: 'short',
        description: 'a short-tier section used by the tests',
    });
    const { table, clock, store } = build();
    table.seed(DEFAULT_KEY, { per_meeting_switch: { engine: 'microvm' } });

    assert.equal((await store.getSection(shortSection)).servedFromCache, false);
    clock.advance(29_000);
    assert.equal((await store.getSection(shortSection)).servedFromCache, true);
    clock.advance(2_000);
    assert.equal((await store.getSection(shortSection)).servedFromCache, false);
});

test('the immediate tier never serves a cached value, however little time has passed', async () => {
    const immediateSection = declareSection({
        name: 'consent_bearing',
        cacheTier: 'immediate',
        description: 'an immediate-tier section used by the tests',
    });
    const { table, store } = build();
    table.seed(DEFAULT_KEY, { consent_bearing: { enrolled: true } });

    assert.equal((await store.getSection(immediateSection)).servedFromCache, false);
    const reads = table.requests.length;
    assert.equal((await store.getSection(immediateSection)).servedFromCache, false);
    assert.ok(table.requests.length > reads, 'the second read came from a cache');
});

test('the immediate tier reads consistently and the standard tier does not', async () => {
    const immediateSection = declareSection({
        name: 'consent_bearing_read',
        cacheTier: 'immediate',
        description: 'an immediate-tier section used by the tests',
    });
    const { table, store } = seeded();

    // The standard tier's own policy, so the refetch is forced by the clock
    // rather than by invalidate, which arms a consistent read on purpose.
    await store.getSection(SUMMARY_PROMPT_TEMPLATES);
    assert.ok(table.requests.every((request) => request.consistentRead === false));

    table.requests = [];
    table.seed(DEFAULT_KEY, { consent_bearing_read: { enrolled: true } });
    await store.getSection(immediateSection);
    assert.ok(table.requests.length > 0);
    assert.ok(table.requests.every((request) => request.consistentRead === true));
});

// The same values and the same expected digest appear in the Python half's
// suite (lma_config/test_store.py). Neither side can run the other, so this
// pinned vector is what keeps the two implementations agreeing: a caller that
// compared a fingerprint taken by one with one taken by the other would
// otherwise rebuild its derived state forever, or never.
//
// The vector holds the two things two JSON serialisers disagree about by
// default, because a vector of plain ASCII strings would agree whatever either
// half did: non-ASCII text, which Python escapes and this one does not, and an
// integral float, which Python writes as `1.0` and this one as `1`.
const FINGERPRINT_VECTOR = {
    '1#Summary': 'Write a summary. Use ’ quotes, 20°, and “this”.',
    '2#List': ['a', 1, true],
    '3#Nested': { z: null, a: 1.5 },
    '4#Numbers': { integral: 1.0, whole: 512, fraction: 0.25, negative: -3.0 },
};
const FINGERPRINT_VECTOR_DIGEST = 'd24206f131a4a1ad';

test('the fingerprint matches the Python half\'s for a pinned input', async () => {
    const vectorSection = declareSection({
        name: 'fingerprint_vector',
        cacheTier: 'standard',
        description: 'a pinned cross-language fingerprint vector',
    });
    const { table, store } = build();
    table.seed(DEFAULT_KEY, { fingerprint_vector: FINGERPRINT_VECTOR });
    const section = await store.getSection(vectorSection);
    assert.equal(section.fingerprint, FINGERPRINT_VECTOR_DIGEST);
});

test('a reader with a cached document can tell a change from an unchanged refetch', async () => {
    const { table, clock, store } = seeded();
    const first = await store.getSection(SUMMARY_PROMPT_TEMPLATES);

    clock.advance(301_000);
    assert.equal((await store.getSection(SUMMARY_PROMPT_TEMPLATES)).fingerprint, first.fingerprint);

    table.seed('Config#custom', { [SECTION]: { '1#SUMMARY': 'changed' } }, 2);
    clock.advance(301_000);
    assert.notEqual(
        (await store.getSection(SUMMARY_PROMPT_TEMPLATES)).fingerprint,
        first.fingerprint
    );
});

test('a section may not be declared twice with different policies', () => {
    declareSection({ name: 'declared_once', cacheTier: 'short', description: 'first' });
    assert.throws(
        () =>
            declareSection({
                name: 'declared_once',
                cacheTier: 'immediate',
                description: 'a contradictory second declaration',
            }),
        /different caching policy/
    );
});

test('the summary prompt templates section is declared on the same tier as in Python', () => {
    const declared = declaredSections().get('summary_prompt_templates');
    assert.ok(declared);
    assert.equal(declared.cacheTier, 'standard');
});

// ── the merge ──────────────────────────────────────────────────────────────

test('the merge recurses into nested objects', () => {
    assert.deepEqual(deepMerge({ a: { keep: 1, replace: 1 }, b: 1 }, { a: { replace: 2 }, c: 3 }), {
        a: { keep: 1, replace: 2 },
        b: 1,
        c: 3,
    });
});

test('the merge replaces a list rather than appending to it', () => {
    assert.deepEqual(deepMerge({ a: [1, 2, 3] }, { a: [9] }), { a: [9] });
});

test('the merge does not mutate either input', () => {
    const base = { a: { x: 1 } };
    const overlay = { a: { y: 2 } };
    deepMerge(base, overlay);
    assert.deepEqual(base, { a: { x: 1 } });
    assert.deepEqual(overlay, { a: { y: 2 } });
});
