/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import type { ChildProcess } from 'child_process';
import { Supervisor, hookNameFromPath, type SupervisorDeps } from './microvm-supervisor.js';

/** Minimal ChildProcess stand-in: records kill signals, never spawns anything. */
function fakeChild(): ChildProcess & { killed: boolean; signals: string[] } {
    const signals: string[] = [];
    return {
        killed: false,
        signals,
        kill(sig?: string) {
            signals.push(sig || 'SIGTERM');
            return true;
        },
        on() {
            return this;
        },
    } as unknown as ChildProcess & { killed: boolean; signals: string[] };
}

interface Harness {
    sup: Supervisor;
    env: Record<string, string | undefined>;
    logs: string[];
    spawned: Array<Record<string, string | undefined>>;
    child: ChildProcess & { killed: boolean; signals: string[] };
    bootCalls: () => number;
    fetchCalls: string[];
    warmCalls: number[];
}

function harness(
    opts: {
        boot?: boolean;
        healthy?: boolean | boolean[];
        staged?: Record<string, string>;
        fetchThrows?: boolean;
        warmFails?: boolean;
    } = {},
): Harness {
    const logs: string[] = [];
    const spawned: Array<Record<string, string | undefined>> = [];
    const fetchCalls: string[] = [];
    const warmCalls: number[] = [];
    const env: Record<string, string | undefined> = {};
    const child = fakeChild();
    let bootCalls = 0;
    const healthSeq = Array.isArray(opts.healthy) ? [...opts.healthy] : undefined;

    const deps: SupervisorDeps = {
        bootStack: async () => {
            bootCalls += 1;
            return opts.boot !== false;
        },
        stackHealthy: async () => {
            if (healthSeq) return healthSeq.shift() ?? false;
            return opts.healthy !== false;
        },
        spawnApp: (e) => {
            spawned.push({ ...e });
            return child;
        },
        warmWorkload: async () => {
            warmCalls.push(1);
            return opts.warmFails !== true;
        },
        fetchConfig: async (vpId) => {
            fetchCalls.push(vpId);
            if (opts.fetchThrows) throw new Error('registry unavailable');
            return opts.staged ?? {};
        },
        log: (m) => logs.push(m),
    };
    return {
        sup: new Supervisor(deps, env),
        env,
        logs,
        spawned,
        child,
        bootCalls: () => bootCalls,
        fetchCalls,
        warmCalls,
    };
}

test('hookNameFromPath extracts hook names from the runtime prefix', () => {
    const p = '/aws/lambda-microvms/runtime/v1';
    assert.equal(hookNameFromPath(`${p}/ready`), 'ready');
    assert.equal(hookNameFromPath(`${p}/run`), 'run');
    assert.equal(hookNameFromPath(`${p}/terminate`), 'terminate');
    assert.equal(hookNameFromPath(`${p}/run?x=1`), 'run');
    assert.equal(hookNameFromPath(`${p}/run/`), 'run');
});

test('hookNameFromPath returns undefined for non-hook paths', () => {
    assert.equal(hookNameFromPath('/health'), undefined);
    assert.equal(hookNameFromPath('/'), undefined);
    assert.equal(hookNameFromPath(undefined), undefined);
    assert.equal(hookNameFromPath('/aws/lambda-microvms/runtime/v1'), undefined);
});

test('/ready boots the stack once and returns 200 when healthy', async () => {
    const h = harness();
    assert.equal(await h.sup.onReady(), 200);
    assert.equal(h.bootCalls(), 1);
    // Lambda retries /ready; the stack must not be booted again each time.
    assert.equal(await h.sup.onReady(), 200);
    assert.equal(h.bootCalls(), 1);
});

test('/ready returns 503 while the stack is not yet healthy', async () => {
    // 503 tells Lambda to retry rather than snapshotting a half-booted stack —
    // the snapshot is taken the moment /ready returns 200.
    const h = harness({ healthy: [false, true] });
    assert.equal(await h.sup.onReady(), 503);
    assert.equal(await h.sup.onReady(), 200);
});

test('/ready returns 503 when the boot itself fails', async () => {
    const h = harness({ boot: false });
    assert.equal(await h.sup.onReady(), 503);
});

test('/validate exercises the real workload so Lambda prefetches its pages', async () => {
    // This is the fix for a ~142s browser launch: Lambda samples the snapshot
    // pages touched during /validate and prefetches them on later launches. A
    // port-only health check never touched Chromium, so none of its ~200 MB was
    // prefetched and the first real launch faulted it all in on demand.
    const h = harness();
    assert.equal(await h.sup.onValidate(), 200);
    assert.equal(h.warmCalls.length, 1, 'validate must exercise the workload');
});

test('/validate returns 503 before the stack is healthy, without warming', async () => {
    const h = harness({ healthy: false });
    assert.equal(await h.sup.onValidate(), 503);
    assert.equal(h.warmCalls.length, 0, 'no point warming an unhealthy stack');
});

test('/validate still succeeds when the warm-up fails', async () => {
    // A missed prefetch costs startup latency; failing validate would fail the
    // entire image build, which is far worse.
    const h = harness({ warmFails: true });
    assert.equal(await h.sup.onValidate(), 200);
});

test('/run applies per-meeting config to the env and spawns the app', async () => {
    const h = harness();
    const payload = JSON.stringify({
        MEETING_PLATFORM: 'Teams',
        MEETING_ID: '243574196567966',
        VIRTUAL_PARTICIPANT_ID: 'vp-9',
    });
    const status = await h.sup.onRun(JSON.stringify({ microvmId: 'mvm-7', runHookPayload: payload }));

    assert.equal(status, 200);
    assert.equal(h.spawned.length, 1);
    assert.equal(h.env.MEETING_PLATFORM, 'Teams');
    assert.equal(h.env.MEETING_ID, '243574196567966');
    assert.equal(h.env.VIRTUAL_PARTICIPANT_ID, 'vp-9');
    assert.equal(h.spawned[0].MEETING_ID, '243574196567966');
    assert.equal(h.sup.state.microvmId, 'mvm-7');
    assert.equal(h.sup.state.appStarted, true);
});

test('/run never spawns the app twice', async () => {
    // Two browsers on one X display would double-join the meeting.
    const h = harness();
    const body = JSON.stringify({ microvmId: 'm', runHookPayload: '{"MEETING_ID":"1"}' });
    assert.equal(await h.sup.onRun(body), 200);
    assert.equal(await h.sup.onRun(body), 200);
    assert.equal(h.spawned.length, 1);
});

test('/run still starts the app when the payload is empty or malformed', async () => {
    // Starting lets the app publish a FAILED status with a legible reason;
    // refusing to start would leave the UI with no signal at all.
    const h = harness();
    assert.equal(await h.sup.onRun('not json'), 200);
    assert.equal(h.spawned.length, 1);
    assert.ok(h.logs.some((l) => l.includes('no per-meeting config')));
});

test('/run does not log secret values', async () => {
    const h = harness();
    await h.sup.onRun(
        JSON.stringify({
            microvmId: 'm',
            runHookPayload: JSON.stringify({
                USER_ACCESS_TOKEN: 'super-secret-token',
                MEETING_PASSWORD: 'hunter2',
                MEETING_ID: '5',
            }),
        }),
    );
    const all = h.logs.join('\n');
    assert.ok(!all.includes('super-secret-token'), 'access token must not be logged');
    assert.ok(!all.includes('hunter2'), 'meeting password must not be logged');
    assert.ok(all.includes('<redacted:'), 'redaction marker expected');
    assert.ok(all.includes('MEETING_ID'), 'non-secret keys should still be visible');
});

test('/terminate signals the app with SIGTERM so its shutdown path runs', async () => {
    // SIGTERM (not SIGKILL) so recording flush / Kinesis end-meeting / status
    // update all still happen.
    const h = harness();
    await h.sup.onRun(JSON.stringify({ microvmId: 'm', runHookPayload: '{"MEETING_ID":"1"}' }));
    assert.equal(await h.sup.onTerminate(), 200);
    assert.deepEqual(h.child.signals, ['SIGTERM']);
});

test('/terminate is safe when the app was never started', async () => {
    const h = harness();
    assert.equal(await h.sup.onTerminate(), 200);
    assert.deepEqual(h.child.signals, []);
});

test('suspend and resume are no-op 200s and are recorded', async () => {
    const h = harness();
    assert.equal(await h.sup.onSuspend(), 200);
    assert.equal(await h.sup.onResume(), 200);
    assert.deepEqual(h.sup.state.hooksSeen, ['suspend', 'resume']);
});

test('dispatch routes every known hook and tolerates unknown ones', async () => {
    const h = harness();
    assert.equal(await h.sup.dispatch('ready', ''), 200);
    assert.equal(await h.sup.dispatch('validate', ''), 200);
    assert.equal(await h.sup.dispatch('run', '{"runHookPayload":"{\\"MEETING_ID\\":\\"1\\"}"}'), 200);
    assert.equal(await h.sup.dispatch('suspend', ''), 200);
    assert.equal(await h.sup.dispatch('resume', ''), 200);
    assert.equal(await h.sup.dispatch('terminate', ''), 200);
    assert.equal(await h.sup.dispatch('bogus', ''), 200);
});

test('image-level env wins over the run payload', async () => {
    // Anything explicitly baked into the image (or set by a dev harness) should
    // not be silently overridden by the launcher.
    const h = harness();
    h.env.MEETING_ID = 'baked-in';
    await h.sup.onRun(JSON.stringify({ runHookPayload: '{"MEETING_ID":"from-hook"}' }));
    assert.equal(h.env.MEETING_ID, 'baked-in');
});

test('/run fetches staged config from the registry using the vpId pointer', async () => {
    // The service enforces a 4096-byte runHookPayload (the developer guide's
    // 16 KB is wrong), and three Cognito JWTs alone are ~3.6 KB — so the payload
    // carries only the vpId and the real config comes from the registry.
    const h = harness({
        staged: {
            GRAPHQL_ENDPOINT: 'https://gql.example.com/graphql',
            RECORDINGS_BUCKET_NAME: 'bucket',
            MEETING_ID: 'from-registry',
        },
    });
    await h.sup.onRun(
        JSON.stringify({
            microvmId: 'mvm-1',
            runHookPayload: JSON.stringify({ VIRTUAL_PARTICIPANT_ID: 'vp-42' }),
        }),
    );
    assert.deepEqual(h.fetchCalls, ['vp-42']);
    assert.equal(h.env.GRAPHQL_ENDPOINT, 'https://gql.example.com/graphql');
    assert.equal(h.env.RECORDINGS_BUCKET_NAME, 'bucket');
    assert.equal(h.env.MEETING_ID, 'from-registry');
    assert.equal(h.spawned.length, 1);
});

test('/run lets inline payload values win over the registry', async () => {
    // Keeps a future larger payload limit working unchanged: anything sent
    // inline overrides the staged copy.
    const h = harness({ staged: { MEETING_ID: 'stale', MEETING_NAME: 'from-registry' } });
    await h.sup.onRun(
        JSON.stringify({
            runHookPayload: JSON.stringify({
                VIRTUAL_PARTICIPANT_ID: 'vp-1',
                MEETING_ID: 'inline-wins',
            }),
        }),
    );
    assert.equal(h.env.MEETING_ID, 'inline-wins');
    assert.equal(h.env.MEETING_NAME, 'from-registry');
});

test('/run still starts the app when the registry read fails', async () => {
    // Starting lets the app publish a FAILED status with a legible reason;
    // refusing to start would leave the UI with no signal at all.
    const h = harness({ fetchThrows: true });
    assert.equal(
        await h.sup.onRun(
            JSON.stringify({ runHookPayload: JSON.stringify({ VIRTUAL_PARTICIPANT_ID: 'vp-9' }) }),
        ),
        200,
    );
    assert.equal(h.spawned.length, 1);
    assert.ok(h.logs.some((l) => l.includes('could not fetch staged config')));
});

test('/run does not query the registry without a vpId', async () => {
    const h = harness();
    await h.sup.onRun(JSON.stringify({ runHookPayload: JSON.stringify({ MEETING_ID: '1' }) }));
    assert.deepEqual(h.fetchCalls, []);
});
