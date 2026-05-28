#!/usr/bin/env python3
"""
CloakBrowser validation — Manager-direct path with our two production-required
additions on top of what the Manager does:

  (1) THIRD-PARTY COOKIE EXCEPTIONS for meeting platforms, pre-written into the
      profile's Default/Preferences before first launch. This is the single
      most important fix we discovered: Chrome blocks 3p cookies by default
      (since v123), and meeting platforms like Zoom rely on cross-domain auth
      cookies to recognize the browser as legitimate. Without these exceptions
      a brand-new profile fails Zoom; with them it passes.

      The exact pref shape we mimic was reverse-engineered by toggling
      "Allow third-party cookies" in Chrome's URL bar and snapshotting the
      Preferences file diff. See README's "Profile model" section.

  (2) OPTIONAL WARM-UP NAVIGATION: on first launch (when the profile dir is
      newly minted), visit a few benign pages — meeting platform homepages,
      a search engine — to build up cookie/IndexedDB/Service Worker state.
      Belt-and-braces; the cookie pref alone is the actual fix, but having
      *some* browsing history makes the profile look more legitimate.

The launch path itself is byte-for-byte the Manager's
`BrowserManager.launch()`. See validate-manager.py's earlier docstring for the
rationale on importing the Manager source directly vs porting it.

Environment variables (same as the other validate-* scripts):
  TARGET_URL       URL to navigate to (default: bot.sannysoft.com)
  PROFILE_ID       Reuse a named profile dir; omit = fresh per-run UUID
  FINGERPRINT_SEED Pin --fingerprint=<n>; omit = random per-run
  USE_FAKE_MEDIA   "0" to DISABLE --use-fake-{ui,device}-for-media-stream
  DATA_DIR         Root for profile dirs (default: /data)
  WARMUP           "1" (default) to visit meeting-platform homepages on
                   first launch; "0" to skip. Cookie-exception prefs are
                   ALWAYS written — they're the actual fix, not the warm-up.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import sys
import time
import uuid
from pathlib import Path

# Make the Manager source importable (cloned to /opt/cloakbrowser-manager
# during the Dockerfile build).
sys.path.insert(0, "/opt/cloakbrowser-manager")
os.environ.setdefault("DATA_DIR", os.environ.get("DATA_DIR", "/data"))

from backend.browser_manager import BrowserManager  # noqa: E402


# -----------------------------------------------------------------------------
# Cookie-exception preference patching
# -----------------------------------------------------------------------------

# Sites we want third-party cookies allowed for. Pattern follows Chrome's
# `[primary],[secondary]` content-settings convention:
#   "*"  in primary  = any top-level URL
#   "https://[*.]X"  = wildcard subdomain match for the embedded resource
# All meeting platforms here use cross-domain auth flows that get broken by
# Chrome's default 3p-cookie blocking.
THIRD_PARTY_COOKIE_ALLOW_PATTERNS = [
    "https://[*.]zoom.us",
    "https://[*.]zoom.com",          # zoom.com (legacy + branded URLs)
    "https://[*.]chime.aws",         # Amazon Chime
    "https://[*.]teams.microsoft.com",
    "https://[*.]office.com",        # Teams calls into office.com auth
    "https://[*.]live.com",          # Microsoft auth
    "https://[*.]webex.com",         # Cisco Webex
    "https://[*.]meet.google.com",   # Google Meet
    "https://[*.]google.com",        # Google auth
    "https://[*.]googleusercontent.com",  # Google CDN tokens
]

# Chrome timestamps are microseconds since 1601-01-01 (Win32 FILETIME epoch).
# Python's time.time() is seconds since 1970. Difference = 11,644,473,600 sec.
CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000

# 90 days in microseconds — matches what Chrome's UI toggle writes for
# "temporarily allow" exceptions (the browser auto-renews on each access, so
# the practical lifetime is "as long as the user keeps using the site").
EXCEPTION_LIFETIME_US = 90 * 24 * 60 * 60 * 1_000_000  # 7,776,000,000,000


def _chrome_timestamp_now_us() -> int:
    """Microseconds-since-1601 for *now*, the format Chrome writes."""
    return int(time.time() * 1_000_000) + CHROME_EPOCH_OFFSET_US


def _build_cookie_exceptions() -> dict:
    """Build the `profile.content_settings.exceptions.cookies` dict — the
    EXACT JSON shape Chrome writes when you click "Allow third-party cookies"
    in the URL bar. Verified via diff'ing a Preferences file before/after the
    UI toggle in CloakBrowser-Manager v0.0.10."""
    now = _chrome_timestamp_now_us()
    expiration = str(now + EXCEPTION_LIFETIME_US)
    last_modified = str(now)
    exceptions = {}
    for pat in THIRD_PARTY_COOKIE_ALLOW_PATTERNS:
        # The key format `*,<pattern>` means: when the top-level URL is
        # ANYTHING (`*`) and the embedded resource matches `<pattern>`, allow
        # cookies. setting=1 = ALLOW, setting=2 = BLOCK, setting=4 =
        # SESSION-ONLY.
        key = f"*,{pat}"
        exceptions[key] = {
            "setting": 1,
            "lifetime": str(EXCEPTION_LIFETIME_US),
            "expiration": expiration,
            "last_modified": last_modified,
        }
    return exceptions


def _patch_preferences_for_3p_cookies(user_data_dir: Path) -> None:
    """Merge our cookie exceptions into the profile's Default/Preferences.

    This must run AFTER Manager._init_profile_defaults() (which writes the
    initial Preferences with DuckDuckGo search) but BEFORE Chromium launches
    (Chromium reads Preferences once on startup and serializes its own copy
    on shutdown — writing during a session would be clobbered).

    Idempotent: re-running on an already-patched profile is a no-op for the
    sites we already added, and an additive update for any new sites.
    """
    prefs_path = user_data_dir / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)

    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text())
        except json.JSONDecodeError:
            # Corrupted Preferences (shouldn't happen, but be safe). Replacing
            # it loses DuckDuckGo + any other init defaults; log and rebuild.
            print(f"[manager-direct] WARN: corrupt Preferences at {prefs_path}, rebuilding", flush=True)
            prefs = {}
    else:
        prefs = {}

    # Drill down through `profile.content_settings.exceptions.cookies` and
    # merge in our patterns (preserving any existing entries the user/UI
    # might have added on previous runs).
    profile_node           = prefs.setdefault("profile", {})
    cs_node                = profile_node.setdefault("content_settings", {})
    exc_node               = cs_node.setdefault("exceptions", {})
    cookies_node           = exc_node.setdefault("cookies", {})
    cookies_node.update(_build_cookie_exceptions())

    prefs_path.write_text(json.dumps(prefs, indent=2))
    print(f"[manager-direct] Patched 3p-cookie exceptions for {len(THIRD_PARTY_COOKIE_ALLOW_PATTERNS)} sites into {prefs_path}", flush=True)


# -----------------------------------------------------------------------------
# Optional profile warm-up
# -----------------------------------------------------------------------------

# Warmup hits TWO classes of URLs in order:
#
#   1. ORDINARY-USER sites (Google, Wikipedia, news) — make the profile look
#      like a real person browsed before joining a meeting. Cheap and the
#      sites all set 1p cookies that persist into the profile.
#
#   2. STEALTH-VALIDATION sites (bot.sannysoft.com, bot.incolumitas.com,
#      browserscan.net/bot-detection, deviceandbrowserinfo.com,
#      demo.fingerprint.com, recaptcha v3 demo) — the same set
#      `cloaktest` (CloakBrowser repo's tests/test_stealth.py +
#      examples/stealth_test.py) hits. We don't run the full pytest suite,
#      but visiting these sites does three things at once:
#         a) Pollutes browsing history with bot-detection probe URLs (which
#            is what a developer-flavored profile would have).
#         b) Lets these sites set cookies / fingerprint state on us — so a
#            later visit "remembers" us.
#         c) Gives us a quick pass/fail readout in the console so we know
#            the stealth patches are actually working before we attempt the
#            real TARGET_URL.
#
#   3. MEETING PLATFORMS (zoom.us, app.chime.aws, teams.microsoft.com,
#      web.webex.com) — visit the homepages so meeting-platform 1p cookies
#      and Service Worker scripts are pre-resident in the profile. These go
#      LAST so they're the freshest entries in Cookies/Local Storage when
#      the actual meeting URL is opened.

WARMUP_ORDINARY_URLS = [
    "https://www.google.com/",
    "https://news.ycombinator.com/",
    "https://en.wikipedia.org/wiki/Special:Random",
]

# Subset of the cloaktest probe URLs that work well as a quick pass/fail
# warmup signal. Each entry has a `check` async function that returns
# (passed: bool, summary: str). We tolerate failures (network hiccups,
# layout changes upstream) — these are sanity checks, not blocking gates.
STEALTH_PROBE_URLS = [
    {
        "url": "https://bot.sannysoft.com/",
        "name": "bot.sannysoft.com",
        "wait_until": "networkidle",
        "settle_secs": 3,
        # Fail rows have class containing 'failed' in column 2.
        "check_js": """() => {
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
        }""",
        "summarize": lambda r: (
            len(r.get("failed", [])) == 0,
            f"{r.get('total', 0)} checks; failures: {r.get('failed', []) or 'none'}"
        ),
    },
    {
        "url": "https://www.browserscan.net/bot-detection",
        "name": "browserscan.net",
        "wait_until": "networkidle",
        "settle_secs": 5,
        "check_js": """() => {
            const text = document.body.innerText;
            const normal   = (text.match(/Normal/g)   || []).length;
            const abnormal = (text.match(/Abnormal/g) || []).length;
            return {normal, abnormal};
        }""",
        "summarize": lambda r: (
            r.get("abnormal", 1) == 0,
            f"normal={r.get('normal')} abnormal={r.get('abnormal')}"
        ),
    },
    {
        "url": "https://deviceandbrowserinfo.com/are_you_a_bot",
        "name": "deviceandbrowserinfo.com",
        "wait_until": "domcontentloaded",
        "settle_secs": 8,
        "check_js": """() => {
            const text = document.body.innerText;
            const m = text.match(/"isBot":\\s*(true|false)/);
            return {isBot: m ? m[1] === 'true' : null};
        }""",
        "summarize": lambda r: (
            r.get("isBot") is False,
            f"isBot={r.get('isBot')}"
        ),
    },
    {
        "url": "https://demo.fingerprint.com/web-scraping",
        "name": "FingerprintJS demo",
        "wait_until": "domcontentloaded",
        "settle_secs": 5,
        "check_js": """() => {
            const text = document.body.innerText;
            const isBlocked = text.includes('request was blocked') ||
                              text.includes('bot visit detected');
            return {isBlocked};
        }""",
        "summarize": lambda r: (
            r.get("isBlocked") is False,
            "not blocked" if r.get("isBlocked") is False else "BLOCKED"
        ),
    },
]

WARMUP_MEETING_PLATFORMS = [
    "https://zoom.us/",
    "https://app.chime.aws/",
    "https://teams.microsoft.com/",
    "https://web.webex.com/",
]


def _profile_is_fresh(user_data_dir: Path) -> bool:
    """A profile is "fresh" if it doesn't have a Cache subdir yet — Chromium
    creates that on the first navigation that loads any resource. Using this
    instead of "directory just created" lets us idempotently rerun: warmup
    on a profile that already has cache state is wasted work."""
    return not (user_data_dir / "Default" / "Cache").exists()


async def _warmup_navigation(context, target_url: str) -> None:
    """Build profile state in three phases (see comment block above the URL
    lists for rationale). All phases run on the same scratch page so the
    main page (the one that'll navigate to TARGET_URL) keeps a clean
    history; cookie/storage state is profile-wide so it carries over.
    """
    page = await context.new_page()
    try:
        # ---- Phase 1: ordinary-user browsing -----------------------------
        for url in WARMUP_ORDINARY_URLS:
            print(f"[warmup] (ordinary)   {url}", flush=True)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await asyncio.sleep(2.0)
            except Exception as exc:
                print(f"[warmup]   hop failed (non-fatal): {exc}", flush=True)

        # ---- Phase 2: stealth-validation probes (cloaktest-equivalent) ---
        print("[warmup] === stealth probe results ===", flush=True)
        for probe in STEALTH_PROBE_URLS:
            print(f"[warmup] (probe)      {probe['name']} ({probe['url']})", flush=True)
            try:
                await page.goto(
                    probe["url"],
                    wait_until=probe["wait_until"],
                    timeout=30_000,
                )
                await asyncio.sleep(probe["settle_secs"])
                raw = await page.evaluate(probe["check_js"])
                ok, summary = probe["summarize"](raw)
                marker = "✅ PASS" if ok else "❌ FAIL"
                print(f"[warmup]              {marker}  {summary}", flush=True)
            except Exception as exc:
                print(f"[warmup]              ⚠  probe error (non-fatal): {exc}", flush=True)

        # ---- Phase 3: meeting platform homepages -------------------------
        for url in WARMUP_MEETING_PLATFORMS:
            print(f"[warmup] (meeting)    {url}", flush=True)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                await asyncio.sleep(2.0)
            except Exception as exc:
                print(f"[warmup]   hop failed (non-fatal): {exc}", flush=True)
    finally:
        await page.close()

    total = (
        len(WARMUP_ORDINARY_URLS)
        + len(STEALTH_PROBE_URLS)
        + len(WARMUP_MEETING_PLATFORMS)
    )
    print(f"[warmup] complete ({total} sites visited across 3 phases)", flush=True)


# -----------------------------------------------------------------------------
# Profile dict construction (Manager database.py shape)
# -----------------------------------------------------------------------------

def _make_profile_dict(target_seed: int | None) -> dict:
    profile_id = os.environ.get("PROFILE_ID") or str(uuid.uuid4())
    user_data_dir = str(Path(os.environ.get("DATA_DIR", "/data")) / "profiles" / profile_id)
    seed = target_seed if target_seed is not None else random.randint(10000, 99999)
    use_fake_media = os.environ.get("USE_FAKE_MEDIA", "1") != "0"

    launch_args = []
    if use_fake_media:
        launch_args.extend([
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
        ])

    return {
        "id":                   profile_id,
        "name":                 f"validate-{profile_id[:8]}",
        "user_data_dir":        user_data_dir,
        "fingerprint_seed":     seed,
        "platform":             "windows",
        "screen_width":         1920,
        "screen_height":        1080,
        "humanize":             True,
        "human_preset":         "default",
        "headless":             False,
        "geoip":                False,
        "color_scheme":         None,
        "user_agent":           None,
        "proxy":                None,
        "timezone":             None,
        "locale":               None,
        "gpu_vendor":           None,
        "gpu_renderer":         None,
        "hardware_concurrency": None,
        "launch_args":          launch_args,
    }


async def main() -> None:
    target_url = os.environ.get("TARGET_URL", "https://bot.sannysoft.com/")
    seed_env   = os.environ.get("FINGERPRINT_SEED")
    seed       = int(seed_env) if seed_env else None
    do_warmup  = os.environ.get("WARMUP", "1") != "0"

    profile = _make_profile_dict(seed)
    user_data_dir = Path(profile["user_data_dir"])
    is_fresh = _profile_is_fresh(user_data_dir)

    print("[manager-direct] Profile dict:", flush=True)
    for k, v in profile.items():
        print(f"  {k:24s} = {v}", flush=True)
    print(f"[manager-direct] Profile freshness: {'fresh (warmup will run)' if is_fresh else 'existing (skipping warmup)'}", flush=True)

    # The Manager's BrowserManager.launch() runs _init_profile_defaults()
    # internally on a fresh profile (writing the initial Bookmarks +
    # DuckDuckGo Preferences). We need our cookie-exception patch to land
    # AFTER that initial write. Easiest way: ensure the profile dir + Default
    # subdir exist (so Manager's mkdir(exist_ok=True) is a no-op), then write
    # both files ourselves before launch. The Manager's `if not …exists()`
    # guards then leave our file alone.
    user_data_dir.mkdir(parents=True, exist_ok=True)
    (user_data_dir / "Default").mkdir(parents=True, exist_ok=True)
    _patch_preferences_for_3p_cookies(user_data_dir)

    mgr = BrowserManager()

    # No-op the Manager's VNC allocator — we already have Xvfb on :99.
    our_display = int(os.environ.get("DISPLAY", ":99").lstrip(":"))
    async def _allocate_noop():       return our_display, 9999
    async def _start_vnc_noop(*a, **kw): return None
    async def _stop_vnc_noop(*a, **kw):  return None
    mgr.vnc.allocate  = _allocate_noop
    mgr.vnc.start_vnc = _start_vnc_noop
    mgr.vnc.stop_vnc  = _stop_vnc_noop

    print("[manager-direct] Calling BrowserManager.launch(profile)…", flush=True)
    running = await mgr.launch(profile)
    print(f"[manager-direct] Launched: cdp_port={running.cdp_port} display=:{running.display}", flush=True)

    ctx = running.context

    # Warm up the profile if it's brand-new and the operator hasn't disabled it.
    # This builds Cookies / Local Storage / Service Worker state for the meeting
    # platforms BEFORE the actual TARGET_URL navigation.
    if is_fresh and do_warmup:
        try:
            await _warmup_navigation(ctx, target_url)
        except Exception as exc:
            print(f"[manager-direct] Warmup phase failed (non-fatal): {exc}", flush=True)
    elif not is_fresh:
        print("[manager-direct] Skipping warmup — profile already has Cache state.", flush=True)
    else:
        print("[manager-direct] Skipping warmup — WARMUP=0", flush=True)

    # Now do the actual test navigation in the page the Manager left open
    # (about:blank by default).
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    print(f"[manager-direct] Navigating to: {target_url}", flush=True)
    await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    print(f"[manager-direct] Page title: {await page.title()}", flush=True)

    fp = await page.evaluate("""() => ({
        userAgent: navigator.userAgent,
        webdriver: navigator.webdriver,
        pluginsLength: navigator.plugins?.length ?? 0,
        chromeObject: typeof window.chrome,
        languages: navigator.languages,
        platform: navigator.platform,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory,
        vendor: navigator.vendor,
    })""")
    print("[manager-direct] Fingerprint snapshot:", flush=True)
    print(json.dumps(fp, indent=2), flush=True)

    print("[manager-direct] ✅ Browser is up via Manager's BrowserManager.launch().", flush=True)
    print("[manager-direct]    noVNC: http://localhost:5901/vnc.html", flush=True)
    print(f"[manager-direct]    Profile dir: {profile['user_data_dir']}", flush=True)
    print(f"[manager-direct]    Reuse: -e PROFILE_ID={profile['id']}", flush=True)
    print("[manager-direct] Press Ctrl+C / docker stop to exit.", flush=True)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    def _on_signal():
        print("[manager-direct] Received signal, stopping…", flush=True)
        stop_event.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _on_signal)

    await stop_event.wait()
    try:
        await mgr.stop(profile["id"])
    except Exception as exc:
        print(f"[manager-direct] Error during stop: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
