// CloakBrowser userDataDir helpers: 3p-cookie patch, freshness check,
// warmup, default state seeding, stale-lock cleanup, fingerprint seed.

import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

const THIRD_PARTY_COOKIE_ALLOW_PATTERNS = [
    'https://[*.]zoom.us',
    'https://[*.]zoom.com',
    'https://[*.]chime.aws',
    'https://[*.]teams.microsoft.com',
    'https://[*.]office.com',
    'https://[*.]live.com',
    'https://[*.]webex.com',
    'https://[*.]meet.google.com',
    'https://[*.]google.com',
    'https://[*.]googleusercontent.com',
];

// Chrome timestamps: microseconds since 1601-01-01 (Win32 FILETIME epoch).
const CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000;
const EXCEPTION_LIFETIME_US = 90 * 24 * 60 * 60 * 1_000_000;

function chromeTimestampNowUs(): bigint {
    return BigInt(Date.now()) * 1000n + BigInt(CHROME_EPOCH_OFFSET_US);
}

function buildCookieExceptions(): Record<string, unknown> {
    const now = chromeTimestampNowUs();
    const expiration = (now + BigInt(EXCEPTION_LIFETIME_US)).toString();
    const lastModified = now.toString();
    const lifetime = String(EXCEPTION_LIFETIME_US);
    const out: Record<string, unknown> = {};
    for (const pat of THIRD_PARTY_COOKIE_ALLOW_PATTERNS) {
        // setting=1 ALLOW, =2 BLOCK, =4 SESSION-ONLY.
        out[`*,${pat}`] = { setting: 1, lifetime, expiration, last_modified: lastModified };
    }
    return out;
}

// Chrome v123+ default-blocks 3p cookies, breaking Zoom cross-domain auth.
// Must run BEFORE Chromium launches; Chromium clobbers in-session edits at shutdown.
export function patchPreferencesFor3pCookies(userDataDir: string): number {
    const prefsPath = path.join(userDataDir, 'Default', 'Preferences');
    fs.mkdirSync(path.dirname(prefsPath), { recursive: true });

    let prefs: Record<string, any> = {};
    if (fs.existsSync(prefsPath)) {
        try {
            prefs = JSON.parse(fs.readFileSync(prefsPath, 'utf8'));
        } catch {
            prefs = {};
        }
    }

    const profileNode = prefs.profile ?? (prefs.profile = {});
    const csNode = profileNode.content_settings ?? (profileNode.content_settings = {});
    const excNode = csNode.exceptions ?? (csNode.exceptions = {});
    const cookiesNode = excNode.cookies ?? (excNode.cookies = {});
    Object.assign(cookiesNode, buildCookieExceptions());

    fs.writeFileSync(prefsPath, JSON.stringify(prefs, null, 2));
    return THIRD_PARTY_COOKIE_ALLOW_PATTERNS.length;
}

// Custom URL schemes the meeting platforms try to hand off to a native desktop
// app. When Chromium hits one of these (e.g. a Webex j.php landing page that
// auto-launches the desktop client) it pops a native "Open <app>?" / "Open
// xdg-open?" external-protocol chooser. That dialog is NOT a JS dialog, so
// page.on('dialog') can't catch it — it just sits there as noise (and on some
// builds steals focus from the join UI). Marking the scheme as excluded in
// Preferences tells Chromium to silently ignore the handoff and stay on the
// web client. Headless containers have no desktop app to launch anyway.
const EXTERNAL_PROTOCOL_EXCLUDED_SCHEMES = [
    'webex',
    'webexstart',
    'wbxstart',
    'wbx',
    'msteams',
    'zoommtg',
    'zoomus',
    'zoomphonecall',
];

// Suppress the native external-protocol ("Open xdg-open?") chooser for the
// meeting-app custom schemes above. Must run BEFORE Chromium launches; like the
// cookie patch, Chromium clobbers in-session edits to Preferences at shutdown.
// Returns the number of schemes excluded.
export function patchPreferencesForExternalProtocols(userDataDir: string): number {
    const prefsPath = path.join(userDataDir, 'Default', 'Preferences');
    fs.mkdirSync(path.dirname(prefsPath), { recursive: true });

    let prefs: Record<string, any> = {};
    if (fs.existsSync(prefsPath)) {
        try {
            prefs = JSON.parse(fs.readFileSync(prefsPath, 'utf8'));
        } catch {
            prefs = {};
        }
    }

    const protoNode = prefs.protocol_handler ?? (prefs.protocol_handler = {});
    const excludedNode = protoNode.excluded_schemes ?? (protoNode.excluded_schemes = {});
    // value=true => scheme is excluded: Chromium neither launches it nor prompts.
    for (const scheme of EXTERNAL_PROTOCOL_EXCLUDED_SCHEMES) {
        excludedNode[scheme] = true;
    }

    fs.writeFileSync(prefsPath, JSON.stringify(prefs, null, 2));
    return EXTERNAL_PROTOCOL_EXCLUDED_SCHEMES.length;
}

const WARMUP_ORDINARY_URLS = [
    'https://www.google.com/',
    'https://news.ycombinator.com/',
    'https://en.wikipedia.org/wiki/Special:Random',
];

const WARMUP_MEETING_PLATFORMS = [
    'https://zoom.us/',
    'https://app.chime.aws/',
    'https://teams.microsoft.com/',
    'https://web.webex.com/',
];

// Cookies file is created on first navigation; survives the profile tar
// (Cache dirs are commonly excluded so aren't a reliable signal). Chromium
// writes the DB at Default/Network/Cookies on modern builds and Default/Cookies
// on older ones — check both so a restored profile isn't mis-flagged as fresh.
export function profileIsFresh(userDataDir: string): boolean {
    return !(
        fs.existsSync(path.join(userDataDir, 'Default', 'Network', 'Cookies')) ||
        fs.existsSync(path.join(userDataDir, 'Default', 'Cookies'))
    );
}

interface WarmupPage {
    goto(url: string, opts?: { waitUntil?: string; timeout?: number }): Promise<unknown>;
    close(): Promise<void>;
}

interface WarmupOptions {
    log?: (msg: string) => void;
    runMeetingPlatforms?: boolean;
    navTimeoutMs?: number;
}

// Builds cookies/SW/storage state on a fresh profile so the first meeting
// join looks like a returning visitor.
export async function warmupNavigation(
    pageOpener: () => Promise<WarmupPage>,
    options: WarmupOptions = {},
): Promise<void> {
    const log = options.log ?? ((m: string) => console.log(m));
    const runMeetingPlatforms = options.runMeetingPlatforms ?? true;
    const navTimeout = options.navTimeoutMs ?? 20_000;

    const page = await pageOpener();
    try {
        for (const url of WARMUP_ORDINARY_URLS) {
            log(`[warmup] (ordinary)   ${url}`);
            try {
                await page.goto(url, { waitUntil: 'domcontentloaded', timeout: navTimeout });
                await sleep(2_000);
            } catch (err) {
                log(`[warmup]   hop failed (non-fatal): ${(err as Error).message ?? err}`);
            }
        }

        if (runMeetingPlatforms) {
            for (const url of WARMUP_MEETING_PLATFORMS) {
                log(`[warmup] (meeting)    ${url}`);
                try {
                    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: navTimeout });
                    await sleep(2_000);
                } catch (err) {
                    log(`[warmup]   hop failed (non-fatal): ${(err as Error).message ?? err}`);
                }
            }
        }
    } finally {
        try {
            await page.close();
        } catch {
            // ignore
        }
    }

    const total = WARMUP_ORDINARY_URLS.length + (runMeetingPlatforms ? WARMUP_MEETING_PLATFORMS.length : 0);
    log(`[warmup] complete (${total} sites visited)`);
}

function sleep(ms: number): Promise<void> {
    return new Promise((r) => setTimeout(r, ms));
}

export function randomFingerprintSeed(): number {
    return 10000 + Math.floor(Math.random() * 90000);
}

// Deterministic seed from any stable string. Used for anonymous / CLI-launched
// VPs that have no Cognito sub: deriving the fingerprint from a deployment-
// stable identifier (e.g. the profiles bucket name) makes every join from that
// deployment present the SAME device fingerprint instead of a brand-new random
// one each launch. A stable fingerprint reads as a returning device rather than
// a never-before-seen one, which is a weaker bot signal to platforms like
// Teams that gate anonymous joins behind a HIP CAPTCHA. Falls back to a random
// seed only when no stable identifier is available at all.
export function stableFingerprintSeed(stableId: string): number {
    const id = (stableId || '').trim();
    if (!id) return randomFingerprintSeed();
    const h = crypto.createHash('sha256').update(id).digest();
    return 10000 + (h.readUInt32BE(0) % 90000);
}

// Stale Singleton* files from SIGKILL/OOM block re-launch.
export function cleanStaleLocks(userDataDir: string): void {
    for (const name of ['SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
        const p = path.join(userDataDir, name);
        try {
            fs.unlinkSync(p);
        } catch (err: any) {
            if (err?.code !== 'ENOENT') throw err;
        }
    }
}

// Seed Bookmarks + DuckDuckGo prefs so a fresh profile doesn't look brand-new.
// Skipped per-file when already present.
export function initProfileDefaults(userDataDir: string): void {
    const defaultDir = path.join(userDataDir, 'Default');
    fs.mkdirSync(defaultDir, { recursive: true });

    const bookmarksPath = path.join(defaultDir, 'Bookmarks');
    if (!fs.existsSync(bookmarksPath)) {
        const ts = String(Date.now() * 1000);
        let nextId = 1;
        const bm = (name: string, url: string) => ({
            type: 'url',
            id: String(++nextId),
            name,
            url,
            date_added: ts,
        });
        const folder = (name: string, children: unknown[]) => ({
            type: 'folder',
            id: String(++nextId),
            name,
            date_added: ts,
            date_modified: ts,
            children,
        });

        const bookmarks = {
            checksum: '',
            roots: {
                bookmark_bar: {
                    type: 'folder',
                    id: '1',
                    name: 'Bookmarks bar',
                    date_added: ts,
                    date_modified: ts,
                    children: [
                        folder('Meetings', [
                            bm('Amazon Chime', 'https://app.chime.aws/'),
                            bm('Microsoft Teams', 'https://teams.microsoft.com/'),
                            bm('Zoom', 'https://zoom.us/'),
                            bm('Webex', 'https://web.webex.com/'),
                            bm('Google Meet', 'https://meet.google.com/'),
                        ]),
                        folder('Work', [
                            bm('AWS Console', 'https://console.aws.amazon.com/'),
                            bm('GitHub', 'https://github.com/'),
                            bm('Outlook', 'https://outlook.office.com/'),
                        ]),
                    ],
                },
                other: { type: 'folder', id: '2', name: 'Other bookmarks', children: [] },
                synced: { type: 'folder', id: '3', name: 'Mobile bookmarks', children: [] },
            },
            version: 1,
        };
        fs.writeFileSync(bookmarksPath, JSON.stringify(bookmarks, null, 2));
    }

    const prefsPath = path.join(defaultDir, 'Preferences');
    if (!fs.existsSync(prefsPath)) {
        const prefs = {
            default_search_provider_data: {
                template_url_data: {
                    keyword: 'duckduckgo.com',
                    short_name: 'DuckDuckGo',
                    url: 'https://duckduckgo.com/?q={searchTerms}',
                    suggestions_url: 'https://duckduckgo.com/ac/?q={searchTerms}&type=list',
                    favicon_url: 'https://duckduckgo.com/favicon.ico',
                },
            },
            default_search_provider: { enabled: true },
        };
        fs.writeFileSync(prefsPath, JSON.stringify(prefs, null, 2));
    }
}

// Stable per-user seed = "returning visitor" fingerprint across launches.
export function fingerprintSeedForUser(cognitoSub: string): number {
    const h = crypto.createHash('sha256').update(cognitoSub).digest();
    return 10000 + (h.readUInt32BE(0) % 90000);
}
