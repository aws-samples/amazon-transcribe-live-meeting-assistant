/* eslint-disable @typescript-eslint/no-explicit-any */
/**
 * Per-user persistent CloakBrowser userDataDir backed by S3 as a single
 * tar.gz blob. One profile per user — all meeting platforms share the same
 * profile dir so cross-platform cookies (e.g. an SSO sign-in to office.com
 * carries to teams.microsoft.com) work without separate warmups.
 *
 * Lifecycle:
 *   1. acquireProfile() — download+extract the tar into the local userDataDir.
 *      First run: no tar in S3 yet; returns an empty dir.
 *   2. persistProfile() — at meeting end, tar+gzip the userDataDir back to S3.
 *      Last-write-wins. We accept this because (a) one user rarely runs two
 *      VPs at once, (b) the tar is small enough that even a clobbered upload
 *      just costs the loser their session cookies — Zoom will challenge them
 *      on the next launch and re-establish state.
 *   3. releaseProfile() — kept for symmetry; currently a no-op.
 *
 * S3 layout:
 *   s3://${VP_PROFILES_BUCKET}/profiles/{sha256(sub)}/profile.tar.gz
 */

import { spawn } from 'child_process';
import { createHash } from 'crypto';
import { promises as fs, createReadStream, createWriteStream } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import {
    S3Client,
    GetObjectCommand,
    PutObjectCommand,
    HeadObjectCommand,
} from '@aws-sdk/client-s3';
import { Readable } from 'stream';

const REGION = process.env.AWS_REGION || 'us-east-1';
const PROFILES_BUCKET = (process.env.VP_PROFILES_BUCKET || '').trim();
const PROFILE_ROOT = process.env.VP_PROFILE_ROOT || '/srv/cloakbrowser-profiles';
const TAR_NAME = 'profile.tar.gz';
// Must match VPProfilesPolicy in template.yaml — the task IAM role only
// allows reads/writes under profiles/*.
const S3_PREFIX = 'profiles/';

let s3Client: S3Client | null = null;
const getS3 = (): S3Client => {
    if (!s3Client) s3Client = new S3Client({ region: REGION });
    return s3Client;
};

export interface ProfileHandle {
    enabled: boolean;
    /** Local userDataDir Chromium will be pointed at. */
    localDir: string;
    /** S3 key for the tar. Empty when disabled. */
    s3Key: string;
}

export async function acquireProfile(opts: { cognitoSub: string }): Promise<ProfileHandle> {
    const handle: ProfileHandle = { enabled: false, localDir: '', s3Key: '' };
    const sub = (opts.cognitoSub || '').trim();
    if (!PROFILES_BUCKET || !sub) {
        console.log(
            `[profile-store] DISABLED (bucket=${PROFILES_BUCKET ? 'set' : 'EMPTY'}, ` +
                `sub=${sub ? 'set' : 'EMPTY'}); using fresh ephemeral profile.`,
        );
        return handle;
    }

    const userHash = createHash('sha256').update(sub.toLowerCase()).digest('hex');
    handle.enabled = true;
    handle.s3Key = `${S3_PREFIX}${userHash}/${TAR_NAME}`;
    handle.localDir = join(PROFILE_ROOT, userHash.slice(0, 16));
    await fs.mkdir(handle.localDir, { recursive: true });

    console.log(`[profile-store] user hash (sha256) : ${userHash.slice(0, 16)}...`);
    console.log(`[profile-store] localDir           : ${handle.localDir}`);
    console.log(`[profile-store] s3Key              : s3://${PROFILES_BUCKET}/${handle.s3Key}`);

    try {
        await downloadAndExtract(handle.s3Key, handle.localDir);
    } catch (err) {
        console.warn('[profile-store] Restore failed (continuing with fresh profile):', err);
    }

    return handle;
}

/**
 * Tar+gzip the userDataDir and upload to S3, replacing whatever was there.
 * No-op when disabled. Must be called AFTER browser.close() so SQLite/IndexedDB
 * are flushed.
 */
export async function persistProfile(handle: ProfileHandle): Promise<void> {
    if (!handle.enabled) return;
    try {
        await fs.access(handle.localDir);
    } catch {
        console.warn(`[profile-store] Skipping upload — local dir missing (${handle.localDir})`);
        return;
    }

    const tmpTar = join(tmpdir(), `vp-profile-save-${process.pid}.tar.gz`);
    try {
        const tarStart = Date.now();
        await runShell('tar', ['-czf', tmpTar, '-C', handle.localDir, '.']);
        const stat = await fs.stat(tmpTar);
        console.log(
            `[profile-store] Tarred profile: ${(stat.size / (1024 * 1024)).toFixed(1)} MB in ${Date.now() - tarStart}ms`,
        );

        const uploadStart = Date.now();
        await getS3().send(
            new PutObjectCommand({
                Bucket: PROFILES_BUCKET,
                Key: handle.s3Key,
                Body: createReadStream(tmpTar),
                ContentType: 'application/gzip',
                ServerSideEncryption: 'aws:kms',
            }),
        );
        console.log(
            `[profile-store] Uploaded profile to s3://${PROFILES_BUCKET}/${handle.s3Key} in ${Date.now() - uploadStart}ms`,
        );
    } catch (err) {
        console.warn('[profile-store] persistProfile failed (non-fatal):', err);
    } finally {
        await fs.unlink(tmpTar).catch(() => undefined);
    }
}

export async function releaseProfile(_handle: ProfileHandle): Promise<void> {
    // No lock to release — last-write-wins. Kept for call-site symmetry.
}

// -----------------------------------------------------------------------------
// internals
// -----------------------------------------------------------------------------

async function downloadAndExtract(s3Key: string, localDir: string): Promise<void> {
    // HEAD first so a missing tar doesn't log a noisy error from GetObject.
    try {
        await getS3().send(new HeadObjectCommand({ Bucket: PROFILES_BUCKET, Key: s3Key }));
    } catch (err: any) {
        if (
            err?.name === 'NotFound' ||
            err?.name === 'NoSuchKey' ||
            err?.$metadata?.httpStatusCode === 404
        ) {
            console.log('[profile-store] No tar in S3 yet — fresh profile.');
            await wipeDirContents(localDir);
            return;
        }
        throw err;
    }

    // Wipe local dir so partial state from a previous tar can't mix with the
    // newly-extracted one and produce a Frankenstein profile Chromium reads
    // but Zoom doesn't trust.
    await wipeDirContents(localDir);

    const tmpTar = join(tmpdir(), `vp-profile-load-${process.pid}.tar.gz`);
    const downloadStart = Date.now();
    try {
        const obj = await getS3().send(new GetObjectCommand({ Bucket: PROFILES_BUCKET, Key: s3Key }));
        if (!obj.Body) throw new Error('S3 GetObject returned empty body');
        await new Promise<void>((resolve, reject) => {
            (obj.Body as Readable)
                .pipe(createWriteStream(tmpTar))
                .on('finish', () => resolve())
                .on('error', reject);
        });
        const stat = await fs.stat(tmpTar);
        console.log(
            `[profile-store] Downloaded profile: ${(stat.size / (1024 * 1024)).toFixed(1)} MB in ${Date.now() - downloadStart}ms`,
        );

        const extractStart = Date.now();
        await runShell('tar', ['-xzf', tmpTar, '-C', localDir]);
        console.log(`[profile-store] Extracted profile in ${Date.now() - extractStart}ms`);
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
            fs.rm(join(dir, e.name), { recursive: true, force: true }).catch(() => undefined),
        ),
    );
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
