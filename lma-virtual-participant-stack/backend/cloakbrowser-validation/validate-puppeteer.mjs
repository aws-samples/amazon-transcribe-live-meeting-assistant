// =============================================================================
// CloakBrowser validation — Puppeteer path
// -----------------------------------------------------------------------------
// Same launch behavior as validate-playwright.mjs but goes through the
// Puppeteer subpath that CloakBrowser exposes.
//
// Why ship both? The existing virtual participant backend uses
// `puppeteer-extra` + the stealth plugin. CloakBrowser's own README warns
// that Puppeteer's CDP traffic leaks more automation signals than
// Playwright (intermittent 403s on reCAPTCHA Enterprise sites), so we keep
// both around to A/B test against the meeting platforms LMA cares about
// (Chime / Webex / Teams / Zoom).
//
// Both scripts share `lib/profile.mjs` for everything that's not
// library-specific. See validate-playwright.mjs for full rationale on the
// flag set and profile-directory model — they're identical.
//
// Puppeteer-specific notes:
//   • cloakbrowser/puppeteer DOES expose a separate `launchPersistentContext`
//     (despite Puppeteer-core not having one natively). Always use it for
//     persistent-profile use cases — see the import comment below for why.
//   • Default viewport: 800x600 if you don't set defaultViewport, which is
//     a bot signal in itself. We always set it explicitly.
//
// What this script ALSO does (port of validate-manager.py's two key fixes):
//   (1) THIRD-PARTY-COOKIE PATCH: pre-write cookie-allow exceptions for all
//       major meeting platforms before launch. Single most important fix
//       for getting Zoom to load reliably on a fresh profile.
//   (2) OPTIONAL WARM-UP NAVIGATION: 3-phase warmup on a fresh profile
//       — ordinary browsing → cloaktest stealth probes (PASS/FAIL) →
//       meeting platform homepages. Skipped on existing profiles.
//
// Environment variables: same as validate-playwright.mjs
//   TARGET_URL, PROFILE_ID, FINGERPRINT_SEED, USE_FAKE_MEDIA, WARMUP, DATA_DIR
// =============================================================================

// IMPORTANT: cloakbrowser/puppeteer exports two launch functions:
//   - `launch()`                    — does NOT pass userDataDir to
//                                     puppeteer-core. Puppeteer falls back
//                                     to creating a /tmp/puppeteer_dev_-
//                                     chrome_profile-XXXXX temp dir, which
//                                     means our pre-written 3p-cookie
//                                     exceptions never take effect.
//   - `launchPersistentContext()`   — explicitly passes userDataDir through.
// Always use launchPersistentContext for the VP use case. Confirmed by
// reading dist/puppeteer.js and inspecting /proc/<chrome>/cmdline.
import { launchPersistentContext } from 'cloakbrowser/puppeteer';
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
  const { profile, profileId, userDataDir, args } = prepareProfile({
    dataDir:         DATA_DIR,
    profileId:       PROFILE_ID,
    fingerprintSeed: FINGERPRINT_SEED,
    useFakeMedia:    USE_FAKE_MEDIA,
    screenWidth:     VIEWPORT_WIDTH,
    screenHeight:    VIEWPORT_HEIGHT,
  });

  // ---------------------------------------------------------------------
  // FIX #1: write 3p-cookie exceptions BEFORE Chromium starts. See
  // validate-playwright.mjs for the full rationale — same logic.
  // ---------------------------------------------------------------------
  const isFresh = profileIsFresh(userDataDir);
  const cookiePatchedCount = patchPreferencesFor3pCookies(userDataDir);
  console.log(`[puppeteer]   3p-cookie patches written = ${cookiePatchedCount} site patterns`);
  console.log(`[puppeteer]   profile freshness         = ${isFresh ? 'fresh (warmup will run)' : 'existing (skipping warmup)'}`);

  console.log('[puppeteer] Launching CloakBrowser persistent profile (headed)…');
  console.log(`[puppeteer]   profileId         = ${profileId}`);
  console.log(`[puppeteer]   userDataDir       = ${userDataDir}`);
  console.log(`[puppeteer]   fingerprint seed  = ${profile.fingerprintSeed}`);
  console.log(`[puppeteer]   viewport          = ${VIEWPORT_WIDTH}x${VIEWPORT_HEIGHT}`);
  console.log(`[puppeteer]   useFakeMedia      = ${USE_FAKE_MEDIA}`);
  console.log(`[puppeteer]   warmup enabled    = ${DO_WARMUP}`);

  // -------------------------------------------------------------------
  // PUPPETEER WINDOW-SIZING QUIRK: `defaultViewport` only changes the
  // page's reported viewport via CDP Emulation.setDeviceMetricsOverride —
  // it does NOT resize the OS-level Chromium window. So without a
  // window-size flag the OS window opens at Chrome's default ~1024x768
  // and floats in the corner of our 1920x1080 Xvfb display, leaving a
  // black void. Playwright's launchPersistentContext({ viewport }) DOES
  // resize the OS window via CDP Browser.setWindowBounds, which is why
  // the Playwright variant fills the screen and Puppeteer doesn't.
  //
  // Fix: pass --window-size + --window-position so the OS window opens
  // exactly the size of the Xvfb screen (entrypoint.sh disables fluxbox's
  // bottom toolbar so we have all VIEWPORT_HEIGHT vertical pixels for
  // Chrome — no need to reserve room for the toolbar like the production
  // VP does in its index.ts).
  //
  // We deliberately do NOT pass --start-maximized: it interacts with
  // --window-size in fluxbox in unpredictable ways (sometimes maximizing
  // BEFORE the explicit size is applied, sometimes after, leaving you with
  // either no maximize or a window taller than the screen).
  // -------------------------------------------------------------------
  const argsWithWindow = [
    `--window-size=${VIEWPORT_WIDTH},${VIEWPORT_HEIGHT}`,
    '--window-position=0,0',
    ...args,
  ];

  const browser = await launchPersistentContext({
    headless: profile.headless,
    humanize: profile.humanize,
    humanPreset: profile.humanPreset,
    userDataDir,
    args: argsWithWindow,
    // PUPPETEER QUIRK #2: defaultViewport must go inside `launchOptions`,
    // not at the top level. cloakbrowser/puppeteer's launchPersistentContext
    // does `await puppeteer.launch({ ...options.launchOptions, executablePath,
    // headless, args, ignoreDefaultArgs, userDataDir })` — note that it
    // explicitly forwards userDataDir but NOT defaultViewport. Top-level
    // `defaultViewport: null` is silently dropped, so puppeteer-core falls
    // back to its hardcoded { width: 800, height: 600 } default and
    // applies CDP Emulation.setDeviceMetricsOverride on every new page,
    // pegging window.innerWidth at 800 even though the OS window is 1920.
    // (Verified empirically by querying the page over CDP.)
    //
    // The fix: pass it through launchOptions so it ends up in the spread
    // and reaches puppeteer-core. Setting it to null disables the override
    // entirely; window.innerWidth then tracks the real OS window from
    // --window-size=1920,1080.
    launchOptions: {
      defaultViewport: null,
    },
  });

  // -------------------------------------------------------------------
  // FIX #2: 3-phase warmup. Run on a fresh profile to populate
  // cookies/storage/SW state and verify stealth probes pass before
  // attempting the real target. Same logic as the Playwright variant.
  // -------------------------------------------------------------------
  if (isFresh && DO_WARMUP) {
    console.log('[puppeteer] Profile is fresh — running 3-phase warmup…');
    try {
      // Open a scratch page on Puppeteer's `browser` (not `context`).
      await warmupNavigation(() => browser.newPage(), {
        log: (m) => console.log(`[puppeteer] ${m}`),
      });
    } catch (err) {
      console.error('[puppeteer] Warmup error (continuing anyway):', err);
    }
  } else if (!isFresh) {
    console.log('[puppeteer] Skipping warmup — profile already has Cache state.');
  } else {
    console.log('[puppeteer] Skipping warmup — WARMUP=0.');
  }

  // Reuse the initial about:blank tab so the URL bar shows our navigation
  // immediately when watching over VNC.
  const pages = await browser.pages();
  const page = pages[0] ?? (await browser.newPage());

  console.log(`[puppeteer] Navigating to: ${TARGET_URL}`);
  await page.goto(TARGET_URL, { waitUntil: 'domcontentloaded', timeout: 60_000 });

  console.log(`[puppeteer] Page title: ${await page.title()}`);

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
  console.log('[puppeteer] Fingerprint snapshot:');
  console.log(JSON.stringify(fingerprint, null, 2));

  console.log('[puppeteer] ✅ Browser is up. Connect via VNC to inspect.');
  console.log('[puppeteer]    noVNC (in browser): http://localhost:5901/vnc.html');
  console.log('[puppeteer]    raw VNC viewer:     vnc://localhost:5900');
  console.log(`[puppeteer] To reuse this exact profile next run: -e PROFILE_ID=${profileId}`);
  console.log('[puppeteer] Press Ctrl+C / docker stop to exit.');

  const shutdown = async (signal) => {
    console.log(`[puppeteer] Received ${signal}, closing browser…`);
    try {
      await browser.close();
    } catch (err) {
      console.error('[puppeteer] Error during close:', err);
    }
    process.exit(0);
  };
  process.on('SIGINT',  () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));

  await new Promise(() => {});
}

main().catch((err) => {
  console.error('[puppeteer] FATAL:', err);
  process.exit(1);
});
