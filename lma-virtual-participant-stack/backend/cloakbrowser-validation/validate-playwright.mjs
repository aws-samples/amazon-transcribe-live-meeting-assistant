// =============================================================================
// CloakBrowser validation — Playwright path
// -----------------------------------------------------------------------------
// Drop-in replacement for `playwright.chromium.launch()`. Per the CloakBrowser
// docs, this is the recommended path for sites with reCAPTCHA Enterprise / CF
// Turnstile (Puppeteer's CDP usage leaks more automation signals).
//
// This script calls into `lib/profile.mjs` — our JS port of the
// CloakBrowser-Manager profile-launch glue (browser_manager.py +
// database.py). The port gives us the same per-profile behavior the Manager
// has when you click "Create Profile" + "Launch":
//   • Per-profile UUID userDataDir under /data/profiles/<id>
//   • Random or pinned --fingerprint=<seed>
//   • _init_profile_defaults() — bookmarks + DuckDuckGo as default search
//   • Stale-lock cleanup (SingletonLock/Cookie/Socket from previous crashes)
//   • The full Manager launch-arg set (swiftshader, ignore-gpu-blocklist,
//     --disable-infobars, --test-type, --fingerprint-platform=windows,
//     --fingerprint-screen-{w,h}=…)
//
// What we add on top of the Manager's defaults: the fake-media flags
// (--use-fake-{ui,device}-for-media-stream) which the Manager doesn't pass
// because it's a general-purpose browser. Container-hosted automation can't
// get past meeting platforms' "your browser is blocking your microphone"
// error without them.
//
// What this script ALSO does (port of validate-manager.py's two key fixes):
//   (1) THIRD-PARTY-COOKIE PATCH: patchPreferencesFor3pCookies() pre-writes
//       cookie-allow exceptions for all major meeting platforms into the
//       profile's Default/Preferences before first launch. This is THE
//       single most important fix for getting Zoom (and similar platforms)
//       to load reliably from a fresh profile — Chrome's default-block-3p-
//       cookies setting (since v123) breaks their cross-domain auth flows.
//   (2) OPTIONAL WARM-UP NAVIGATION: on first launch (when the profile dir
//       is fresh) we run a 3-phase warmup — ordinary browsing → cloaktest-
//       equivalent stealth probes (with inline ✅PASS/❌FAIL summaries) →
//       meeting platform homepages. After the first launch the profile has
//       Cache state and warmup is skipped. Set WARMUP=0 to disable.
//
// Environment variables:
//   TARGET_URL       URL to navigate to (default: bot.sannysoft.com)
//   PROFILE_ID       Reuse a named profile dir (e.g. 'vp-prod' →
//                    /data/profiles/vp-prod). Omit = fresh per-run UUID.
//   FINGERPRINT_SEED Pin --fingerprint=<n> for stable identity across runs.
//                    Omit = random per-run seed (matches Manager defaults).
//   USE_FAKE_MEDIA   "0" to DISABLE --use-fake-{ui,device}-for-media-stream.
//                    Default ON (needed for meeting platforms in containers).
//   WARMUP           "0" to skip the 3-phase warmup on a fresh profile.
//                    Default "1" (run on fresh profiles, skip on existing).
//                    Cookie-exception prefs are ALWAYS written regardless.
//   DATA_DIR         Root for profile dirs (default: /data).
// =============================================================================

import { launchPersistentContext } from 'cloakbrowser';
import {
  prepareProfile,
  patchPreferencesFor3pCookies,
  profileIsFresh,
  warmupNavigation,
} from './lib/profile.mjs';

const TARGET_URL       = process.env.TARGET_URL || 'https://bot.sannysoft.com/';
const PROFILE_ID       = process.env.PROFILE_ID || undefined;
const FINGERPRINT_SEED = process.env.FINGERPRINT_SEED
  ? parseInt(process.env.FINGERPRINT_SEED, 10)
  : undefined;
const USE_FAKE_MEDIA   = process.env.USE_FAKE_MEDIA !== '0';
const DO_WARMUP        = process.env.WARMUP !== '0';
const DATA_DIR         = process.env.DATA_DIR || '/data';
const VIEWPORT_WIDTH   = parseInt(process.env.VIEWPORT_WIDTH  || '1920', 10);
const VIEWPORT_HEIGHT  = parseInt(process.env.VIEWPORT_HEIGHT || '1080', 10);

async function main() {
  // Mint or resolve the profile. prepareProfile() does everything the
  // Manager does in `_init_profile_defaults` + the stale-lock cleanup +
  // the launch-arg construction.
  const { profile, profileId, userDataDir, args } = prepareProfile({
    dataDir:         DATA_DIR,
    profileId:       PROFILE_ID,        // undefined = mint a fresh UUID
    fingerprintSeed: FINGERPRINT_SEED,  // undefined = random per-run
    useFakeMedia:    USE_FAKE_MEDIA,
    screenWidth:     VIEWPORT_WIDTH,
    screenHeight:    VIEWPORT_HEIGHT,
  });

  // ---------------------------------------------------------------------
  // FIX #1: write 3p-cookie exceptions BEFORE Chromium starts. Chromium
  // serializes its own copy of Preferences on shutdown; in-session edits
  // would be clobbered. We must write before first launch — this is the
  // single most important fix for getting Zoom to load on a fresh profile.
  // ---------------------------------------------------------------------
  const isFresh = profileIsFresh(userDataDir);
  const cookiePatchedCount = patchPreferencesFor3pCookies(userDataDir);
  console.log(`[playwright]   3p-cookie patches written = ${cookiePatchedCount} site patterns`);
  console.log(`[playwright]   profile freshness         = ${isFresh ? 'fresh (warmup will run)' : 'existing (skipping warmup)'}`);

  console.log('[playwright] Launching CloakBrowser persistent context (headed)…');
  console.log(`[playwright]   profileId         = ${profileId}`);
  console.log(`[playwright]   userDataDir       = ${userDataDir}`);
  console.log(`[playwright]   fingerprint seed  = ${profile.fingerprintSeed}`);
  console.log(`[playwright]   viewport          = ${VIEWPORT_WIDTH}x${VIEWPORT_HEIGHT}`);
  console.log(`[playwright]   useFakeMedia      = ${USE_FAKE_MEDIA}`);
  console.log(`[playwright]   warmup enabled    = ${DO_WARMUP}`);

  const context = await launchPersistentContext({
    userDataDir,
    headless: profile.headless,
    humanize: profile.humanize,
    humanPreset: profile.humanPreset,
    // Match the Manager's chrome-bar offset: real Chrome on a 1080p Windows
    // desktop has ~133px of OS+browser chrome (taskbar 48 + tab strip 33 +
    // address bar 36 + bookmark bar 16). The viewport reported to the page
    // should be screen height minus that.
    viewport: { width: VIEWPORT_WIDTH, height: VIEWPORT_HEIGHT - 133 },
    args,
  });

  // -------------------------------------------------------------------
  // FIX #2: 3-phase warmup. Run on a fresh profile to populate
  // cookies/storage/SW state and verify stealth probes pass before we
  // attempt the real target. Skipped on existing profiles (idempotent
  // via profileIsFresh check) and when WARMUP=0.
  // -------------------------------------------------------------------
  if (isFresh && DO_WARMUP) {
    console.log('[playwright] Profile is fresh — running 3-phase warmup…');
    try {
      // Use a scratch page so the main page below keeps a clean history.
      // Cookie/SW/localStorage state is profile-wide and carries over.
      await warmupNavigation(() => context.newPage(), {
        log: (m) => console.log(`[playwright] ${m}`),
      });
    } catch (err) {
      console.error('[playwright] Warmup error (continuing anyway):', err);
    }
  } else if (!isFresh) {
    console.log('[playwright] Skipping warmup — profile already has Cache state.');
  } else {
    console.log('[playwright] Skipping warmup — WARMUP=0.');
  }

  // launchPersistentContext returns the BrowserContext directly. It always
  // opens with at least one page (about:blank); reuse it instead of opening
  // a second tab so the URL bar shows our navigation immediately when
  // watching over VNC.
  const page = context.pages()[0] ?? await context.newPage();

  console.log(`[playwright] Navigating to: ${TARGET_URL}`);
  await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 60_000 });

  console.log(`[playwright] Page title: ${await page.title()}`);

  // Quick stealth fingerprint dump so we can confirm the C++ patches are
  // live without needing to read the screen over VNC.
  const fingerprint = await page.evaluate(() => ({
    userAgent: navigator.userAgent,
    webdriver: navigator.webdriver,
    pluginsLength: navigator.plugins?.length ?? 0,
    chromeObject: typeof window.chrome,
    languages: navigator.languages,
    platform: navigator.platform,
    hardwareConcurrency: navigator.hardwareConcurrency,
    deviceMemory: navigator.deviceMemory,
    vendor: navigator.vendor,
  }));
  console.log('[playwright] Fingerprint snapshot:');
  console.log(JSON.stringify(fingerprint, null, 2));

  console.log('[playwright] ✅ Browser is up. Connect via VNC to inspect.');
  console.log('[playwright]    noVNC (in browser): http://localhost:5901/vnc.html');
  console.log('[playwright]    raw VNC viewer:     vnc://localhost:5900');
  console.log('[playwright]    (replace "localhost" with the docker host IP if running remotely,');
  console.log('[playwright]     or SSH-tunnel: ssh -L 5901:localhost:5901 -L 5900:localhost:5900 user@host)');
  console.log(`[playwright] To reuse this exact profile next run: -e PROFILE_ID=${profileId}`);
  console.log('[playwright] Press Ctrl+C / docker stop to exit.');

  // Graceful shutdown so the browser doesn't leave zombie Singleton* locks
  // in the profile dir.
  const shutdown = async (signal) => {
    console.log(`[playwright] Received ${signal}, closing context…`);
    try {
      await context.close();
    } catch (err) {
      console.error('[playwright] Error during close:', err);
    }
    process.exit(0);
  };
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  // Park forever — actual lifetime is controlled by the container.
  await new Promise(() => {});
}

main().catch((err) => {
  console.error('[playwright] FATAL:', err);
  process.exit(1);
});
