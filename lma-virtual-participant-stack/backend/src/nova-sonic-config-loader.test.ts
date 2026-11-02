/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Where the voice assistant's settings are read from, and how its two layers
 * combine.
 *
 * The settings live in the shared configuration table as the
 * `nova_sonic_config` section. What is asserted here is the part that would be
 * silent when wrong:
 *
 * - The shipped layer and the administrator's layer are combined the way the
 *   previous table's Default and Custom items were. `inject` mode appends the
 *   custom prompt to the shipped one, which a plain merge of the two would lose.
 * - The read is a consistent one, because the section is on the immediate tier:
 *   a meeting starting straight after a save gets what was saved.
 * - The previous table is read only when the configuration table cannot be
 *   read or holds no shipped settings, and never otherwise.
 *
 * DynamoDB is a fake answering GetItem for the configuration table through the
 * real accessor; the previous table is reached through the loader's seam.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { gzipSync } from 'node:zlib';

import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';

import { ConfigurationStore } from './configuration-store.js';
import { details } from './details.js';
import {
  hasNovaSonicConfigTable,
  loadNovaSonicConfig,
  novaSonicConfigTablesFromEnv,
} from './nova-sonic-config-loader.js';
import { routeLogsToConsole } from './logger.js';

// Modules on the structured logger write through console here too, so this
// file's console capture sees their records as it sees console output.
routeLogsToConsole();

const TABLE = 'lma-configuration';

const SHIPPED = {
  systemPrompt: 'You are Alex, an AI meeting assistant. Be concise and helpful.',
  promptMode: 'base',
  modelId: 'amazon.nova-2-sonic-v1:0',
  voiceId: 'tiffany',
  endpointingSensitivity: 'MEDIUM',
  meetingMode: 'normal',
  translatorLanguageA: 'English',
  translatorLanguageB: 'Spanish',
  translatorMutePhrases: 'translator mute, alex mute',
  translatorUnmutePhrases: 'translator unmute, alex unmute',
  groupMeetingMode: false,
};

class FakeConfigurationTable {
  items = new Map<string, Record<string, AttributeValue>>();

  consistentReads: boolean[] = [];

  fail = false;

  seed(key: string, body: unknown): void {
    this.items.set(key, {
      Configuration: { S: key },
      Version: { N: '1' },
      Body: { B: gzipSync(Buffer.from(JSON.stringify(body), 'utf-8')) },
    });
  }

  pointAt(profile: string): void {
    this.items.set('Config#__active', {
      Configuration: { S: 'Config#__active' },
      ActiveProfile: { S: profile },
    });
  }

  asClient(): DynamoDBClient {
    return {
      send: async (command: { input: Record<string, unknown> }) => {
        if (this.fail) {
          throw new Error('AccessDeniedException');
        }
        const input = command.input as { Key: Record<string, { S: string }>; ConsistentRead?: boolean };
        this.consistentReads.push(input.ConsistentRead === true);
        const item = this.items.get(input.Key['Configuration']!.S);
        return item ? { Item: item } : {};
      },
    } as unknown as DynamoDBClient;
  }
}

const seeded = (profileSection?: Record<string, unknown>): FakeConfigurationTable => {
  const table = new FakeConfigurationTable();
  table.seed('Config#default', { nova_sonic_config: SHIPPED, summary_prompt_templates: {} });
  table.seed(
    'Config#custom',
    profileSection === undefined ? { summary_prompt_templates: {} } : { nova_sonic_config: profileSection }
  );
  table.pointAt('custom');
  return table;
};

/** A previous-table reader that records whether it was asked. */
const legacy = (customConfig: Record<string, unknown> | null = null) => {
  const calls: string[] = [];
  return {
    calls,
    readLegacy: async (_client: DynamoDBClient, tableName: string) => {
      calls.push(tableName);
      return { defaultConfig: { ...SHIPPED, voiceId: 'from-the-previous-table' }, customConfig };
    },
  };
};

const load = async (
  table: FakeConfigurationTable,
  previous = legacy(),
  tables = { configurationTableName: TABLE, legacyTableName: 'old-nova-table' }
) => {
  const client = table.asClient();
  const config = await loadNovaSonicConfig(client, tables, {
    storeFor: (tableName, storeClient) => new ConfigurationStore({ tableName, client: storeClient }),
    readLegacy: previous.readLegacy,
  });
  return { config, previous };
};

// Building the config logs; keep the test report readable.
const quiet = <T>(run: () => Promise<T>): Promise<T> => {
  const saved = { log: console.log, warn: console.warn, error: console.error };
  console.log = () => undefined;
  console.warn = () => undefined;
  console.error = () => undefined;
  return run().finally(() => Object.assign(console, saved));
};

test('the shipped settings apply when the profile overrides nothing', () =>
  quiet(async () => {
    const { config, previous } = await load(seeded());
    assert.equal(config.systemPrompt, SHIPPED.systemPrompt);
    assert.equal(config.voiceId, 'tiffany');
    assert.equal(config.meetingMode, 'normal');
    assert.deepEqual(config.translatorMutePhrases, ['translator mute', 'alex mute']);
    assert.deepEqual(previous.calls, [], 'the previous table was read');
  }));

test("an administrator's settings override the shipped ones", () =>
  quiet(async () => {
    const { config } = await load(
      seeded({ meetingMode: 'translator', translatorLanguageA: 'French', translatorLanguageB: 'German' })
    );
    assert.equal(config.meetingMode, 'translator');
    assert.equal(config.translatorLanguageA, 'French');
    assert.equal(config.translatorLanguageB, 'German');
    assert.equal(config.voiceId, 'tiffany');
  }));

test('inject mode appends the custom prompt to the shipped one rather than replacing it', () =>
  quiet(async () => {
    const { config } = await load(seeded({ promptMode: 'inject', systemPrompt: 'Answer in French.' }));
    assert.equal(config.systemPrompt, `${SHIPPED.systemPrompt}\n\nAnswer in French.`);
  }));

test('base mode uses the custom prompt on its own', () =>
  quiet(async () => {
    const { config } = await load(seeded({ systemPrompt: 'You are Sam.' }));
    assert.equal(config.systemPrompt, 'You are Sam.');
  }));

test('the legacy group flag still selects group mode', () =>
  quiet(async () => {
    const { config } = await load(seeded({ groupMeetingMode: true }));
    assert.equal(config.meetingMode, 'group');
    assert.equal(config.groupMeetingMode, true);
  }));

test('a reset profile section reads as the shipped settings', () =>
  quiet(async () => {
    const { config } = await load(seeded({}));
    assert.equal(config.systemPrompt, SHIPPED.systemPrompt);
    assert.equal(config.promptMode, 'base');
  }));

test('the read is strongly consistent, so a save applies to the next meeting', () =>
  quiet(async () => {
    const table = seeded({ voiceId: 'matthew' });
    await load(table);
    assert.ok(table.consistentReads.length > 0);
    assert.ok(table.consistentReads.every(Boolean), 'an eventually consistent read was issued');
  }));

test('the loaded meeting mode is published for the transcription path', () =>
  quiet(async () => {
    await load(seeded({ meetingMode: 'translator' }));
    assert.equal(details.meetingMode, 'translator');
    await load(seeded({}));
    assert.equal(details.meetingMode, 'normal');
  }));

test('a configuration table with no shipped settings falls back to the previous table', () =>
  quiet(async () => {
    const table = new FakeConfigurationTable();
    table.seed('Config#default', { summary_prompt_templates: {} });
    table.seed('Config#custom', { nova_sonic_config: { voiceId: 'matthew' } });
    table.pointAt('custom');

    const { config, previous } = await load(table, legacy({ voiceId: 'amy' }));

    assert.deepEqual(previous.calls, ['old-nova-table']);
    assert.equal(config.voiceId, 'amy', "the profile's layer was used without a shipped layer beneath it");
  }));

test('a configuration table that cannot be read falls back to the previous table', () =>
  quiet(async () => {
    const table = seeded({ voiceId: 'matthew' });
    table.fail = true;
    const { config, previous } = await load(table);
    assert.deepEqual(previous.calls, ['old-nova-table']);
    assert.equal(config.voiceId, 'from-the-previous-table');
  }));

test('with only the previous table named, it is read as before', () =>
  quiet(async () => {
    const { config, previous } = await load(seeded(), legacy({ translatorLanguageA: 'Italian' }), {
      configurationTableName: '',
      legacyTableName: 'old-nova-table',
    });
    assert.deepEqual(previous.calls, ['old-nova-table']);
    assert.equal(config.translatorLanguageA, 'Italian');
  }));

test('nothing readable anywhere gives the built-in defaults', () =>
  quiet(async () => {
    const table = seeded();
    table.fail = true;
    const { config } = await load(table, legacy(), { configurationTableName: TABLE, legacyTableName: '' });
    assert.equal(config.systemPrompt, SHIPPED.systemPrompt);
    assert.equal(config.meetingMode, 'normal');
  }));

test('the tables come from the two environment variables', () => {
  assert.deepEqual(
    novaSonicConfigTablesFromEnv({ CONFIGURATION_TABLE_NAME: 'config', NOVA_SONIC_CONFIG_TABLE_NAME: 'old' }),
    { configurationTableName: 'config', legacyTableName: 'old' }
  );
  assert.equal(hasNovaSonicConfigTable(novaSonicConfigTablesFromEnv({})), false);
  assert.equal(hasNovaSonicConfigTable(novaSonicConfigTablesFromEnv({ CONFIGURATION_TABLE_NAME: 'c' })), true);
});
