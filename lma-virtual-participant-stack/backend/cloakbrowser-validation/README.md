# CloakBrowser Validation Harness

Standalone Docker image that brings up the
[CloakBrowser](https://github.com/CloakHQ/CloakBrowser) stealth Chromium
binary inside a virtual display, exposes it over VNC + noVNC, and drives a
single page through one of four backends so we can eyeball end-to-end
behavior before swapping LMA's production virtual participant browser.

This is a **read-only experiment** — no production code is modified.
Everything lives under
`lma-virtual-participant-stack/backend/cloakbrowser-validation/`.

> **TL;DR — `playwright`, `puppeteer`, and `manager` all pass Zoom from a
> fresh datacenter profile** as of the latest revision. The two fixes that
> made it work for ALL of them are: (1) pre-writing third-party-cookie
> exceptions for meeting platforms into `Default/Preferences` before first
> launch, and (2) an optional 3-phase warmup (ordinary sites → cloaktest
> stealth probes → meeting-platform homepages) the first time a profile is
> used. The production VP will use `puppeteer` mode since the existing
> backend is already Puppeteer-based; see "Production port" at the bottom.

## Root cause we discovered

We A/B-tested four launch paths against the same Zoom URL on the same EC2
host. Initial results (before discovering the cookie-pref fix):

| MODE | Backend | Initial first-run Zoom result | After fix |
|---|---|---|---|
| `playwright` | JS — `cloakbrowser` npm `launchPersistentContext` | ❌ flagged | ✅ passes |
| `puppeteer`  | JS — `cloakbrowser/puppeteer` `launchPersistentContext` | ❌ flagged | ✅ passes |
| `python`     | Python — `cloakbrowser` pip `launch_persistent_context_async()` | ❌ flagged | (not retested) |
| `manager`    | Python — Manager's actual `BrowserManager.launch()` (cloned source) | ❌ flagged | ✅ passes |

**All four originally failed Zoom from a fresh profile**, which ruled out:

- ✗ Playwright vs Puppeteer JS wrapper bugs
- ✗ JS vs Python wrapper bugs
- ✗ Anything in CloakBrowser-Manager's launch glue that we missed when porting

…leaving only profile state. We snapshotted the working Manager profile
(`fb5de49e-d16b-4d9c-aed6-a58cb94b7b3d`) which had been in interactive use
and diff'd its `Default/Preferences` against a freshly-created Manager
profile that *also* failed Zoom. The diff was a single block of entries:

```json
"profile": {
  "content_settings": {
    "exceptions": {
      "cookies": {
        "*,https://[*.]zoom.us":   { "setting": 1, "lifetime": "7776000000000", ... },
        "*,https://[*.]zoom.com":  { "setting": 1, ... },
        ...
      }
    }
  }
}
```

These are **third-party cookie exceptions** — exactly what Chrome writes
when a user clicks the URL bar shield → "Allow third-party cookies for this
site" on `app.zoom.us`. Chrome 146's default-block-third-party-cookies
behaviour (rolled out in v123, early 2025) breaks Zoom's cross-domain auth
flow; without the exception, Zoom's "is this a real browser?" check fails
and the join page silently bot-flags the session.

**The fix:** pre-write the exception entries into `Default/Preferences`
*before first launch*. All four MODEs now do this — `validate-manager.py`'s
`_patch_preferences_for_3p_cookies()` and the JS equivalent
`patchPreferencesFor3pCookies()` in `lib/profile.mjs`.

### Two Puppeteer-specific quirks (only in `MODE=puppeteer`)

While wiring the fix into `validate-puppeteer.mjs` we found two more issues
that don't affect the other modes. Both are documented in detail inline in
that file; the short version:

1. **`cloakbrowser/puppeteer` exports two launch functions** — `launch()`
   and `launchPersistentContext()`. **Only the latter forwards
   `userDataDir`** to puppeteer-core. Use `launch()` and Puppeteer silently
   creates a temp profile at `/tmp/puppeteer_dev_chrome_profile-XXXXX` —
   so the cookie-pref exceptions we wrote into our profile dir are never
   loaded. Symptom: argv shows `--user-data-dir=/tmp/puppeteer_dev_…`
   instead of `/data/profiles/<UUID>`. Fix: import
   `launchPersistentContext` from `cloakbrowser/puppeteer`.

2. **Top-level `defaultViewport: null` is silently dropped.**
   `cloakbrowser/puppeteer.launchPersistentContext()` only forwards
   `userDataDir` (not `defaultViewport`) explicitly to puppeteer-core,
   so a top-level `defaultViewport: null` doesn't take effect — puppeteer-
   core falls back to its hardcoded `{ width: 800, height: 600 }` default
   and applies CDP `Emulation.setDeviceMetricsOverride` on every new page,
   pegging `window.innerWidth` at 800 even though the OS window is 1920.
   Symptom: pages render in a narrow / mobile-ish layout with empty space
   on the right. Fix: pass it via `launchOptions` so it lands in the
   `...options.launchOptions` spread inside the wrapper:

   ```javascript
   const browser = await launchPersistentContext({
     userDataDir,
     args: [`--window-size=${W},${H}`, '--window-position=0,0', ...args],
     launchOptions: {
       defaultViewport: null,   // disables Emulation.setDeviceMetricsOverride
     },
   });
   ```

   The `--window-size` flag in `args` is also Puppeteer-only — Playwright's
   `launchPersistentContext({ viewport })` resizes the OS window directly
   via CDP `Browser.setWindowBounds`, but Puppeteer doesn't, so we have to
   pass the size flag explicitly.

## File layout

```
cloakbrowser-validation/
├── Dockerfile                  # python:3.12-slim + Node 22 + cloakbrowser pip + cloakbrowser-manager clone + Xvfb/x11vnc/noVNC, VOLUME /data
├── entrypoint.sh               # Brings up display stack, dispatches to MODE
├── package.json                # cloakbrowser, playwright-core, puppeteer-core (for the JS modes)
├── lib/
│   └── profile.mjs             # JS port of the Manager's profile-launch glue + the cookie-exception patcher + warmup helper
├── validate-playwright.mjs     # MODE=playwright (default) — JS launchPersistentContext + cookie-pref + warmup
├── validate-puppeteer.mjs      # MODE=puppeteer            — JS launchPersistentContext (NOT launch — see quirks) + cookie-pref + warmup
├── validate-python.py          # MODE=python               — Python launch_persistent_context_async()
├── validate-manager.py         # MODE=manager              — imports the Manager's real BrowserManager class + applies the cookie-pref fix + 3-phase warmup. The original reference implementation.
└── README.md                   # this file
```

The Dockerfile clones `cloakhq/CloakBrowser-Manager` at tag `v0.0.10` to
`/opt/cloakbrowser-manager` so `MODE=manager` can `import backend.browser_manager`
directly (bypassing the Manager's FastAPI server / SQLite / KasmVNC layer
and going straight to its `launch()` method).

## Build

```bash
cd lma-virtual-participant-stack/backend/cloakbrowser-validation
docker build -t lma-cloakbrowser-validation .
```

The build pre-downloads the CloakBrowser binary (~200 MB, Chromium
146.0.7680.177.3) via `python -m cloakbrowser install`, plus clones the
Manager source and installs its requirements.

## Run

```bash
docker rm -f cloak-validate 2>/dev/null
# Stop any other consumer of /data (e.g. a running Manager image)
docker stop $(docker ps --filter ancestor=cloakhq/cloakbrowser-manager -q) 2>/dev/null

# Default: MODE=playwright (fastest startup). Use MODE=puppeteer to mirror
# what the production VP will look like once we port. All three modes
# (playwright, puppeteer, manager) apply the cookie-pref fix and warmup.
docker run --rm -it --name cloak-validate \
  -p 5900:5900 -p 5901:5901 --shm-size=1g \
  -v cloakprofiles:/data \
  -e MODE=puppeteer \
  -e TARGET_URL=https://zoom.us/wc/<MEETING_ID>/join \
  lma-cloakbrowser-validation
```

What you'll see in the log:

```
=== CloakBrowser Validation Startup ===
  MODE         = manager
  TARGET_URL   = https://zoom.us/wc/<...>/join
[manager-direct] Profile dict: ... (fresh UUID, fingerprint_seed=…, humanize=True, etc.)
[manager-direct] Profile freshness: fresh (warmup will run)
[manager-direct] Patched 3p-cookie exceptions for 10 sites into …/Default/Preferences
[manager-direct] Calling BrowserManager.launch(profile)…
[manager-direct] Launched: cdp_port=5100 display=:99
[warmup] (ordinary)   https://www.google.com/
[warmup] (ordinary)   https://news.ycombinator.com/
[warmup] (ordinary)   https://en.wikipedia.org/wiki/Special:Random
[warmup] === stealth probe results ===
[warmup] (probe)      bot.sannysoft.com (https://bot.sannysoft.com/)
[warmup]              ✅ PASS  57 checks; failures: none
[warmup] (probe)      browserscan.net (https://www.browserscan.net/bot-detection)
[warmup]              ✅ PASS  normal=19 abnormal=0
[warmup] (probe)      deviceandbrowserinfo.com (https://deviceandbrowserinfo.com/are_you_a_bot)
[warmup]              ✅ PASS  isBot=False
[warmup] (probe)      FingerprintJS demo (https://demo.fingerprint.com/web-scraping)
[warmup]              ✅ PASS  not blocked
[warmup] (meeting)    https://zoom.us/
[warmup] (meeting)    https://app.chime.aws/
[warmup] (meeting)    https://teams.microsoft.com/
[warmup] (meeting)    https://web.webex.com/
[warmup] complete (11 sites visited across 3 phases)
[manager-direct] Navigating to: https://zoom.us/wc/<...>/join
[manager-direct] Page title: Zoom meeting on web
[manager-direct] Fingerprint snapshot:
{
  "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) … Chrome/146.0.0.0 Safari/537.36",
  "webdriver": false,
  "pluginsLength": 5,
  "chromeObject": "object",
  …
}
[manager-direct] ✅ Browser is up via Manager's BrowserManager.launch().
```

`Page title: Zoom meeting on web` is what you want — it's Zoom's actual join
page. If detection had triggered, the page would say something like "join
from your browser is not supported".

## Connect via VNC

| Method | URL |
|---|---|
| noVNC in any browser | <http://localhost:5901/vnc.html> |
| Native VNC viewer    | `vnc://localhost:5900` |

Running on a remote VM? Forward the ports:

```bash
ssh -L 5901:localhost:5901 -L 5900:localhost:5900 user@your-vm
# then open http://localhost:5901/vnc.html on your laptop
```

## Environment variables

| Var                | Default                  | Purpose |
|--------------------|--------------------------|---------|
| `MODE`             | `playwright`             | One of `playwright`, `puppeteer`, `python`, `manager`. The production VP will use `puppeteer` (matches the existing backend's library). |
| `TARGET_URL`       | `https://bot.sannysoft.com/` | URL the script navigates to after launch (and after warmup if applicable) |
| `PROFILE_ID`       | random UUID              | Reuse a named profile dir (e.g. `vp-prod` → `/data/profiles/vp-prod`). Omit to mint a fresh UUID per run. |
| `WARMUP`           | `1`                      | `0` to skip the 3-phase warm-up. Implemented in playwright/puppeteer/manager modes. Cookie-exception prefs are written regardless — they're the actual fix. |
| `USE_FAKE_MEDIA`   | `1`                      | `0` to drop `--use-fake-{ui,device}-for-media-stream`. Containers have no real cam/mic so meeting platforms refuse to join without these. |
| `FINGERPRINT_SEED` | random per launch        | Pin `--fingerprint=<n>` for stable identity across runs |
| `DATA_DIR`         | `/data`                  | Root for profile dirs (must be inside the volume mount) |
| `AUTO_UPDATE`      | `0`                      | `1` to `pip install -U cloakbrowser` + `python -m cloakbrowser update` at container start (~30 s overhead, gives you the latest stealth Chromium without rebuilding the image) |

> `--shm-size=1g` matters: Chromium crashes on the default 64 MB `/dev/shm`.
>
> `-v cloakprofiles:/data` is recommended so profile state (cookies,
> localStorage, cache, fingerprint-derived files) persists across runs.

## Iteration tips

### Rebuild + relaunch (one-liner)

```bash
docker rm -f cloak-validate 2>/dev/null; \
docker build -t lma-cloakbrowser-validation \
  lma-virtual-participant-stack/backend/cloakbrowser-validation && \
docker run --rm -it --name cloak-validate \
  -p 5900:5900 -p 5901:5901 --shm-size=1g \
  -v cloakprofiles:/data \
  -e MODE=manager \
  lma-cloakbrowser-validation
```

### No-rebuild iteration on Python/JS scripts

Bind-mount the script files as read-only volumes — saves a 20-second image
rebuild per change:

```bash
docker rm -f cloak-validate 2>/dev/null; \
docker run --rm -it --name cloak-validate \
  -p 5900:5900 -p 5901:5901 --shm-size=1g \
  -v cloakprofiles:/data \
  -v "$PWD/lma-virtual-participant-stack/backend/cloakbrowser-validation/validate-manager.py:/app/validate-manager.py:ro" \
  -e MODE=manager \
  lma-cloakbrowser-validation
```

### Wipe profile state for a clean test

```bash
docker volume rm cloakprofiles
```

### Borrow a profile created in the cloakhq/cloakbrowser-manager image

If you also run the upstream Manager on the same volume, you can launch any
of its profiles with our harness:

```bash
docker run --rm -v cloakprofiles:/data alpine ls /data/profiles/   # find IDs
# stop Manager first — Chrome locks the dir
docker stop $(docker ps --filter ancestor=cloakhq/cloakbrowser-manager -q)

docker run --rm -it -p 5900:5900 -p 5901:5901 --shm-size=1g \
  -v cloakprofiles:/data \
  -e MODE=manager \
  -e PROFILE_ID=<UUID-from-listing-above> \
  lma-cloakbrowser-validation
```

## Profile model & the third-party-cookie fix

`validate-manager.py` (and `lib/profile.mjs` for the JS modes) implements
the per-profile setup the Manager's UI does on Create + Launch, plus the two
production-required additions:

1. **`profile.content_settings.exceptions.cookies` patch.** Pre-writes
   `setting: 1` (allow) entries with 90-day lifetime for the meeting
   platforms below into `<userDataDir>/Default/Preferences`. The same JSON
   shape Chrome's URL-bar "Allow third-party cookies" toggle writes —
   verified via diff against a manual toggle in the Manager UI. **This is
   the actual fix.**

   ```
   zoom.us, zoom.com, chime.aws, teams.microsoft.com, office.com,
   live.com, webex.com, meet.google.com, google.com, googleusercontent.com
   ```

2. **3-phase warm-up navigation.** Runs only on first launch (detected via
   absence of `Default/Cache`). Visits in order:

   - **Ordinary user sites:** Google, Hacker News, Wikipedia random
     article. Plants 1p cookies and makes the profile look like a real
     person browsed before joining a meeting.
   - **Stealth probes (cloaktest-equivalent):** `bot.sannysoft.com`,
     `browserscan.net/bot-detection`, `deviceandbrowserinfo.com`,
     `demo.fingerprint.com`. Same URL set the upstream
     `docker run cloakhq/cloakbrowser cloaktest` command checks. Each
     prints `✅ PASS` / `❌ FAIL` inline so you can see at a glance
     whether the stealth patches are doing their job before you attempt the
     real `TARGET_URL`. Browsing history naturally accumulates — a
     stealth-research-flavored profile.
   - **Meeting platforms:** zoom.us, app.chime.aws, teams.microsoft.com,
     web.webex.com. 1p cookies + Service Worker scripts pre-resident
     before the actual meeting URL is opened.

   Set `WARMUP=0` to skip phases 1-3 (cookie pref still applied).

The Manager's own `_init_profile_defaults()` (initial `Bookmarks` JSON +
DuckDuckGo as default search) runs before our patcher — we leave its
files alone and just merge our cookie exceptions into the same
`Preferences` file.

## When to use which MODE

| Situation | MODE | Why |
|---|---|---|
| Modeling the production VP code path | `puppeteer` | Production VP backend uses `rebrowser-puppeteer` — `cloakbrowser/puppeteer` is the closest API surface to that. Includes the Puppeteer-specific quirk fixes. |
| Quick stealth fingerprint sanity check | `playwright` | Fastest startup; bot.sannysoft.com etc. all pass. CloakBrowser's docs note Playwright leaks fewer automation signals than Puppeteer for reCAPTCHA Enterprise / CF Turnstile sites. |
| Debugging a JS-vs-Python launch difference | `python` vs `manager` | Same Python launch path, with vs without Manager's glue around it. Isolates "Manager-glue" bugs. |
| Verifying upstream Manager regressions before pinning | `manager` (with `AUTO_UPDATE=1`) | Pulls latest cloakbrowser pip + Chromium binary at container start. |

## Troubleshooting

### "Zoom says join from your browser is not supported"

You're hitting the third-party-cookie issue. All four modes apply the
cookie-pref patch, but it only takes effect on a **fresh** profile dir —
if you reused an existing `PROFILE_ID` that was already launched without
the patch, Chrome's serialized `Default/Preferences` from the previous run
overrode our patch on shutdown. Wipe the profile (`docker volume rm
cloakprofiles`) or use a different `PROFILE_ID` and try again.

If you're on `MODE=puppeteer` specifically, double-check that the script
imports `launchPersistentContext` (not `launch`) from `cloakbrowser/puppeteer`
and that `defaultViewport: null` is inside `launchOptions`, not at the top
level — see "Two Puppeteer-specific quirks" near the top of this README.

### "Your browser is preventing access to your microphone"

`USE_FAKE_MEDIA=0` was set explicitly. Default is `1`; containers have no
real cam/mic and meeting platforms reject `getUserMedia()` rejection.

### Stealth probes show ❌ FAIL on the warmup

Inspect the failure summary line — it usually points to a specific check
(e.g. browserscan.net abnormal count > 0) which means a fingerprint patch
isn't loading. Most often that's a Chromium/cloakbrowser version mismatch;
try `AUTO_UPDATE=1` to upgrade to latest at container start, OR pin a
different `CLOAKBROWSER_VERSION` build arg in the Dockerfile.

### Container exits with `Event loop is closed` traceback after Ctrl+C

Cosmetic only — Playwright's subprocess transport tries to `write_eof` on
an already-closed asyncio loop during shutdown. Doesn't affect the run.

## Production port — what's next

Now that the validation harness proves the fix works end-to-end, the next
task is porting it into the production VP backend at
`lma-virtual-participant-stack/backend/`:

1. **Switch the Dockerfile from `node:22-alpine3.21` to a glibc base** —
   CloakBrowser's binary is glibc-only, doesn't run on Alpine/musl.
2. **Install `cloakbrowser` (pip + npm) and pre-download the Chromium
   binary at build time**, same approach as this Dockerfile.
3. **Replace the `rebrowser-puppeteer` call site** at
   `src/index.ts:271` (and adjust `getPuppeteerConfig` at lines 31-66) to
   use `cloakbrowser/puppeteer`'s `launchPersistentContext`. **Important**:
   it must be `launchPersistentContext`, NOT `launch` — see the Puppeteer
   quirks section above. Also pass `launchOptions: { defaultViewport: null }`
   (top-level `defaultViewport: null` is silently dropped by the wrapper).
   The per-platform join logic (`chime.ts` / `webex.ts` / `teams.ts` /
   `zoom.ts`) probably doesn't need changes — they consume the page handle
   returned from the launch call.
4. **Lift `patchPreferencesFor3pCookies()`** from `lib/profile.mjs` and call
   it from the production launch path, after the profile dir is resolved
   (e.g. via `acquireProfile()`) but before `launchPersistentContext()`.
5. **Add an opt-in warm-up call** for cold-start profiles. Most production
   joins will reuse a long-lived per-VP-task profile so warmup runs once
   per task lifetime; for ephemeral meetings it can run per-join. Reuse
   `warmupNavigation()` from `lib/profile.mjs`.
6. **Keep the existing PulseAudio routing** in the production
   `entrypoint.sh` (this validation harness intentionally omits it — we
   only need video over VNC for the bot-detection question). Consider
   also adopting the fluxbox-no-toolbar trick from this harness's
   entrypoint (`session.screen0.toolbar.visible: false` in
   `~/.fluxbox/init`); it's cleaner than the production `Xvfb=1920x1120
   + --window-size=W,H+80` workaround for the same problem.
