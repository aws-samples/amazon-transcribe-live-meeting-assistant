/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Lambda MicroVMs supervisor / entrypoint for the Virtual Participant.
 *
 * Why a supervisor rather than starting the app directly:
 *
 *  - MicroVMs boot from a memory+disk snapshot taken at image-build time. The
 *    snapshot is captured when the `/ready` hook returns 200, so everything that
 *    should be pre-warmed (Xvfb, fluxbox, x11vnc, websockify, PulseAudio) must
 *    be running BEFORE that. That is the whole startup-latency win.
 *  - Per-meeting config is NOT available at snapshot time. Image environment
 *    variables are shared by every MicroVM launched from the image, so the
 *    meeting id, tokens, etc. arrive later in the `/run` hook body.
 *  - `details.ts` builds its singleton from `process.env` at module-import time.
 *    So instead of refactoring every config read site, the supervisor applies
 *    the `/run` payload to the environment and only THEN spawns the app as a
 *    child process. The app itself is unchanged.
 *
 * Lifecycle:
 *
 *    build time:  boot stack -> /ready 200 -> SNAPSHOT -> /validate 200
 *    run time:    resume from snapshot -> /run {payload} -> spawn VP app
 *    teardown:    /terminate -> stop app
 */
import { spawn, type ChildProcess } from 'child_process';
import { createServer, type IncomingMessage, type ServerResponse } from 'http';
import { realpathSync } from 'fs';
import { pathToFileURL } from 'url';
import {
    parseRunHookPayload,
    applyPerMeetingConfig,
    redactPerMeetingConfig,
    type PerMeetingConfig,
} from './launch-mode.js';

/** Hooks are served under this prefix; the port is set in the image config. */
const HOOK_PREFIX = '/aws/lambda-microvms/runtime/v1';
const HOOK_PORT = parseInt(process.env.HOOK_PORT || '9000', 10);

/** Where the VP application entrypoint lives inside the image. */
const APP_ENTRY = process.env.VP_APP_ENTRY || '/srv/dist/index.js';

export interface SupervisorDeps {
    /** Boot the pre-snapshot stack (Xvfb, VNC, audio). Resolves when healthy. */
    bootStack: () => Promise<boolean>;
    /** True when the pre-snapshot stack is currently healthy. */
    stackHealthy: () => Promise<boolean>;
    /** Spawn the VP application with the given environment. */
    spawnApp: (env: Record<string, string | undefined>) => ChildProcess;
    log: (msg: string) => void;
}

export interface SupervisorState {
    booted: boolean;
    appStarted: boolean;
    microvmId?: string;
    hooksSeen: string[];
}

/**
 * Core hook-handling logic, separated from the HTTP server so it can be unit
 * tested without binding a port or launching real processes.
 */
export class Supervisor {
    readonly state: SupervisorState = { booted: false, appStarted: false, hooksSeen: [] };
    private app?: ChildProcess;

    constructor(
        private deps: SupervisorDeps,
        private env: Record<string, string | undefined>,
    ) {}

    /**
     * `/ready` — called during image build. Returning 200 triggers the snapshot,
     * so we must not return 200 until the stack is genuinely up. 503 asks Lambda
     * to retry (observed ~3 retries in practice).
     */
    async onReady(): Promise<number> {
        this.state.hooksSeen.push('ready');
        if (!this.state.booted) {
            this.deps.log('[ready] booting pre-snapshot stack');
            this.state.booted = await this.deps.bootStack();
        }
        const healthy = this.state.booted && (await this.deps.stackHealthy());
        this.deps.log(`[ready] healthy=${healthy}`);
        return healthy ? 200 : 503;
    }

    /**
     * `/validate` — called after a test run from the snapshot. Also lets Lambda
     * sample which snapshot pages are touched so it can prefetch them, which is
     * why it is worth implementing even though it only reports health.
     */
    async onValidate(): Promise<number> {
        this.state.hooksSeen.push('validate');
        const healthy = await this.deps.stackHealthy();
        this.deps.log(`[validate] healthy=${healthy}`);
        return healthy ? 200 : 503;
    }

    /**
     * `/run` — fires once per MicroVM, carrying this meeting's config. Traffic is
     * only forwarded to the app after this returns 200, so the response must not
     * wait on the meeting itself; we spawn the app and return immediately.
     */
    async onRun(body: string): Promise<number> {
        this.state.hooksSeen.push('run');
        if (this.state.appStarted) {
            // Defensive: /run should fire exactly once. Spawning twice would put
            // two browsers on one display and double-join the meeting.
            this.deps.log('[run] already started, ignoring duplicate');
            return 200;
        }

        let parsedId: string | undefined;
        try {
            parsedId = (JSON.parse(body) as { microvmId?: string }).microvmId;
        } catch {
            parsedId = undefined;
        }
        this.state.microvmId = parsedId;

        const config: PerMeetingConfig = parseRunHookPayload(body);
        const applied = applyPerMeetingConfig(this.env, config);
        this.deps.log(
            `[run] microvmId=${parsedId ?? 'unknown'} applied=${applied.join(',') || '(none)'} ` +
                `config=${JSON.stringify(redactPerMeetingConfig(config))}`,
        );

        if (Object.keys(config).length === 0) {
            // Start anyway: the app publishes a FAILED status with a legible
            // reason, which is far easier to debug than a container that never
            // starts and reports nothing.
            this.deps.log('[run] WARNING: no per-meeting config in payload');
        }

        this.app = this.deps.spawnApp(this.env);
        this.state.appStarted = true;
        return 200;
    }

    /** `/suspend` and `/resume` are fast notifications; nothing to do today. */
    async onSuspend(): Promise<number> {
        this.state.hooksSeen.push('suspend');
        return 200;
    }

    async onResume(): Promise<number> {
        this.state.hooksSeen.push('resume');
        return 200;
    }

    /** `/terminate` — flush and stop the app before the VM goes away. */
    async onTerminate(): Promise<number> {
        this.state.hooksSeen.push('terminate');
        if (this.app && !this.app.killed) {
            this.deps.log('[terminate] signalling app to exit');
            // SIGTERM so the app's own shutdown path runs (recording flush,
            // Kinesis end-meeting event, status update).
            this.app.kill('SIGTERM');
        }
        return 200;
    }

    /** Route a hook name to its handler. Unknown hooks are a no-op 200. */
    async dispatch(hook: string, body: string): Promise<number> {
        switch (hook) {
            case 'ready':
                return this.onReady();
            case 'validate':
                return this.onValidate();
            case 'run':
                return this.onRun(body);
            case 'suspend':
                return this.onSuspend();
            case 'resume':
                return this.onResume();
            case 'terminate':
                return this.onTerminate();
            default:
                this.deps.log(`[hook] unknown hook '${hook}'`);
                return 200;
        }
    }
}

/** Extract the hook name from a request path, or undefined if not a hook path. */
export function hookNameFromPath(path: string | undefined): string | undefined {
    if (!path) return undefined;
    const clean = path.split('?')[0].replace(/\/+$/, '');
    if (!clean.startsWith(HOOK_PREFIX)) return undefined;
    const name = clean.slice(HOOK_PREFIX.length).replace(/^\//, '');
    return name === '' ? undefined : name;
}

/* c8 ignore start - process wiring, exercised by the container e2e test */

function defaultSpawnApp(env: Record<string, string | undefined>): ChildProcess {
    const child = spawn('node', [APP_ENTRY], {
        env: env as NodeJS.ProcessEnv,
        stdio: 'inherit',
        detached: false,
    });
    child.on('exit', (code, signal) => {
        console.log(`[supervisor] VP app exited code=${code} signal=${signal}`);
        // Mirror the app's exit status so MicroVM logs/metrics reflect reality.
        process.exit(code === null ? 1 : code);
    });
    return child;
}

async function main(): Promise<void> {
    const { bootStack, stackHealthy } = await import('./microvm-stack.js');
    const supervisor = new Supervisor(
        {
            bootStack,
            stackHealthy,
            spawnApp: defaultSpawnApp,
            log: (m) => console.log(`[supervisor] ${m}`),
        },
        process.env as Record<string, string | undefined>,
    );

    const server = createServer((req: IncomingMessage, res: ServerResponse) => {
        const chunks: Buffer[] = [];
        req.on('data', (c: Buffer) => chunks.push(c));
        req.on('end', () => {
            void (async () => {
                const body = Buffer.concat(chunks).toString('utf8');
                const hook = hookNameFromPath(req.url);
                if (hook === undefined) {
                    // /health is handy for manual poking via the MicroVM endpoint.
                    res.writeHead(200, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ ok: true, state: supervisor.state }));
                    return;
                }
                let status = 500;
                try {
                    status = await supervisor.dispatch(hook, body);
                } catch (err) {
                    console.error(`[supervisor] hook ${hook} threw:`, err);
                }
                res.writeHead(status, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ hook, status, state: supervisor.state }));
            })();
        });
    });

    server.listen(HOOK_PORT, '0.0.0.0', () => {
        console.log(`[supervisor] hooks listening on :${HOOK_PORT}${HOOK_PREFIX}`);
    });
}

// Start the server only when this module IS the process entrypoint.
//
// A substring check on argv[1] is not enough: the unit-test file is also named
// `microvm-supervisor*`, so importing it would bind port 9000 and the test run
// would fail with EADDRINUSE. Compare resolved paths instead.
const isEntrypoint = ((): boolean => {
    const invoked = process.argv[1];
    if (!invoked) return false;
    try {
        return pathToFileURL(realpathSync(invoked)).href === import.meta.url;
    } catch {
        return false;
    }
})();

if (isEntrypoint) {
    void main();
}

/* c8 ignore stop */
