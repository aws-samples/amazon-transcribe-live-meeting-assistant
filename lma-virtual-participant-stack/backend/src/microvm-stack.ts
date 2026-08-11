/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Brings up (and health-checks) the pre-snapshot VP stack under Lambda MicroVMs.
 *
 * The boot itself is delegated to the existing `entrypoint.sh` with
 * `STACK_ONLY=true`, deliberately: that script owns the Xvfb / fluxbox / x11vnc
 * / websockify / PulseAudio sequence including the three-sink barge-in routing.
 * Reimplementing it here would duplicate ~200 lines of shell and let the ECS and
 * MicroVM paths drift apart.
 */
import { spawn } from 'child_process';
import { connect } from 'net';

const ENTRYPOINT = process.env.VP_ENTRYPOINT || '/srv/entrypoint.sh';

/** noVNC (websockify) and the raw VNC port that x11vnc serves. */
export const NOVNC_PORT = parseInt(process.env.NOVNC_PORT || '5901', 10);
export const VNC_PORT = parseInt(process.env.VNC_PORT || '5900', 10);

/** Resolve true when a TCP port accepts a connection on localhost. */
export function portOpen(port: number, timeoutMs = 2000): Promise<boolean> {
    return new Promise((resolve) => {
        const socket = connect({ port, host: '127.0.0.1' });
        let settled = false;
        const done = (value: boolean): void => {
            if (settled) return;
            settled = true;
            socket.destroy();
            resolve(value);
        };
        socket.setTimeout(timeoutMs);
        socket.once('connect', () => done(true));
        socket.once('error', () => done(false));
        socket.once('timeout', () => done(false));
    });
}

/**
 * True when the pre-snapshot stack is serving. Both ports must be up: 5900 means
 * x11vnc has the X display, 5901 means websockify is proxying it to the browser.
 */
export async function stackHealthy(): Promise<boolean> {
    const [vnc, novnc] = await Promise.all([portOpen(VNC_PORT), portOpen(NOVNC_PORT)]);
    return vnc && novnc;
}

/**
 * Run `entrypoint.sh` in STACK_ONLY mode, then wait for the stack to serve.
 *
 * The script exits 0 once the stack is backgrounded, so completion of the child
 * is not sufficient evidence of health — we poll the ports afterwards.
 */
export async function bootStack(timeoutMs = 180_000): Promise<boolean> {
    await new Promise<void>((resolve) => {
        const child = spawn('bash', [ENTRYPOINT], {
            env: { ...process.env, STACK_ONLY: 'true' },
            stdio: 'inherit',
        });
        child.on('exit', (code) => {
            console.log(`[microvm-stack] entrypoint STACK_ONLY exited code=${code}`);
            resolve();
        });
        child.on('error', (err) => {
            console.error('[microvm-stack] failed to spawn entrypoint:', err);
            resolve();
        });
    });

    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        if (await stackHealthy()) return true;
        await new Promise((r) => setTimeout(r, 1000));
    }
    console.error('[microvm-stack] stack did not become healthy before timeout');
    return false;
}
