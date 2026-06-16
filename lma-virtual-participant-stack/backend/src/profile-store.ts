/* eslint-disable @typescript-eslint/no-explicit-any */
// Per-user, per-platform CloakBrowser userDataDir backed by S3 as profile.tar.gz.
// Last-write-wins on concurrent VPs for the same user+platform. Profiles are
// keyed by meeting platform so a Zoom-authenticated session is never reused for
// a Webex/Teams/Chime meeting (and vice versa) — each platform's cookies and
// trusted-device markers live in their own tar.

import { spawn } from 'child_process';
import { createHash } from 'crypto';
import { promises as fs, existsSync, createReadStream, createWriteStream } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { DatabaseSync } from 'node:sqlite';
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
// Must match VPProfilesPolicy in template.yaml.
const S3_PREFIX = 'profiles/';

// Meeting platforms whose web login rides on SESSION cookies (is_persistent=0,
// expires_utc=0). Chromium keeps those only in memory for the life of one
// browser session and does NOT reload them from the on-disk Cookies DB on a
// fresh launch — so a faithfully saved+restored profile still comes up logged
// out. (The old rebrowser-puppeteer launch path happened to carry them over;
// cloakbrowser's Playwright launchPersistentContext does a clean start that
// drops them.) We promote these auth cookies to persistent with a future
// expiry on the restored DB before launch, so the next session is recognized
// as logged in. host_key is matched as a LIKE suffix (e.g. '%zoom.us').
//
// The Microsoft identity hosts (microsoftonline.com / live.com / office.com /
// the broad microsoft.com suffix) carry not just Teams sign-in but also the
// anonymous-join HIP CAPTCHA *trust token*. Microsoft issues that token on its
// identity surface (the anon join redirects through login.microsoftonline.com
// before landing on the teams.microsoft.com light-meetings pre-join), so it
// lands on a different host than teams.microsoft.com. Promoting these lets a
// CAPTCHA the user solved once in VNC survive the profile save/restore, so
// subsequent joins for that user skip the challenge.
const SESSION_AUTH_COOKIE_HOSTS = [
    'zoom.us',
    'zoom.com',
    'chime.aws',
    'webex.com',
    'teams.microsoft.com',
    'microsoft.com',
    'microsoftonline.com',
    'live.com',
    'office.com',
];
// 30 days, expressed as Chrome's Win32 FILETIME (microseconds since 1601-01-01).
const CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000;
const PROMOTE_LIFETIME_US = 30 * 24 * 60 * 60 * 1_000_000;

// Promote meeting-platform session auth cookies to persistent so the login
// survives the next launch. Operates on whichever Cookies DB Chromium uses
// (modern: Default/Network/Cookies, legacy: Default/Cookies). Best-effort:
// any failure is logged and swallowed — a missing/locked DB must never block
// profile restore.
function promoteSessionAuthCookies(localDir: string): void {
    const candidates = [
        join(localDir, 'Default', 'Network', 'Cookies'),
        join(localDir, 'Default', 'Cookies'),
    ];
    const cookiesPath = candidates.find((p) => existsSync(p));
    if (!cookiesPath) {
        console.log('[profile-store] No Cookies DB found — skipping session-auth promotion.');
        return;
    }

    const expiresUtc = String(BigInt(Date.now()) * 1000n + BigInt(CHROME_EPOCH_OFFSET_US) + BigInt(PROMOTE_LIFETIME_US));
    const hostClause = SESSION_AUTH_COOKIE_HOSTS.map(() => 'host_key LIKE ?').join(' OR ');
    const params = SESSION_AUTH_COOKIE_HOSTS.map((h) => `%${h}`);

    let db: DatabaseSync | null = null;
    try {
        db = new DatabaseSync(cookiesPath);
        // Only touch session cookies (is_persistent=0); leave already-persistent
        // cookies untouched so we don't extend tracker/analytics lifetimes.
        const sel = db.prepare(
            `SELECT count(*) AS n FROM cookies WHERE is_persistent = 0 AND (${hostClause})`,
        );
        const before = sel.get(...params) as { n: number };
        if (!before || before.n === 0) {
            console.log('[profile-store] No session auth cookies to promote.');
            return;
        }
        const upd = db.prepare(
            `UPDATE cookies SET is_persistent = 1, expires_utc = ? WHERE is_persistent = 0 AND (${hostClause})`,
        );
        const res = upd.run(expiresUtc, ...params);
        console.log(`[profile-store] Promoted ${res.changes} session auth cookie(s) to persistent (meeting-platform login survival).`);
    } catch (err) {
        console.warn('[profile-store] Session-auth cookie promotion failed (non-fatal):', err);
    } finally {
        try { db?.close(); } catch { /* ignore */ }
    }
}

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

// Normalize a meeting platform label to a stable S3 path segment. Mirrors the
// values the UI / scheduler send ('ZOOM' | 'CHIME' | 'TEAMS' | 'WEBEX', with
// older mixed-case variants) so the key matches between writer and reader.
// Falls back to 'unknown' so a missing/garbage platform still gets an isolated
// (never shared) profile rather than colliding with a real one.
function normalizePlatform(platform: string | undefined): string {
    const p = (platform || '').trim().toLowerCase();
    if (p.startsWith('zoom')) return 'zoom';
    if (p.startsWith('chime')) return 'chime';
    if (p.startsWith('team')) return 'teams';
    if (p.startsWith('webex')) return 'webex';
    if (p.startsWith('google')) return 'googlemeet';
    return p.replace(/[^a-z0-9]+/g, '') || 'unknown';
}

export async function acquireProfile(opts: { cognitoSub: string; platform?: string }): Promise<ProfileHandle> {
    const handle: ProfileHandle = { enabled: false, localDir: '', s3Key: '' };
    const sub = (opts.cognitoSub || '').trim();
    if (!PROFILES_BUCKET || !sub) {
        console.log(
            `[profile-store] DISABLED (bucket=${PROFILES_BUCKET ? 'set' : 'EMPTY'}, ` +
                `sub=${sub ? 'set' : 'EMPTY'}); using fresh ephemeral profile.`,
        );
        return handle;
    }

    const platform = normalizePlatform(opts.platform);
    const userHash = createHash('sha256').update(sub.toLowerCase()).digest('hex');
    handle.enabled = true;
    handle.s3Key = `${S3_PREFIX}${userHash}/${TAR_NAME}`;
    handle.localDir = join(PROFILE_ROOT, userHash.slice(0, 16));
    await fs.mkdir(handle.localDir, { recursive: true });

    console.log(`[profile-store] user hash (sha256) : ${userHash.slice(0, 16)}...`);
    console.log(`[profile-store] platform           : ${platform}`);
    console.log(`[profile-store] localDir           : ${handle.localDir}`);
    console.log(`[profile-store] s3Key              : s3://${PROFILES_BUCKET}/${handle.s3Key}`);

    try {
        await downloadAndExtract(handle.s3Key, handle.localDir);
        // Promote meeting-platform session auth cookies to persistent so the
        // restored login is actually loaded by Chromium on this launch.
        promoteSessionAuthCookies(handle.localDir);
    } catch (err) {
        console.warn('[profile-store] Restore failed (continuing with fresh profile):', err);
    }

    return handle;
}

// Call AFTER browser.close() so SQLite/IndexedDB are flushed.
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

// Kept for call-site symmetry; no-op since there's no lock.
export async function releaseProfile(_handle: ProfileHandle): Promise<void> {}

async function downloadAndExtract(s3Key: string, localDir: string): Promise<void> {
    // HEAD first so a missing tar doesn't log a noisy GetObject error.
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

    // Mirror S3 exactly so partial leftover state can't pollute the new profile.
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
