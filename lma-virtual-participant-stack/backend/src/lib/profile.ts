// CloakBrowser profile helpers — TypeScript port of the validation harness's
// lib/profile.mjs (which itself is a JS port of CloakBrowser-Manager's
// backend/browser_manager.py + database.py). See
// `cloakbrowser-validation/lib/profile.mjs` for the porting rationale.
//
// This module owns:
//   • patchPreferencesFor3pCookies — pre-write 3p-cookie exceptions for
//     meeting platforms before Chromium starts (Chrome v123+ default-blocks
//     3p cookies, breaking Zoom cross-domain auth on a fresh profile).
//   • profileIsFresh — detect first-launch profiles to gate the warmup.
//   • warmupNavigation — 3-phase navigation that builds cookies / Service
//     Worker / localStorage state on a fresh profile so the first meeting
//     join looks like a returning visitor.
//   • initProfileDefaults — write Bookmarks + DuckDuckGo Preferences on a
//     freshly created userDataDir so it doesn't look brand-new.
//   • cleanStaleLocks — remove SingletonLock/Cookie/Socket files left
//     behind by an unclean previous exit.
//   • buildLaunchArgs — cloakbrowser-specific --fingerprint-* flags.

import * as fs from 'fs';
import * as path from 'path';
import * as crypto from 'crypto';

// =============================================================================
// 3p-cookie pref patch — single most important Zoom-detection fix
// =============================================================================

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

// Chrome timestamps are microseconds since 1601-01-01 (Win32 FILETIME epoch).
// Date.now() is ms since 1970. Difference is 11_644_473_600 seconds.
const CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000;
// 90 days, matches what Chrome's URL-bar "temporarily allow" toggle writes.
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
        out[`*,${pat}`] = {
            setting: 1,
            lifetime,
            expiration,
            last_modified: lastModified,
        };
    }
    return out;
}

/**
 * Merge our 3p-cookie exceptions into Default/Preferences. Idempotent:
 * re-running on a patched profile re-asserts the same key/value. Must run
 * BEFORE Chromium launches — Chromium serializes its own copy on shutdown
 * and clobbers in-session edits.
 */
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

// =============================================================================
// Warmup — 3-phase navigation on fresh profiles
// =============================================================================

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

/**
 * A profile is "fresh" if Chromium has never written its Cache subdir.
 * That dir appears on the first navigation that loads any resource, so it's
 * a reliable "have we ever launched here?" signal even after we've created
 * the userDataDir + Default/ ourselves.
 */
export function profileIsFresh(userDataDir: string): boolean {
    return !fs.existsSync(path.join(userDataDir, 'Default', 'Cache'));
}

interface WarmupPage {
    goto(url: string, opts?: { waitUntil?: string; timeout?: number }): Promise<unknown>;
    close(): Promise<void>;
}

interface WarmupOptions {
    log?: (msg: string) => void;
    /** Skip phase 3. Default true (run meeting platforms). */
    runMeetingPlatforms?: boolean;
    /** Soft timeout for any single navigation. Default 20s. */
    navTimeoutMs?: number;
}

/**
 * Run the 3-phase warmup against a Puppeteer Browser (or Playwright
 * BrowserContext) by way of `pageOpener`. We deliberately do NOT run the
 * stealth-validation probe phase from the validation harness here — those
 * URLs (bot.sannysoft.com, etc.) are dev signals, not something we want to
 * leave in a real user's browsing history.
 */
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

// =============================================================================
// Profile defaults + dir layout
// =============================================================================

export interface CloakProfile {
    fingerprintSeed: number;
    platform: string;
    screenWidth: number;
    screenHeight: number;
    humanize: boolean;
    humanPreset: string;
    headless: boolean;
    extraArgs: string[];
}

export function randomFingerprintSeed(): number {
    // Match the Manager: 10000–99999 inclusive.
    return 10000 + Math.floor(Math.random() * 90000);
}

export function profileDefaults(overrides: Partial<CloakProfile> = {}): CloakProfile {
    return {
        fingerprintSeed: overrides.fingerprintSeed ?? randomFingerprintSeed(),
        platform: overrides.platform ?? 'windows',
        screenWidth: overrides.screenWidth ?? 1920,
        screenHeight: overrides.screenHeight ?? 1080,
        humanize: overrides.humanize ?? true,
        humanPreset: overrides.humanPreset ?? 'default',
        headless: overrides.headless ?? false,
        extraArgs: overrides.extraArgs ?? [],
    };
}

/**
 * Remove SingletonLock / SingletonCookie / SingletonSocket symlinks from a
 * userDataDir. Chromium leaves these behind after SIGKILL / OOM, and refuses
 * to launch (or silently falls back to a different dir) on the next start.
 */
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

/**
 * Write Bookmarks + a DuckDuckGo Preferences seed on a fresh profile so it
 * doesn't look brand-new to fingerprinters that flag empty default state.
 * Both files are only written when missing — accumulated state is preserved.
 *
 * Run AFTER the userDataDir + Default/ dirs are created and BEFORE
 * patchPreferencesFor3pCookies (which merges into Preferences).
 */
export function initProfileDefaults(userDataDir: string): void {
    const defaultDir = path.join(userDataDir, 'Default');
    fs.mkdirSync(defaultDir, { recursive: true });

    const bookmarksPath = path.join(defaultDir, 'Bookmarks');
    if (!fs.existsSync(bookmarksPath)) {
        // The Manager passes `int(time.time() * 1_000_000)` as the timestamp,
        // which is microseconds-since-1970 (not microseconds-since-1601).
        // Chrome accepts it without complaint, so we mirror that.
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

// =============================================================================
// Launch args — cloakbrowser-specific fingerprint flags
// =============================================================================

/**
 * Build the cloakbrowser-specific launch args. The `cloakbrowser` library
 * adds --no-sandbox + a random --fingerprint=<seed> + --fingerprint-platform
 * by default; we override the seed (so reuse is deterministic) and add the
 * Manager's standard supporting flags (--use-angle=swiftshader for
 * containers, --ignore-gpu-blocklist, etc.).
 */
export function buildLaunchArgs(
    profile: CloakProfile,
    opts: { useFakeMedia?: boolean } = {},
): string[] {
    const useFakeMedia = opts.useFakeMedia ?? true;

    const args = [
        `--fingerprint=${profile.fingerprintSeed}`,
        '--disable-infobars',
        '--test-type', // suppresses the "--no-sandbox" warning bar
        '--use-angle=swiftshader', // software GL, required in containers
        '--ignore-gpu-blocklist',
        `--fingerprint-screen-width=${profile.screenWidth}`,
        `--fingerprint-screen-height=${profile.screenHeight}`,
    ];

    if (profile.platform && profile.platform !== 'windows') {
        args.push(`--fingerprint-platform=${profile.platform}`);
    }

    if (useFakeMedia) {
        args.push('--use-fake-ui-for-media-stream');
    }

    return [...args, ...profile.extraArgs];
}

/**
 * One-call helper: prepare a profile dir at the given path (clean stale
 * locks, init defaults if first launch) and return everything the launch
 * site needs.
 *
 * Unlike the validation harness, this does NOT generate a UUID — the
 * production VP is given a stable per-(user, platform) directory by the
 * profile store.
 */
export function prepareProfile(opts: {
    userDataDir: string;
    fingerprintSeed?: number;
    screenWidth?: number;
    screenHeight?: number;
    extraArgs?: string[];
    useFakeMedia?: boolean;
}): { profile: CloakProfile; userDataDir: string; args: string[] } {
    const profile = profileDefaults({
        fingerprintSeed: opts.fingerprintSeed,
        screenWidth: opts.screenWidth,
        screenHeight: opts.screenHeight,
        extraArgs: opts.extraArgs,
    });
    fs.mkdirSync(opts.userDataDir, { recursive: true });
    cleanStaleLocks(opts.userDataDir);
    initProfileDefaults(opts.userDataDir);
    const args = buildLaunchArgs(profile, { useFakeMedia: opts.useFakeMedia ?? true });
    return { profile, userDataDir: opts.userDataDir, args };
}

/**
 * Stable per-user fingerprint seed derived from the Cognito sub. The same
 * user gets the same seed every launch ("returning user"), but each user
 * gets a different one ("we have many users"). Seeds outside the Manager's
 * 10000–99999 range still work but break parity with the Manager UI; we
 * stay in-range.
 */
export function fingerprintSeedForUser(cognitoSub: string): number {
    const h = crypto.createHash('sha256').update(cognitoSub).digest();
    // Read a 32-bit unsigned int and clamp to [10000, 99999].
    const n = h.readUInt32BE(0);
    return 10000 + (n % 90000);
}

