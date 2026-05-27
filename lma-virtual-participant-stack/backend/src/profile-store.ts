/* eslint-disable @typescript-eslint/no-explicit-any */
import { promises as fs } from 'fs';
import * as path from 'path';
import * as os from 'os';
import {
    S3Client,
    ListObjectsV2Command,
    GetObjectCommand,
    PutObjectCommand,
    DeleteObjectsCommand,
    HeadObjectCommand,
} from '@aws-sdk/client-s3';
import { Readable } from 'stream';

/**
 * Per-user, per-platform Chromium userDataDir backed by S3.
 *
 * - On launch we download `s3://${bucket}/profiles/{sub}/{platform}/` into a
 *   local scratch directory and pass it to Puppeteer as userDataDir. After
 *   the first sign-in (with reCAPTCHA / 2FA solved manually via VNC) the
 *   user's session cookies persist; subsequent meetings reuse them and skip
 *   the bot-detection dialog entirely.
 * - On meeting end / shutdown we upload the local profile back to S3.
 *
 * Concurrency: a single profile cannot be opened by two tasks at once. We
 * use an S3 conditional write (If-None-Match) on `lock.json` as the lock,
 * with a 10-minute expiry so a crashed task doesn't leave the profile
 * permanently locked.
 *
 * Fargate doesn't grant FUSE privileges so we don't use Mountpoint for S3 —
 * sync-on-start / sync-on-end gives us the same observable behaviour.
 */

const REGION = process.env.AWS_REGION || 'us-east-1';
const PROFILES_BUCKET = (process.env.VP_PROFILES_BUCKET || '').trim();
const LOCK_TTL_MS = 10 * 60 * 1000;

let s3Client: S3Client | null = null;
const getS3 = (): S3Client => {
    if (!s3Client) s3Client = new S3Client({ region: REGION });
    return s3Client;
};

const SAFE_PLATFORM = (p: string): string => p.replace(/[^A-Za-z0-9_-]/g, '_').toLowerCase();
const SAFE_SUB = (s: string): string => s.replace(/[^A-Za-z0-9-]/g, '');

interface ProfileHandle {
    enabled: boolean;
    localDir: string;
    s3Prefix: string;
    s3LockKey: string;
    locked: boolean;
}

// Module-level reference to the currently-acquired handle. Exposed so platform
// handlers (zoom.ts, etc.) can trigger an interim persist after sign-in
// without having to plumb the handle through every call site. There is at
// most one active VP per container so this is safe.
let activeHandle: ProfileHandle | null = null;
let lastInterimPersistMs = 0;

export async function acquireProfile(opts: {
    cognitoSub: string;
    platform: string;
}): Promise<ProfileHandle> {
    const cognitoSub = SAFE_SUB(opts.cognitoSub);
    const platform = SAFE_PLATFORM(opts.platform);
    const handle: ProfileHandle = {
        enabled: false,
        localDir: '',
        s3Prefix: '',
        s3LockKey: '',
        locked: false,
    };
    if (!PROFILES_BUCKET || !cognitoSub) {
        console.log(
            `[profile-store] Persistent profile DISABLED (PROFILES_BUCKET=${
                PROFILES_BUCKET ? 'set' : 'EMPTY'
            }, cognitoSub=${cognitoSub ? 'set' : 'EMPTY'}); using fresh profile per launch.`,
        );
        return handle;
    }

    handle.enabled = true;
    activeHandle = handle;
    handle.s3Prefix = `profiles/${cognitoSub}/${platform}/`;
    handle.s3LockKey = `profiles/${cognitoSub}/${platform}/lock.json`;
    handle.localDir = await fs.mkdtemp(path.join(os.tmpdir(), `vp-profile-${cognitoSub.slice(0, 8)}-${platform}-`));

    // Try to acquire the S3 lock (conditional write).
    try {
        const lockBody = JSON.stringify({
            taskArn: process.env.ECS_CONTAINER_METADATA_URI_V4 || 'unknown',
            pid: process.pid,
            acquiredAt: new Date().toISOString(),
            expiresAt: new Date(Date.now() + LOCK_TTL_MS).toISOString(),
        });
        await getS3().send(
            new PutObjectCommand({
                Bucket: PROFILES_BUCKET,
                Key: handle.s3LockKey,
                Body: lockBody,
                IfNoneMatch: '*', // Fail if the object already exists
                ContentType: 'application/json',
                ServerSideEncryption: 'aws:kms',
            }),
        );
        handle.locked = true;
        console.log(`[profile-store] Acquired S3 lock at s3://${PROFILES_BUCKET}/${handle.s3LockKey}`);
    } catch (err: any) {
        const code = err?.name || err?.Code || '';
        if (code === 'PreconditionFailed' || code === 'PreconditionFailedException') {
            // Lock object exists. Check if it's expired.
            const expired = await isLockExpired(handle.s3LockKey);
            if (!expired) {
                handle.enabled = false;
                console.warn(
                    `[profile-store] Profile is locked by another LMA session (lock at s3://${PROFILES_BUCKET}/${handle.s3LockKey}); falling back to a fresh profile`,
                );
                return handle;
            }
            // Expired — overwrite (no IfNoneMatch this time)
            try {
                await getS3().send(
                    new PutObjectCommand({
                        Bucket: PROFILES_BUCKET,
                        Key: handle.s3LockKey,
                        Body: JSON.stringify({
                            takenoverFromExpired: true,
                            pid: process.pid,
                            acquiredAt: new Date().toISOString(),
                            expiresAt: new Date(Date.now() + LOCK_TTL_MS).toISOString(),
                        }),
                        ContentType: 'application/json',
                        ServerSideEncryption: 'aws:kms',
                    }),
                );
                handle.locked = true;
                console.log('[profile-store] Took over expired lock');
            } catch (innerErr) {
                console.warn('[profile-store] Could not steal expired lock; falling back to fresh profile:', innerErr);
                handle.enabled = false;
                return handle;
            }
        } else {
            console.warn('[profile-store] Lock acquisition error; falling back to fresh profile:', err);
            handle.enabled = false;
            return handle;
        }
    }

    // Wipe the local profile dir before hydrating from S3. Without this, a
    // partial download can leave a Frankenstein profile mixing leftover
    // local files with S3-extracted state — which Chromium can read but
    // Zoom doesn't recognize as authenticated. (MR 154 hit the same issue
    // and added a wipeProfileDir() before extracting their tarball; we do
    // the same for our file-by-file downloader.)
    try {
        await wipeDirContents(handle.localDir);
    } catch (err) {
        console.warn('[profile-store] Could not wipe profile dir before hydrate:', err);
    }

    // Pull whatever is already in the user's profile down into localDir.
    try {
        await downloadPrefix(handle.s3Prefix, handle.localDir);
        console.log(`[profile-store] Hydrated profile from s3://${PROFILES_BUCKET}/${handle.s3Prefix} → ${handle.localDir}`);
    } catch (err) {
        console.warn('[profile-store] Could not hydrate profile from S3 (continuing with empty dir):', err);
    }
    return handle;
}

async function wipeDirContents(dir: string): Promise<void> {
    let entries: any[];
    try {
        entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
        return; // dir may not exist yet
    }
    await Promise.all(
        entries.map((e) =>
            fs.rm(path.join(dir, e.name), { recursive: true, force: true }).catch(() => {}),
        ),
    );
}

async function isLockExpired(key: string): Promise<boolean> {
    try {
        const obj = await getS3().send(
            new GetObjectCommand({ Bucket: PROFILES_BUCKET, Key: key }),
        );
        const body = await streamToString(obj.Body as Readable);
        const parsed = JSON.parse(body);
        const expiresAt = Date.parse(parsed.expiresAt || '');
        if (!expiresAt) return true;
        return expiresAt < Date.now();
    } catch {
        return true;
    }
}

async function streamToString(stream: Readable): Promise<string> {
    const chunks: Buffer[] = [];
    return new Promise((resolve, reject) => {
        stream.on('data', (c) => chunks.push(Buffer.from(c)));
        stream.on('error', reject);
        stream.on('end', () => resolve(Buffer.concat(chunks).toString('utf-8')));
    });
}

async function downloadPrefix(prefix: string, localDir: string): Promise<void> {
    let token: string | undefined;
    do {
        const list = await getS3().send(
            new ListObjectsV2Command({
                Bucket: PROFILES_BUCKET,
                Prefix: prefix,
                ContinuationToken: token,
            }),
        );
        for (const obj of list.Contents || []) {
            if (!obj.Key) continue;
            // Skip the lock file
            if (obj.Key.endsWith('/lock.json') || obj.Key.endsWith('lock.json')) continue;
            const rel = obj.Key.slice(prefix.length);
            if (!rel) continue;
            const local = path.join(localDir, rel);
            await fs.mkdir(path.dirname(local), { recursive: true });
            const got = await getS3().send(
                new GetObjectCommand({ Bucket: PROFILES_BUCKET, Key: obj.Key }),
            );
            const buf = await streamToBuffer(got.Body as Readable);
            await fs.writeFile(local, buf);
        }
        token = list.NextContinuationToken;
    } while (token);
}

async function streamToBuffer(stream: Readable): Promise<Buffer> {
    const chunks: Buffer[] = [];
    return new Promise((resolve, reject) => {
        stream.on('data', (c) => chunks.push(Buffer.from(c)));
        stream.on('error', reject);
        stream.on('end', () => resolve(Buffer.concat(chunks)));
    });
}

async function* walk(dir: string): AsyncGenerator<string> {
    let entries;
    try {
        entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
        return;
    }
    for (const entry of entries) {
        const p = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            yield* walk(p);
        } else if (entry.isFile()) {
            yield p;
        }
    }
}

/**
 * Sync the local profile directory back up to S3. We do a simple full upload
 * — Chromium profile sizes are typically <100MB so this completes well
 * within the meeting-end window.
 */
/**
 * Persist the active profile mid-session — call this after sign-in success,
 * so the trusted-device cookie is captured even if the meeting later crashes
 * or the VP exits abnormally. Throttled to one call per minute so platform
 * code can fire it on multiple "good moments" (sign-in success, joined
 * meeting, …) without redundant uploads. The full meeting-end persist still
 * runs at shutdown.
 */
export async function persistProfileInterim(reason: string): Promise<void> {
    if (!activeHandle || !activeHandle.enabled || !activeHandle.locked) return;
    const now = Date.now();
    if (now - lastInterimPersistMs < 60_000) {
        console.log(`[profile-store] Interim persist skipped (throttled, reason=${reason})`);
        return;
    }
    lastInterimPersistMs = now;
    console.log(`[profile-store] Interim persist starting (reason=${reason})`);
    await persistProfile(activeHandle);
}

// Subdirectories not worth uploading: large, ephemeral, or unrelated to the
// session-restore properties we care about (auth cookies, "trusted device"
// markers in IndexedDB / localStorage). Cache especially is a moving target —
// Chromium constantly creates and deletes files there, racing the walker and
// causing ENOENT mid-upload, and reading a half-flushed cache entry adds zero
// value for session restoration.
const SKIP_DIR_PATTERNS = [
    /\/Cache\//,
    /\/Code Cache\//,
    /\/GPUCache\//,
    /\/ShaderCache\//,
    /\/GrShaderCache\//,
    /\/Service Worker\/CacheStorage\//,
    /\/component_crx_cache\//,
    /\/optimization_guide_/,
    /\/Crashpad\//,
    /\/CertificateRevocation\//,
];

export async function persistProfile(handle: ProfileHandle): Promise<void> {
    if (!handle.enabled || !handle.locked) return;
    let uploaded = 0;
    let skipped = 0;
    let failed = 0;
    for await (const local of walk(handle.localDir)) {
        const rel = path.relative(handle.localDir, local);
        // Skip lock files inside the profile directory (just in case)
        if (rel.endsWith('lock.json')) continue;
        // Skip cache / volatile dirs that Chromium rewrites constantly and
        // that aren't load-bearing for session restoration.
        if (SKIP_DIR_PATTERNS.some((re) => re.test(`/${rel}`))) {
            skipped += 1;
            continue;
        }
        try {
            const stat = await fs.stat(local);
            if (stat.size > 100 * 1024 * 1024) {
                skipped += 1;
                continue;
            }
            const body = await fs.readFile(local);
            await getS3().send(
                new PutObjectCommand({
                    Bucket: PROFILES_BUCKET,
                    Key: `${handle.s3Prefix}${rel.replace(/\\/g, '/')}`,
                    Body: body,
                    ServerSideEncryption: 'aws:kms',
                }),
            );
            uploaded += 1;
        } catch (err: any) {
            // Chromium may have deleted the file between the walker listing
            // it and us reading it (ENOENT). It can also be temporarily
            // unreadable (EBUSY) during a write. Either case: skip this file
            // and keep going so a transient race on one file doesn't abort
            // the whole sync of cookies / IndexedDB / localStorage.
            failed += 1;
            const code = err?.code || '';
            if (code !== 'ENOENT' && code !== 'EBUSY') {
                console.warn(`[profile-store] Skipping ${rel} (${code || err?.message || err})`);
            }
        }
    }
    console.log(`[profile-store] Persisted ${uploaded} files to s3://${PROFILES_BUCKET}/${handle.s3Prefix} (skipped ${skipped}, transient errors ${failed})`);
}

/**
 * Release the S3 lock and clean up the local scratch dir. Always safe to
 * call even if the profile was never acquired.
 */
export async function releaseProfile(handle: ProfileHandle): Promise<void> {
    if (activeHandle === handle) activeHandle = null;
    if (handle.locked) {
        try {
            await getS3().send(
                new DeleteObjectsCommand({
                    Bucket: PROFILES_BUCKET,
                    Delete: { Objects: [{ Key: handle.s3LockKey }] },
                }),
            );
            console.log('[profile-store] Released S3 lock');
        } catch (err) {
            console.warn('[profile-store] Could not delete lock object (non-critical):', err);
        }
    }
    if (handle.localDir) {
        try {
            await fs.rm(handle.localDir, { recursive: true, force: true });
        } catch (err) {
            console.warn('[profile-store] Could not delete local profile dir:', err);
        }
    }
}

/**
 * Used by the deleteMyZoomCredentials path (or admin tooling) to wipe a user's
 * persisted profiles when they remove their Zoom account from LMA.
 */
export async function deleteUserProfiles(cognitoSub: string): Promise<void> {
    if (!PROFILES_BUCKET) return;
    const sub = SAFE_SUB(cognitoSub);
    const prefix = `profiles/${sub}/`;
    let token: string | undefined;
    let total = 0;
    do {
        const list = await getS3().send(
            new ListObjectsV2Command({
                Bucket: PROFILES_BUCKET,
                Prefix: prefix,
                ContinuationToken: token,
            }),
        );
        const objects = (list.Contents || []).map((o) => ({ Key: o.Key as string }));
        if (objects.length) {
            await getS3().send(
                new DeleteObjectsCommand({
                    Bucket: PROFILES_BUCKET,
                    Delete: { Objects: objects },
                }),
            );
            total += objects.length;
        }
        token = list.NextContinuationToken;
    } while (token);
    console.log(`[profile-store] Deleted ${total} objects under ${prefix}`);
}

// Suppress lint warnings about unused HeadObjectCommand if we don't end up using it
void HeadObjectCommand;
