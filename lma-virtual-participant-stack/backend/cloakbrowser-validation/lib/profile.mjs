// =============================================================================
// CloakBrowser profile helpers — JS port of the relevant bits of
// CloakBrowser-Manager's `backend/browser_manager.py` and `database.py`.
// -----------------------------------------------------------------------------
// We port (rather than vendor) the Manager's profile-creation glue because:
//   • The "what makes a profile stealth-effective" code is small (~150 LOC)
//     and has been stable since the Manager's initial release in March 2026.
//     Almost all updates since have been operational/UX (CDP port rotation,
//     auto-launch flag, clipboard sync) — none stealth-critical.
//   • All the *real* stealth value lives in the `cloakbrowser` PyPI library
//     (which we already pin and can update independently). The Manager's
//     backend is glue; it just composes cloakbrowser into a launch call.
//   • Porting keeps the security surface limited to what's in our own image
//     (better for ECR/Mirador scanning than `FROM cloakhq/cloakbrowser-manager`).
//   • LMA's virtual participant doesn't need the Manager's FastAPI server,
//     React frontend, or SQLite profile database — just the launch logic.
//
// Source files we ported from (CloakBrowser-Manager v0.0.8):
//   - backend/browser_manager.py: launch(), _init_profile_defaults(), and
//     _build_fingerprint_args()
//   - backend/database.py:        create_profile() default field values
//
// Things we deliberately did NOT port:
//   - The clipboard-sync init script (irrelevant for VP automation)
//   - The KasmVNC/Xvnc display allocation (we use the existing Xvfb pipeline)
//   - The CDP port allocator (Playwright/Puppeteer pick one for us)
//   - The SQLite-backed profile registry (we identify profiles by directory
//     name; if the production VP wants a registry it can add one separately)
// =============================================================================

import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

// =============================================================================
// THE ACTUAL FIX FOR ZOOM (and other meeting platforms): Chrome blocks
// third-party cookies by default since v123 (early 2025). Meeting platforms
// like Zoom rely on cross-domain auth cookies (e.g. zoom.us iframe embedded
// inside app.zoom.us, or zoomgov.com → zoom.us auth handoffs) and their
// "is this a real browser?" checks fail without them. A brand-new profile
// loaded straight into Zoom shows "join from your browser is not supported"
// or silently bot-flags the session — UNTIL the user clicks the URL bar's
// shield icon and toggles "Allow third-party cookies for this site".
//
// We pre-write that toggle's effect into Default/Preferences before first
// launch, for every meeting platform we want to support. Verified by diffing
// a Preferences file before/after toggling the URL-bar setting in Chrome 146.
// =============================================================================

// Pattern format follows Chrome's `[primary],[secondary]` content-settings
// convention:
//   "*"             in primary slot = match any top-level URL
//   "https://[*.]X" in secondary    = wildcard subdomain match for the
//                                     embedded resource
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
// Date.now() is milliseconds since 1970. Difference is a constant.
const CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000;
// 90 days in microseconds — matches what Chrome's URL-bar toggle uses for
// "temporarily allow" (Chromium auto-renews on each access, so practical
// lifetime is "until you stop using the site").
const EXCEPTION_LIFETIME_US = 90 * 24 * 60 * 60 * 1_000_000;
// 7,776,000,000,000

function chromeTimestampNowUs() {
  // Date.now() is ms; multiply by 1000 to get microseconds, then add the
  // 1601-vs-1970 offset.
  return BigInt(Date.now()) * 1000n + BigInt(CHROME_EPOCH_OFFSET_US);
}

function buildCookieExceptions() {
  const now           = chromeTimestampNowUs();
  const expiration    = (now + BigInt(EXCEPTION_LIFETIME_US)).toString();
  const lastModified  = now.toString();
  const lifetime      = String(EXCEPTION_LIFETIME_US);
  const out = {};
  for (const pat of THIRD_PARTY_COOKIE_ALLOW_PATTERNS) {
    // setting=1 = ALLOW, =2 BLOCK, =4 SESSION-ONLY.
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
 * Merge our 3p-cookie exceptions into the profile's `Default/Preferences`.
 * Must run BEFORE Chromium launches (Chromium serializes its own copy of
 * Preferences on shutdown and clobbers any in-session edits).
 *
 * Idempotent: re-running on an already-patched profile re-asserts the
 * settings (same key → same value) without disturbing other entries.
 *
 * Returns the count of exception entries written, for logging.
 */
export function patchPreferencesFor3pCookies(userDataDir) {
  const prefsPath = path.join(userDataDir, 'Default', 'Preferences');
  fs.mkdirSync(path.dirname(prefsPath), { recursive: true });

  let prefs = {};
  if (fs.existsSync(prefsPath)) {
    try {
      prefs = JSON.parse(fs.readFileSync(prefsPath, 'utf8'));
    } catch {
      // Corrupted Preferences (shouldn't happen). Replacing it loses
      // DuckDuckGo + any other init defaults; this is fine for our use case
      // since initProfileDefaults() will rewrite them on the next call.
      prefs = {};
    }
  }

  // profile.content_settings.exceptions.cookies — that's where Chrome
  // stores per-site cookie exceptions, including the 3p ones the URL-bar
  // toggle creates.
  const profileNode = prefs.profile               ?? (prefs.profile               = {});
  const csNode      = profileNode.content_settings ?? (profileNode.content_settings = {});
  const excNode     = csNode.exceptions            ?? (csNode.exceptions            = {});
  const cookiesNode = excNode.cookies              ?? (excNode.cookies              = {});
  Object.assign(cookiesNode, buildCookieExceptions());

  fs.writeFileSync(prefsPath, JSON.stringify(prefs, null, 2));
  return THIRD_PARTY_COOKIE_ALLOW_PATTERNS.length;
}

// =============================================================================
// 3-PHASE WARMUP — JS port of validate-manager.py's _warmup_navigation().
// -----------------------------------------------------------------------------
// Phase 1: ordinary-user browsing (Google, HN, Wikipedia random)
// Phase 2: cloaktest-equivalent stealth probes — the same 4 sites the
//          CloakBrowser repo's tests/test_stealth.py + examples/stealth_test.py
//          hit. Each probe runs an inline DOM check and prints PASS/FAIL so
//          we can confirm the C++ stealth patches are live before attempting
//          the meeting platforms.
// Phase 3: meeting platform homepages — establishes cookies/SW/localStorage
//          state on each platform so the eventual navigation to the actual
//          meeting URL doesn't look like a brand-new visit.
//
// All three phases run on a single scratch page so the main page (the one
// that'll navigate to TARGET_URL after warmup) keeps a clean history.
// Cookie/Storage/SW state is profile-wide so it carries over to the main
// page just fine.
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

// Subset of the cloaktest probes that work well as a quick pass/fail signal.
// Each entry has a `checkJs` (string, runs in the page) returning a small
// JSON object, and a `summarize(raw) → {ok, summary}` that interprets it.
// We tolerate failures — these are sanity checks, not blocking gates.
const STEALTH_PROBES = [
  {
    name:       'bot.sannysoft.com',
    url:        'https://bot.sannysoft.com/',
    waitUntil:  'networkidle',
    settleMs:   3000,
    checkJs: `() => {
      const rows = document.querySelectorAll('table tr');
      const failed = [];
      let total = 0;
      rows.forEach(r => {
        const cells = r.querySelectorAll('td');
        if (cells.length >= 2) {
          total++;
          const cls = cells[1].className || '';
          if (cls.includes('failed')) failed.push(cells[0].innerText.trim());
        }
      });
      return {total, failed};
    }`,
    summarize: (r) => ({
      ok:      (r.failed?.length ?? 1) === 0,
      summary: `${r.total ?? 0} checks; failures: ${r.failed?.length ? r.failed.join(',') : 'none'}`,
    }),
  },
  {
    name:       'browserscan.net',
    url:        'https://www.browserscan.net/bot-detection',
    waitUntil:  'networkidle',
    settleMs:   5000,
    checkJs: `() => {
      const text = document.body.innerText;
      const normal   = (text.match(/Normal/g)   || []).length;
      const abnormal = (text.match(/Abnormal/g) || []).length;
      return {normal, abnormal};
    }`,
    summarize: (r) => ({
      ok:      (r.abnormal ?? 1) === 0,
      summary: `normal=${r.normal} abnormal=${r.abnormal}`,
    }),
  },
  {
    name:       'deviceandbrowserinfo.com',
    url:        'https://deviceandbrowserinfo.com/are_you_a_bot',
    waitUntil:  'domcontentloaded',
    settleMs:   8000,
    checkJs: `() => {
      const text = document.body.innerText;
      const m = text.match(/"isBot":\\s*(true|false)/);
      return {isBot: m ? m[1] === 'true' : null};
    }`,
    summarize: (r) => ({
      ok:      r.isBot === false,
      summary: `isBot=${r.isBot}`,
    }),
  },
  {
    name:       'FingerprintJS demo',
    url:        'https://demo.fingerprint.com/web-scraping',
    waitUntil:  'domcontentloaded',
    settleMs:   5000,
    checkJs: `() => {
      const text = document.body.innerText;
      const isBlocked = text.includes('request was blocked') ||
                        text.includes('bot visit detected');
      return {isBlocked};
    }`,
    summarize: (r) => ({
      ok:      r.isBlocked === false,
      summary: r.isBlocked === false ? 'not blocked' : 'BLOCKED',
    }),
  },
];

/**
 * Detect whether a profile dir has been used before. We check for the
 * existence of `Default/Cache` because Chromium creates it on the first
 * navigation that loads any resource. Mere directory existence isn't
 * enough — our prepareProfile() creates `Default/` empty.
 */
export function profileIsFresh(userDataDir) {
  return !fs.existsSync(path.join(userDataDir, 'Default', 'Cache'));
}

// Back-compat: some callers may still reference WARMUP_URLS as the flat list.
export const WARMUP_URLS = [
  ...WARMUP_ORDINARY_URLS,
  ...WARMUP_MEETING_PLATFORMS,
];

/**
 * Run the 3-phase warmup against an open browser context/browser.
 *
 * @param {() => Promise<Page>} pageOpener  Opens a fresh page. For Playwright
 *   pass `() => context.newPage()`. For Puppeteer pass `() => browser.newPage()`.
 *   We open a single scratch page and reuse it across all phases.
 * @param {object} [options]
 * @param {(msg: string) => void} [options.log]  Defaults to console.log.
 * @param {boolean} [options.runProbes]  Skip phase 2. Default true.
 * @param {boolean} [options.runMeetingPlatforms]  Skip phase 3. Default true.
 *
 * Pass a Playwright BrowserContext OR a Puppeteer Browser — both expose
 * `newPage()`. The shape of the returned `page` object is similar enough
 * for `goto()`/`evaluate()`/`close()` to work uniformly.
 */
export async function warmupNavigation(pageOpener, options = {}) {
  const log                  = options.log ?? console.log;
  const runProbes            = options.runProbes ?? true;
  const runMeetingPlatforms  = options.runMeetingPlatforms ?? true;

  const page = await pageOpener();
  try {
    // ---- Phase 1: ordinary-user browsing -------------------------------
    for (const url of WARMUP_ORDINARY_URLS) {
      log(`[warmup] (ordinary)   ${url}`);
      try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20_000 });
        await sleep(2_000);
      } catch (err) {
        log(`[warmup]   hop failed (non-fatal): ${err.message ?? err}`);
      }
    }

    // ---- Phase 2: stealth-validation probes (cloaktest-equivalent) -----
    if (runProbes) {
      log('[warmup] === stealth probe results ===');
      for (const probe of STEALTH_PROBES) {
        log(`[warmup] (probe)      ${probe.name} (${probe.url})`);
        try {
          // Puppeteer doesn't accept 'networkidle' as a string; it wants
          // 'networkidle0' / 'networkidle2'. Playwright accepts 'networkidle'
          // directly. Map between them transparently.
          const waitUntil = mapWaitUntil(probe.waitUntil, page);
          await page.goto(probe.url, { waitUntil, timeout: 30_000 });
          await sleep(probe.settleMs);
          // page.evaluate accepts function-source-as-string in both libs.
          // eslint-disable-next-line no-new-func
          const fn = new Function(`return (${probe.checkJs})()`);
          const raw = await page.evaluate(fn);
          const { ok, summary } = probe.summarize(raw);
          const marker = ok ? '✅ PASS' : '❌ FAIL';
          log(`[warmup]              ${marker}  ${summary}`);
        } catch (err) {
          log(`[warmup]              ⚠  probe error (non-fatal): ${err.message ?? err}`);
        }
      }
    }

    // ---- Phase 3: meeting platform homepages ---------------------------
    if (runMeetingPlatforms) {
      for (const url of WARMUP_MEETING_PLATFORMS) {
        log(`[warmup] (meeting)    ${url}`);
        try {
          await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20_000 });
          await sleep(2_000);
        } catch (err) {
          log(`[warmup]   hop failed (non-fatal): ${err.message ?? err}`);
        }
      }
    }
  } finally {
    try { await page.close(); } catch { /* ignore */ }
  }

  const total =
    WARMUP_ORDINARY_URLS.length +
    (runProbes ? STEALTH_PROBES.length : 0) +
    (runMeetingPlatforms ? WARMUP_MEETING_PLATFORMS.length : 0);
  log(`[warmup] complete (${total} sites visited across 3 phases)`);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// Playwright accepts: 'load' | 'domcontentloaded' | 'networkidle' | 'commit'
// Puppeteer  accepts: 'load' | 'domcontentloaded' | 'networkidle0' | 'networkidle2'
// We disambiguate by sniffing for a Playwright-only method (route()) on the
// page object's parent context vs Puppeteer's _client method. Simplest:
// check for `page.waitForNetworkIdle` (Puppeteer) vs not (Playwright).
function mapWaitUntil(value, page) {
  if (value === 'networkidle' && typeof page.waitForNetworkIdle === 'function') {
    // Puppeteer page → translate to networkidle2 (less strict, less likely
    // to time out on chatty homepages).
    return 'networkidle2';
  }
  return value;
}
// =============================================================================
// Below: the existing profile-helpers (random seed, defaults, dir resolver,
// stale-lock cleanup, init-defaults, launch-args). Unchanged — see the
// per-function docstrings.
// =============================================================================

// (placeholder — original content continues below)

/**
 * Generate a numeric fingerprint seed. Mirrors database.py:
 *     seed = fingerprint_seed if fingerprint_seed is not None
 *            else random.randint(10000, 99999)
 *
 * The CloakBrowser binary derives its full per-launch fingerprint (canvas
 * noise, WebGL noise, audio noise, client rects, etc.) from this seed. Same
 * seed = same identity across launches ("returning user"). Random seed each
 * launch = different identity each time, which is what you want for one-shot
 * scrapes but suspicious for sites that track you across visits.
 */
export function randomFingerprintSeed() {
  // 10000–99999 inclusive — exactly what the Manager generates.
  return 10000 + Math.floor(Math.random() * 90000);
}

/**
 * Default profile fields. Mirrors database.py create_profile() defaults.
 * Pass any subset; missing fields fall back to these.
 *
 * The defaults match what the Manager UI's "Create Profile" button creates
 * when you don't change anything — random seed, Windows desktop, 1920x1080,
 * humanize on, fake-media flags ON (we add those at launch time).
 */
export function profileDefaults(overrides = {}) {
  return {
    fingerprintSeed: overrides.fingerprintSeed ?? randomFingerprintSeed(),
    platform:        overrides.platform        ?? 'windows',
    screenWidth:     overrides.screenWidth     ?? 1920,
    screenHeight:    overrides.screenHeight    ?? 1080,
    locale:          overrides.locale          ?? null,    // null = inherit from system
    timezone:        overrides.timezone        ?? null,
    humanize:        overrides.humanize        ?? true,
    humanPreset:     overrides.humanPreset     ?? 'default',
    headless:        overrides.headless        ?? false,
    geoip:           overrides.geoip           ?? false,
    colorScheme:     overrides.colorScheme     ?? null,
    userAgent:       overrides.userAgent       ?? null,
    proxy:           overrides.proxy           ?? null,
    extraArgs:       overrides.extraArgs       ?? [],      // free-form launch_args
  };
}

/**
 * Resolve a profile directory inside /data. Two modes:
 *   - profileId provided: /data/profiles/<id>            (named, reusable)
 *   - profileId omitted:  /data/profiles/<random-uuid>   (fresh per-run)
 *
 * Mirrors database.py:
 *     user_data_dir = str(DATA_DIR / "profiles" / profile_id)
 *
 * The "profiles" subdirectory keeps the Manager's tree structure intact so a
 * profile created here is openable in the Manager UI when both share the
 * same /data volume — useful for warming up a profile in the Manager UI then
 * handing it off to the production VP.
 */
export function resolveProfileDir({ dataDir = '/data', profileId } = {}) {
  const id = profileId ?? crypto.randomUUID();
  const dir = path.join(dataDir, 'profiles', id);
  fs.mkdirSync(dir, { recursive: true });
  return { profileId: id, userDataDir: dir };
}

/**
 * Remove stale Chromium lock files from a profile directory. Mirrors
 * browser_manager.py:
 *     for lock_file in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
 *         lock_path = user_data_dir / lock_file
 *         lock_path.unlink(missing_ok=True)
 *
 * Chromium creates these on launch and is supposed to clean them up on exit.
 * If the previous run was killed (SIGKILL, container OOM, etc.) the locks
 * stay behind and the next launch will refuse to start the profile, or
 * worse, silently fall back to a new profile dir.
 */
export function cleanStaleLocks(userDataDir) {
  for (const name of ['SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
    const p = path.join(userDataDir, name);
    try {
      fs.unlinkSync(p);
    } catch (err) {
      if (err.code !== 'ENOENT') {
        // Re-throw anything other than "doesn't exist" — permission errors,
        // I/O errors, etc. are real problems.
        throw err;
      }
    }
  }
}

/**
 * Initialize a fresh profile directory with sensible Chromium defaults so
 * it doesn't look brand-new to fingerprinters that check for default-state
 * markers (empty Bookmarks file, no preferences). Mirrors the Manager's
 * _init_profile_defaults().
 *
 * Specifically writes:
 *   - {dir}/Default/Bookmarks   — a small set of bookmarks in the bookmark bar
 *   - {dir}/Default/Preferences — DuckDuckGo as default search engine
 *
 * Both are written ONLY if the file doesn't already exist (so a profile
 * that's accumulated state through actual use isn't clobbered).
 *
 * NOTE: We use a slightly less "automation-flavored" bookmark set than the
 * Manager's defaults — the Manager bookmarks "BrowserLeaks", "FingerprintJS
 * Demo", etc. (literally the bot-detection sites it's used for testing).
 * For the LMA virtual participant we want bookmarks that look like a real
 * remote-work user (meeting platforms, news, productivity tools).
 */
export function initProfileDefaults(userDataDir) {
  const defaultDir = path.join(userDataDir, 'Default');
  fs.mkdirSync(defaultDir, { recursive: true });

  // ---- Bookmarks ----------------------------------------------------------
  const bookmarksPath = path.join(defaultDir, 'Bookmarks');
  if (!fs.existsSync(bookmarksPath)) {
    // Chrome stores timestamps as microseconds since 1601-01-01 (Win32 epoch).
    // The Manager fudges this with `int(time.time() * 1_000_000)` which is
    // microseconds since 1970 — Chrome accepts it without complaint, so we
    // do the same. (Real Chrome timestamps would be ~13 trillion higher.)
    const ts = String(Date.now() * 1000);
    let nextId = 1;
    const bm = (name, url) => ({
      type: 'url',
      id: String(++nextId),
      name,
      url,
      date_added: ts,
    });
    const folder = (name, children) => ({
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
              bm('Google Workspace', 'https://workspace.google.com/'),
              bm('Outlook', 'https://outlook.office.com/'),
            ]),
            folder('News', [
              bm('Hacker News', 'https://news.ycombinator.com/'),
              bm('Reuters', 'https://www.reuters.com/'),
              bm('NYT', 'https://www.nytimes.com/'),
            ]),
          ],
        },
        other:  { type: 'folder', id: '2', name: 'Other bookmarks',  children: [] },
        synced: { type: 'folder', id: '3', name: 'Mobile bookmarks', children: [] },
      },
      version: 1,
    };
    fs.writeFileSync(bookmarksPath, JSON.stringify(bookmarks, null, 2));
  }

  // ---- Preferences (default search engine) -------------------------------
  // The Manager sets DuckDuckGo as the default search engine. We do the same
  // — Google as default would require the profile to log in to Google to
  // look "real", DuckDuckGo doesn't require an account so it's a more
  // plausible "fresh remote-work device" default.
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

/**
 * Build the launch args list. Mirrors browser_manager.py's
 * _build_fingerprint_args(profile) plus the extra meeting-platform flags
 * we need for headless containers (fake media devices).
 *
 * The cloakbrowser library's get_default_stealth_args() ALREADY adds
 * --no-sandbox + --fingerprint=<random> + --fingerprint-platform=windows
 * automatically. We override the fingerprint seed with our pinned value
 * (so reuse is deterministic) and add the extra flags the Manager always
 * tacks on.
 */
export function buildLaunchArgs(profile, { useFakeMedia = true } = {}) {
  const args = [
    // Pin the seed (overrides cloakbrowser's random per-launch seed).
    `--fingerprint=${profile.fingerprintSeed}`,

    // The Manager always sets these three; cloakbrowser doesn't add them
    // by default. See the parity diff in our README.
    '--disable-infobars',
    '--test-type',                  // suppresses the "--no-sandbox" warning bar
    '--use-angle=swiftshader',      // software GL — critical in containers
    '--ignore-gpu-blocklist',       // required so swiftshader actually inits

    // Pin reported screen dimensions (separate from the actual Xvfb size).
    `--fingerprint-screen-width=${profile.screenWidth}`,
    `--fingerprint-screen-height=${profile.screenHeight}`,

    // Override --fingerprint-platform if the caller asked for non-windows
    // (cloakbrowser defaults to windows on Linux already).
    ...(profile.platform && profile.platform !== 'windows'
      ? [`--fingerprint-platform=${profile.platform}`]
      : []),
  ];

  if (useFakeMedia) {
    // Containers don't have a real cam/mic. Without these, getUserMedia()
    // rejects and meeting platforms (Zoom, Teams, etc.) refuse to join with
    // "Your browser is preventing access to your microphone." The Manager
    // doesn't set these because it's a general-purpose browser; we always
    // need them for the VP use case.
    args.push('--use-fake-ui-for-media-stream');
    args.push('--use-fake-device-for-media-stream');
  }

  return [...args, ...profile.extraArgs];
}

/**
 * One-call helper: prepare a profile directory (resolve path, clean stale
 * locks, initialize defaults if first launch) and return everything the
 * caller needs to invoke `launchPersistentContext()`.
 *
 * Usage:
 *
 *   import { prepareProfile } from './lib/profile.mjs';
 *   import { launchPersistentContext } from 'cloakbrowser';
 *
 *   const { userDataDir, args } = prepareProfile({
 *     profileId: process.env.PROFILE_ID,   // omit for fresh-per-run
 *     fingerprintSeed: 42069,              // omit for random
 *   });
 *   const ctx = await launchPersistentContext({
 *     userDataDir, headless: false, humanize: true, args,
 *     viewport: { width: 1920, height: 1080 - 133 },
 *   });
 */
export function prepareProfile(opts = {}) {
  const profile = profileDefaults(opts);
  const { profileId, userDataDir } = resolveProfileDir({
    dataDir:    opts.dataDir,
    profileId:  opts.profileId,
  });
  cleanStaleLocks(userDataDir);
  initProfileDefaults(userDataDir);
  const args = buildLaunchArgs(profile, { useFakeMedia: opts.useFakeMedia ?? true });
  return { profile, profileId, userDataDir, args };
}
