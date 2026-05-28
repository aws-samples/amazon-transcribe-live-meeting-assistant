/* eslint-disable @typescript-eslint/no-explicit-any */
import { promises as fs, createReadStream, createWriteStream } from 'fs';
import { spawn } from 'child_process';
import * as path from 'path';
import {
    S3Client,
    GetObjectCommand,
    PutObjectCommand,
    DeleteObjectCommand,
    DeleteObjectsCommand,
    HeadObjectCommand,
    ListObjectsV2Command,
} from '@aws-sdk/client-s3';
import { Readable } from 'stream';

/**
 * Per-user, per-platform CloakBrowser userDataDir backed by S3 as a single
 * tar.gz blob. Replaces an older per-file sync strategy that intermittently
 * corrupted profiles by reading half-flushed Chromium files mid-walk.
 *
 * Lifecycle:
 *   1. acquireProfile() — try to grab a write lock, ALWAYS download+extract
 *      the tar (even if the lock is held by another VP). The corruption
 *      risk is in the WRITE path — concurrent reads of an immutable tar are
 *      safe. If the lock is held, the handle is marked read-only so
 *      persistProfile() will skip the upload at meeting end.
 *   2. persistProfile() — at meeting end, tar+gzip the userDataDir back to
 *      S3. Skipped on read-only handles.
 *   3. releaseProfile() — delete the S3 lock object (if held) and clean up.
 *
 * S3 layout:
 *   s3://${VP_PROFILES_BUCKET}/profiles/{sub}/{platform}/profile.tar.gz
 *   s3://${VP_PROFILES_BUCKET}/profiles/{sub}/{platform}/lock.json   (writers)
 *
 * Local layout:
 *   /srv/cloakbrowser-profiles/{sub-hash-8}/{platform}/    (the userDataDir)
 *
 * Concurrency model: an S3 conditional write (If-None-Match) on lock.json,
 * with a 10-min TTL so a crashed task doesn't hold the lock forever.
 * Concurrent READERS coexist; concurrent WRITERS don't.
 */

const REGION = process.env.AWS_REGION || 'us-east-1';
const PROFILES_BUCKET = (process.env.VP_PROFILES_BUCKET || '').trim();
const LOCK_TTL_MS = 10 * 60 * 1000;

// Default profile root. Override with VP_PROFILE_ROOT for local testing.
const PROFILE_ROOT = process.env.VP_PROFILE_ROOT || '/srv/cloakbrowser-profiles';

let s3Client: S3Client | null = null;
const getS3 = (): S3Client => {
    if (!s3Client) s3Client = new S3Client({ region: REGION });
    return s3Client;
};

const SAFE_PLATFORM = (p: string): string => p.replace(/[^A-Za-z0-9_-]/g, '_').toLowerCase();
const SAFE_SUB = (s: string): string => s.replace(/[^A-Za-z0-9-]/g, '');

interface ProfileHandle {
    enabled: boolean;
    /**
     * Local userDataDir Chromium will be pointed at. Always set when
     * enabled (even if S3 had no tar — we'll create an empty dir). Always
     * the same path for a given (sub, platform) pair so an EC2 host with a
     * persistent volume reuses it across container restarts without an S3
     * round-trip.
     */
    localDir: string;
    s3TarKey: string;
    s3LockKey: string;
    /** True iff we hold the write lock (we'll upload at meeting end). */
    locked: boolean;
    /**
     * True iff we ran with a read-only profile because another VP held the
     * write lock when we started. Surfaced for logging only.
     */
    readOnly: boolean;
}

const TAR_NAME = 'profile.tar.gz';

// Tar contents we don't bother shipping. Cache dirs are large, ephemeral,
// and irrelevant for session restoration. Match the previous-implementation
// SKIP_DIR_PATTERNS to keep tarballs small (~30-50 MB compressed).
const TAR_EXCLUDES = [
    'Default/Cache',
    'Default/Code Cache',
    'Default/GPUCache',
    'Default/ShaderCache',
    'Default/GrShaderCache',
    'Default/Service Worker/CacheStorage',
    'Default/component_crx_cache',
    'Default/Crashpad',
    'Default/CertificateRevocation',
    // optimization_guide_* — match by prefix via tar's --exclude glob
    'Default/optimization_guide_*',
    'Crashpad',
];

export async function acquireProfile(opts: {
    cognitoSub: string;
    platform: string;
}): Promise<ProfileHandle> {
    const cognitoSub = SAFE_SUB(opts.cognitoSub);
    const platform = SAFE_PLATFORM(opts.platform);
    const handle: ProfileHandle = {
        enabled: false,
        localDir: '',
        s3TarKey: '',
        s3LockKey: '',
        locked: false,
        readOnly: false,
    };
    if (!PROFILES_BUCKET || !cognitoSub) {
        console.log(
            `[profile-store] Persistent profile DISABLED (PROFILES_BUCKET=${
                PROFILES_BUCKET ? 'set' : 'EMPTY'
            }, cognitoSub=${cognitoSub ? 'set' : 'EMPTY'}); using fresh profile.`,
        );
        return handle;
    }

    handle.enabled = true;
    handle.s3TarKey = `profiles/${cognitoSub}/${platform}/${TAR_NAME}`;
    handle.s3LockKey = `profiles/${cognitoSub}/${platform}/lock.json`;
    // Stable per-(user, platform) local path so EC2 hosts can reuse the
    // extracted profile across container restarts.
    const subShort = cognitoSub.slice(0, 8) || 'unknown';
    handle.localDir = path.join(PROFILE_ROOT, subShort, platform);
    await fs.mkdir(handle.localDir, { recursive: true });

    activeHandle = handle;

    // Try the write lock first. Outcome determines whether we upload at end.
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
                IfNoneMatch: '*',
                ContentType: 'application/json',
                ServerSideEncryption: 'aws:kms',
            }),
        );
        handle.locked = true;
        console.log(`[profile-store] Acquired write lock: s3://${PROFILES_BUCKET}/${handle.s3LockKey}`);
    } catch (err: any) {
        const code = err?.name || err?.Code || '';
        if (code === 'PreconditionFailed' || code === 'PreconditionFailedException') {
            const expired = await isLockExpired(handle.s3LockKey);
            if (expired) {
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
                    console.log('[profile-store] Took over expired write lock');
                } catch (innerErr) {
                    console.warn('[profile-store] Could not steal expired lock; running read-only:', innerErr);
                    handle.readOnly = true;
                }
            } else {
                handle.readOnly = true;
                console.warn(
                    '[profile-store] Another VP holds the write lock — running with read-only profile (this session\'s state will not be persisted)',
                );
            }
        } else {
            console.warn('[profile-store] Lock acquisition error; running read-only:', err);
            handle.readOnly = true;
        }
    }

    // Always download. Read sharing is safe — concurrent readers of an
    // immutable tar can't corrupt anything, and the second VP needs the
    // latest persisted state to join the meeting reliably.
    try {
        const restored = await downloadAndExtract(handle.s3TarKey, handle.localDir);
        if (restored) {
            console.log(`[profile-store] Restored profile from s3://${PROFILES_BUCKET}/${handle.s3TarKey} → ${handle.localDir}`);
        } else {
            console.log(`[profile-store] No tar in S3 yet — running with fresh profile at ${handle.localDir}`);
        }
    } catch (err) {
        console.warn('[profile-store] Could not restore profile (continuing with whatever is on disk):', err);
    }

    return handle;
}

let activeHandle: ProfileHandle | null = null;

/**
 * Tar+gzip the userDataDir and upload to S3, replacing whatever was there
 * before. No-op for read-only handles (another VP holds the write lock) and
 * for disabled handles (no PROFILES_BUCKET configured).
 *
 * Must be called AFTER browser.close() so SQLite/IndexedDB are flushed.
 */
export async function persistProfile(handle: ProfileHandle): Promise<void> {
    if (!handle.enabled) return;
    if (!handle.locked) {
        console.log(
            handle.readOnly
                ? '[profile-store] Skipping upload — handle is read-only'
                : '[profile-store] Skipping upload — write lock not held',
        );
        return;
    }

    try {
        await fs.access(handle.localDir);
    } catch {
        console.warn(`[profile-store] Skipping upload — local dir missing (${handle.localDir})`);
        return;
    }

    const tmpTar = path.join('/tmp', `vp-profile-save-${process.pid}.tar.gz`);
    try {
        const tarStart = Date.now();
        const excludeArgs = TAR_EXCLUDES.flatMap((p) => ['--exclude', p]);
        await runShell('tar', [
            ...excludeArgs,
            '-czf',
            tmpTar,
            '-C',
            handle.localDir,
            '.',
        ]);
        const stat = await fs.stat(tmpTar);
        console.log(
            `[profile-store] Tarred profile: ${(stat.size / (1024 * 1024)).toFixed(1)} MB in ${Date.now() - tarStart}ms`,
        );

        const uploadStart = Date.now();
        await getS3().send(
            new PutObjectCommand({
                Bucket: PROFILES_BUCKET,
                Key: handle.s3TarKey,
                Body: createReadStream(tmpTar),
                ContentType: 'application/gzip',
                ServerSideEncryption: 'aws:kms',
            }),
        );
        console.log(
            `[profile-store] Uploaded profile to s3://${PROFILES_BUCKET}/${handle.s3TarKey} in ${Date.now() - uploadStart}ms`,
        );
    } catch (err) {
        console.warn('[profile-store] persistProfile failed (non-fatal):', err);
    } finally {
        await fs.unlink(tmpTar).catch(() => undefined);
    }
}

/**
 * Release the S3 write lock (if held). Local files are left in place so the
 * next launch on this host (EC2 with persistent volume) can reuse them
 * without a S3 round-trip. On Fargate the local files vanish with the task.
 */
export async function releaseProfile(handle: ProfileHandle): Promise<void> {
    if (activeHandle === handle) activeHandle = null;
    if (handle.locked) {
        try {
            await getS3().send(
                new DeleteObjectCommand({
                    Bucket: PROFILES_BUCKET,
                    Key: handle.s3LockKey,
                }),
            );
            console.log('[profile-store] Released write lock');
        } catch (err) {
            console.warn('[profile-store] Could not delete lock object (non-critical):', err);
        }
    }
}

/**
 * Used by the deleteMyZoomCredentials path (or admin tooling) to wipe a
 * user's persisted profiles when they remove their account from LMA.
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
    console.log(`[profile-store] Deleted ${total} S3 objects under ${prefix}`);
}

// -----------------------------------------------------------------------------
// internals
// -----------------------------------------------------------------------------

/**
 * @returns true if a tar was downloaded and extracted, false if there was
 * no tar in S3 yet (HEAD 404).
 */
async function downloadAndExtract(s3Key: string, localDir: string): Promise<boolean> {
    // HEAD first so a missing tar doesn't log an error from GetObject.
    try {
        await getS3().send(new HeadObjectCommand({ Bucket: PROFILES_BUCKET, Key: s3Key }));
    } catch (err: any) {
        if (
            err?.name === 'NotFound' ||
            err?.name === 'NoSuchKey' ||
            err?.$metadata?.httpStatusCode === 404
        ) {
            return false;
        }
        throw err;
    }

    // Wipe local dir to mirror S3 exactly. Without this, partial state from
    // a previous tar can mix with newly-extracted files and produce a
    // Frankenstein profile that Chromium reads but Zoom doesn't trust.
    await wipeDirContents(localDir);

    const tmpTar = path.join('/tmp', `vp-profile-load-${process.pid}.tar.gz`);
    const downloadStart = Date.now();
    try {
        const obj = await getS3().send(new GetObjectCommand({ Bucket: PROFILES_BUCKET, Key: s3Key }));
        if (!obj.Body) throw new Error('S3 GetObject returned empty body');
        const writeStream = createWriteStream(tmpTar);
        await new Promise<void>((resolve, reject) => {
            (obj.Body as Readable).pipe(writeStream).on('finish', () => resolve()).on('error', reject);
        });
        const stat = await fs.stat(tmpTar);
        console.log(
            `[profile-store] Downloaded profile: ${(stat.size / (1024 * 1024)).toFixed(1)} MB in ${Date.now() - downloadStart}ms`,
        );

        const extractStart = Date.now();
        await runShell('tar', ['-xzf', tmpTar, '-C', localDir]);
        console.log(`[profile-store] Extracted profile in ${Date.now() - extractStart}ms`);
        return true;
    } finally {
        await fs.unlink(tmpTar).catch(() => undefined);
    }
}

async function wipeDirContents(dir: string): Promise<void> {
    let entries: any[];
    try {
        entries = await fs.readdir(dir, { withFileTypes: true });
    } catch {
        return;
    }
    await Promise.all(
        entries.map((e) =>
            fs.rm(path.join(dir, e.name), { recursive: true, force: true }).catch(() => undefined),
        ),
    );
}

async function isLockExpired(key: string): Promise<boolean> {
    try {
        const obj = await getS3().send(new GetObjectCommand({ Bucket: PROFILES_BUCKET, Key: key }));
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

function runShell(cmd: string, args: string[]): Promise<void> {
    return new Promise((resolve, reject) => {
        const child = spawn(cmd, args, { stdio: ['ignore', 'inherit', 'inherit'] });
        child.on('error', reject);
        child.on('exit', (code) => {
            if (code === 0) resolve();
            else reject(new Error(`${cmd} exited ${code}`));
        });
    });
}
