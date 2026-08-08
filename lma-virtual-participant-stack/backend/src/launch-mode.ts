/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * Launch-mode abstraction for the Virtual Participant container.
 *
 * The VP can be started three ways, and they differ in how per-meeting
 * configuration arrives and how the browser view is exposed:
 *
 *   ECS (EC2 | FARGATE)  -- config arrives as container environment variables
 *                           (ECS `ContainerOverrides.Environment`); the container
 *                           discovers its own IP from the ECS task metadata
 *                           endpoint and registers itself with an ALB target
 *                           group so the UI can reach noVNC.
 *
 *   MICROVM              -- config arrives in the body of the Lambda MicroVMs
 *                           `/run` lifecycle hook, because MicroVM image
 *                           environment variables are baked into the image and
 *                           SHARED by every MicroVM launched from it. There is
 *                           no ALB: `RunMicrovm` returns a dedicated HTTPS
 *                           endpoint, so the launcher already knows the address
 *                           before the container starts and publishes it. The
 *                           container must NOT try to self-register.
 *
 * This module deliberately contains no AWS calls and no I/O so it can be unit
 * tested directly.
 */

export type LaunchType = 'EC2' | 'FARGATE' | 'MICROVM';

/** Values the launcher supplies per meeting (as opposed to per image/task). */
export const PER_MEETING_KEYS = [
    'VIRTUAL_PARTICIPANT_ID',
    'MEETING_PLATFORM',
    'MEETING_ID',
    'MEETING_PASSWORD',
    'MEETING_NAME',
    'MEETING_TIME',
    'LMA_USER',
    'LMA_USER_SUB',
    'USER_ACCESS_TOKEN',
    'USER_ID_TOKEN',
    'USER_REFRESH_TOKEN',
    'ZOOM_CREDENTIALS_SECRET_NAME',
    'ENABLE_VIDEO_RECORDING',
] as const;

export type PerMeetingKey = (typeof PER_MEETING_KEYS)[number];

/**
 * Static, per-stack configuration the launcher forwards in the same payload
 * (GraphQL endpoint, S3 buckets, Transcribe settings, voice-assistant config...).
 *
 * On ECS these arrive as ~60 container environment variables from the task
 * definition. They cannot be MicroVM image environment variables: the image caps
 * at 50, and they are per-stack values, so baking them in would force an image
 * rebuild on every parameter change. Measured at ~3.7 KB, well inside the 16 KB
 * runHookPayload budget alongside the per-meeting values.
 *
 * Accepted keys are allow-listed by SHAPE rather than enumerated, so adding a
 * variable to the template does not require a container change — while still
 * refusing anything that could hijack the process (PATH, LD_*, AWS credentials).
 */
const BLOCKED_ENV_KEYS = new Set([
    'PATH',
    'HOME',
    'USER',
    'SHELL',
    'NODE_OPTIONS',
    'AWS_ACCESS_KEY_ID',
    'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN',
    'VP_LAUNCH_TYPE',
    'STACK_ONLY',
]);

/**
 * True when a payload key may be applied to the environment.
 *
 * Upper snake case only, and never one of the blocked names above. This is what
 * stops a malformed or tampered payload from replacing PATH or injecting
 * credentials.
 */
export function isAllowedConfigKey(key: string): boolean {
    if (BLOCKED_ENV_KEYS.has(key)) return false;
    if (key.startsWith('LD_') || key.startsWith('AWS_ACCESS') || key.startsWith('AWS_SECRET')) {
        return false;
    }
    return /^[A-Z][A-Z0-9_]*$/.test(key);
}

/**
 * Hard limit on `runHookPayload` (RunMicrovm). AWS documents 16 KB / 16,384
 * bytes. We refuse to build a payload larger than this rather than let the
 * service truncate it -- silently losing, say, USER_REFRESH_TOKEN would surface
 * much later as a confusing auth failure inside the meeting.
 */
export const RUN_HOOK_PAYLOAD_MAX_BYTES = 16384;

/**
 * Resolve the launch type. Defaults to FARGATE to match the pre-existing
 * default in the scheduler Lambda (`os.environ.get('VP_LAUNCH_TYPE','FARGATE')`)
 * so behaviour is unchanged when the variable is absent.
 */
export function resolveLaunchType(raw: string | undefined): LaunchType {
    const v = (raw || '').trim().toUpperCase();
    if (v === 'MICROVM') return 'MICROVM';
    if (v === 'EC2') return 'EC2';
    if (v === 'FARGATE') return 'FARGATE';
    return 'FARGATE';
}

/** True when running under Lambda MicroVMs rather than ECS. */
export function isMicrovmLaunch(raw: string | undefined): boolean {
    return resolveLaunchType(raw) === 'MICROVM';
}

/**
 * True when the container is expected to register itself with an ALB target
 * group. Only ECS launch types have an ALB in front of noVNC; under MicroVMs
 * the per-MicroVM HTTPS endpoint replaces it entirely.
 */
export function requiresAlbSelfRegistration(raw: string | undefined): boolean {
    return !isMicrovmLaunch(raw);
}

/** Per-meeting config as delivered by a launcher. */
export type PerMeetingConfig = Partial<Record<PerMeetingKey, string>>;

/**
 * Body Lambda POSTs to the `/run` hook. `microvmId` is injected by the
 * platform; `runHookPayload` is the opaque string we supplied to RunMicrovm.
 */
export interface RunHookBody {
    microvmId?: string;
    runHookPayload?: string;
}

/**
 * Build the `runHookPayload` string for RunMicrovm from per-meeting config.
 *
 * Throws when the result would exceed the service limit -- see
 * RUN_HOOK_PAYLOAD_MAX_BYTES for why failing loudly is the right call.
 */
export function buildRunHookPayload(config: PerMeetingConfig): string {
    const clean: Record<string, string> = {};
    for (const key of PER_MEETING_KEYS) {
        const value = config[key];
        // Omit empty values rather than shipping empty strings: the app's own
        // `process.env.X || default` fallbacks then apply as they do on ECS.
        if (value !== undefined && value !== null && value !== '') {
            clean[key] = String(value);
        }
    }
    const payload = JSON.stringify(clean);
    const bytes = Buffer.byteLength(payload, 'utf8');
    if (bytes > RUN_HOOK_PAYLOAD_MAX_BYTES) {
        throw new Error(
            `runHookPayload is ${bytes} bytes, exceeding the ${RUN_HOOK_PAYLOAD_MAX_BYTES}-byte limit. ` +
                'Refusing to truncate per-meeting config (it contains auth tokens). ' +
                `Keys: ${Object.keys(clean).join(',')}`,
        );
    }
    return payload;
}

/**
 * Parse a `/run` hook body back into per-meeting config.
 *
 * Tolerant by design: a malformed or absent payload yields an empty config
 * rather than throwing, so the container still boots and reports status (and
 * the operator sees the VP fail for a legible reason) instead of crash-looping
 * before any status can be published.
 */
export function parseRunHookPayload(body: RunHookBody | string | undefined): PerMeetingConfig {
    if (body === undefined || body === null) return {};

    let parsedBody: RunHookBody;
    if (typeof body === 'string') {
        if (body.trim() === '') return {};
        try {
            parsedBody = JSON.parse(body) as RunHookBody;
        } catch {
            return {};
        }
    } else {
        parsedBody = body;
    }

    const raw = parsedBody.runHookPayload;
    if (!raw || typeof raw !== 'string' || raw.trim() === '') return {};

    let inner: unknown;
    try {
        inner = JSON.parse(raw);
    } catch {
        return {};
    }
    if (typeof inner !== 'object' || inner === null || Array.isArray(inner)) return {};

    const record = inner as Record<string, unknown>;
    const out: PerMeetingConfig = {};
    for (const [key, value] of Object.entries(record)) {
        if (!isAllowedConfigKey(key)) continue;
        if (typeof value === 'string' && value !== '') {
            out[key as PerMeetingKey] = value;
        } else if (typeof value === 'number' || typeof value === 'boolean') {
            // MEETING_TIME / ENABLE_VIDEO_RECORDING may arrive non-stringified.
            out[key as PerMeetingKey] = String(value);
        }
    }
    return out;
}

/**
 * Apply per-meeting config to an environment map.
 *
 * `details.ts` builds its singleton from `process.env` at module-import time, so
 * applying config to the environment BEFORE that import lets every existing
 * consumer keep reading `process.env` unchanged. That is why the MicroVM
 * entrypoint is a supervisor that waits for `/run` and only then starts the app
 * process, rather than a refactor of every config read site.
 *
 * Existing values are not overwritten by default: anything explicitly baked
 * into the image (or set by a test harness) wins over the hook payload, which
 * keeps local/dev overrides predictable.
 */
export function applyPerMeetingConfig(
    env: Record<string, string | undefined>,
    config: PerMeetingConfig,
    options: { overwrite?: boolean } = {},
): string[] {
    const overwrite = options.overwrite === true;
    const applied: string[] = [];
    for (const [key, value] of Object.entries(config) as [PerMeetingKey, string][]) {
        if (value === undefined) continue;
        const existing = env[key];
        if (!overwrite && existing !== undefined && existing !== '') continue;
        env[key] = value;
        applied.push(key);
    }
    return applied;
}

/**
 * Build the browser-facing noVNC WebSocket URL for a MicroVM.
 *
 * Under ECS the UI connects through CloudFront to an ALB
 * (`wss://<cloudfront>/vnc/<vpId>`). Under MicroVMs each VM has its own
 * endpoint and there is no path-based routing, so the vpId is not part of the
 * URL -- isolation comes from the endpoint plus a port-scoped auth token that
 * the UI supplies as a WebSocket subprotocol.
 *
 * `endpoint` is the value RunMicrovm returns (a bare hostname, no scheme).
 * Returns '' when absent so callers can treat it like the existing
 * missing-CLOUDFRONT_DOMAIN case rather than emitting a malformed URL.
 */
export function microvmVncEndpoint(endpoint: string | undefined): string {
    const host = (endpoint || '').trim().replace(/^wss?:\/\//i, '').replace(/\/+$/, '');
    if (host === '') return '';
    return `wss://${host}`;
}

/**
 * Redact secret-bearing values for logging. The per-meeting config carries
 * three Cognito JWTs and a meeting password; none should ever reach CloudWatch.
 */
export function redactPerMeetingConfig(config: PerMeetingConfig): Record<string, string> {
    const SECRET_KEYS = new Set<string>([
        'USER_ACCESS_TOKEN',
        'USER_ID_TOKEN',
        'USER_REFRESH_TOKEN',
        'MEETING_PASSWORD',
    ]);
    const out: Record<string, string> = {};
    for (const [key, value] of Object.entries(config)) {
        if (value === undefined) continue;
        out[key] = SECRET_KEYS.has(key) ? `<redacted:${String(value).length}>` : String(value);
    }
    return out;
}
