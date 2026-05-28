#!/usr/bin/env python3
"""
CloakBrowser validation — Python path.

Uses the EXACT same `cloakbrowser` Python wrapper the
CloakBrowser-Manager uses (launch_persistent_context_async). The validate-
playwright.mjs and validate-puppeteer.mjs scripts use the JS npm `cloakbrowser`
package — which exports its own re-implementations of the same launch logic.
If both JS variants fail Zoom but this Python variant passes, we've isolated
the variable to "JS wrapper has a stealth bug" and the production VP needs to
launch the browser from Python (and have its Node code connect via CDP).

This is also the closest possible byte-for-byte match to the Manager's
launch path:

  Manager:        uvicorn → fastapi → asyncio → launch_persistent_context_async()
  This script:    python  →           asyncio → launch_persistent_context_async()

Same Python interpreter version, same cloakbrowser pip version, same
playwright-python driver, same chromium binary. The only differences left
are (1) FastAPI vs no FastAPI in the parent process, (2) DISPLAY :99 vs :100,
(3) our --use-fake-{ui,device}-for-media-stream flags (added on top of
the Manager's flag set).

Environment variables (same as the JS variants):
  TARGET_URL       URL to navigate to (default: bot.sannysoft.com)
  PROFILE_ID       Reuse a named profile dir (e.g. 'vp-prod' →
                   /data/profiles/vp-prod). Omit = fresh per-run UUID.
  FINGERPRINT_SEED Pin --fingerprint=<n>. Omit = random per-run.
  USE_FAKE_MEDIA   "0" to DISABLE --use-fake-{ui,device}-for-media-stream.
                   Default ON (needed for meeting platforms in containers).
  DATA_DIR         Root for profile dirs (default: /data).
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

from cloakbrowser import launch_persistent_context_async


# -----------------------------------------------------------------------------
# Profile defaults / launch-arg construction
# -----------------------------------------------------------------------------
# This is the Python-native equivalent of our lib/profile.mjs — both are
# direct ports of CloakBrowser-Manager's browser_manager.py +
# database.py. Keeping a Python copy here lets us A/B-test the JS wrapper
# vs the Python wrapper from the same harness; in production we'll use
# whichever path proved out.
# -----------------------------------------------------------------------------


def _random_seed() -> int:
    """Same range as the Manager's database.py."""
    return random.randint(10000, 99999)


def _resolve_profile_dir(data_dir: str, profile_id: str | None) -> tuple[str, Path]:
    pid = profile_id or str(uuid.uuid4())
    path = Path(data_dir) / "profiles" / pid
    path.mkdir(parents=True, exist_ok=True)
    return pid, path


def _clean_stale_locks(user_data_dir: Path) -> None:
    """Remove SingletonLock/Cookie/Socket from previous crashes. Mirrors
    browser_manager.py."""
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (user_data_dir / name).unlink()
        except FileNotFoundError:
            pass


def _init_profile_defaults(user_data_dir: Path) -> None:
    """Mirror browser_manager.py:_init_profile_defaults().

    Writes Default/Bookmarks + Default/Preferences ONLY on first launch
    (existence check — won't clobber profiles that have accumulated state).
    Bookmarks are a meeting/work-flavored set instead of the Manager's
    bot-detection-flavored set.
    """
    default_dir = user_data_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)

    # Bookmarks
    bookmarks_path = default_dir / "Bookmarks"
    if not bookmarks_path.exists():
        ts = str(int(time.time() * 1_000_000))
        nid = [1]

        def bm(name: str, url: str) -> dict:
            nid[0] += 1
            return {"type": "url", "id": str(nid[0]), "name": name, "url": url, "date_added": ts}

        def folder(name: str, children: list) -> dict:
            nid[0] += 1
            return {"type": "folder", "id": str(nid[0]), "name": name,
                    "date_added": ts, "date_modified": ts, "children": children}

        bookmarks = {
            "checksum": "",
            "roots": {
                "bookmark_bar": {
                    "type": "folder", "id": "1", "name": "Bookmarks bar",
                    "date_added": ts, "date_modified": ts,
                    "children": [
                        folder("Meetings", [
                            bm("Amazon Chime", "https://app.chime.aws/"),
                            bm("Microsoft Teams", "https://teams.microsoft.com/"),
                            bm("Zoom", "https://zoom.us/"),
                            bm("Webex", "https://web.webex.com/"),
                            bm("Google Meet", "https://meet.google.com/"),
                        ]),
                        folder("Work", [
                            bm("AWS Console", "https://console.aws.amazon.com/"),
                            bm("GitHub", "https://github.com/"),
                            bm("Outlook", "https://outlook.office.com/"),
                        ]),
                        folder("News", [
                            bm("Hacker News", "https://news.ycombinator.com/"),
                            bm("Reuters", "https://www.reuters.com/"),
                        ]),
                    ],
                },
                "other":  {"type": "folder", "id": "2", "name": "Other bookmarks",  "children": []},
                "synced": {"type": "folder", "id": "3", "name": "Mobile bookmarks", "children": []},
            },
            "version": 1,
        }
        bookmarks_path.write_text(json.dumps(bookmarks, indent=2))

    # Preferences (DuckDuckGo as default search)
    prefs_path = default_dir / "Preferences"
    if not prefs_path.exists():
        prefs = {
            "default_search_provider_data": {
                "template_url_data": {
                    "keyword": "duckduckgo.com",
                    "short_name": "DuckDuckGo",
                    "url": "https://duckduckgo.com/?q={searchTerms}",
                    "suggestions_url": "https://duckduckgo.com/ac/?q={searchTerms}&type=list",
                    "favicon_url": "https://duckduckgo.com/favicon.ico",
                },
            },
            "default_search_provider": {"enabled": True},
        }
        prefs_path.write_text(json.dumps(prefs, indent=2))


def _build_launch_args(seed: int, screen_w: int, screen_h: int, use_fake_media: bool) -> list[str]:
    """Mirror the Manager's _build_fingerprint_args() + add fake-media flags
    we need for the VP container use case."""
    args = [
        f"--fingerprint={seed}",
        "--disable-infobars",
        "--test-type",
        "--use-angle=swiftshader",
        "--ignore-gpu-blocklist",
        f"--fingerprint-screen-width={screen_w}",
        f"--fingerprint-screen-height={screen_h}",
        # cloakbrowser's get_default_stealth_args() already adds --no-sandbox
        # and --fingerprint-platform=windows on Linux; we don't need to repeat
        # them here.
    ]
    if use_fake_media:
        args.append("--use-fake-ui-for-media-stream")
        args.append("--use-fake-device-for-media-stream")
    return args


# -----------------------------------------------------------------------------
# Main async harness
# -----------------------------------------------------------------------------

async def main() -> None:
    target_url       = os.environ.get("TARGET_URL", "https://bot.sannysoft.com/")
    profile_id_env   = os.environ.get("PROFILE_ID") or None
    seed_env         = os.environ.get("FINGERPRINT_SEED")
    use_fake_media   = os.environ.get("USE_FAKE_MEDIA", "1") != "0"
    data_dir         = os.environ.get("DATA_DIR", "/data")
    viewport_w       = int(os.environ.get("VIEWPORT_WIDTH",  "1920"))
    viewport_h       = int(os.environ.get("VIEWPORT_HEIGHT", "1080"))

    seed = int(seed_env) if seed_env else _random_seed()
    profile_id, user_data_dir = _resolve_profile_dir(data_dir, profile_id_env)
    _clean_stale_locks(user_data_dir)
    _init_profile_defaults(user_data_dir)
    args = _build_launch_args(seed, viewport_w, viewport_h, use_fake_media)

    print("[python] Launching CloakBrowser persistent context (headed)…", flush=True)
    print(f"[python]   profileId         = {profile_id}", flush=True)
    print(f"[python]   userDataDir       = {user_data_dir}", flush=True)
    print(f"[python]   fingerprint seed  = {seed}", flush=True)
    print(f"[python]   viewport          = {viewport_w}x{viewport_h}", flush=True)
    print(f"[python]   useFakeMedia      = {use_fake_media}", flush=True)
    print(f"[python]   args              = {args}", flush=True)

    # The exact same call the Manager makes in
    # browser_manager.py:launch(). Same pkg version, same kwargs, same
    # interpreter — the only intentional differences are the flag-set
    # additions (fake-media) and the absence of the Manager's clipboard
    # init script.
    context = await launch_persistent_context_async(
        user_data_dir=str(user_data_dir),
        headless=False,
        args=args,
        humanize=True,
        human_preset="default",
        viewport={"width": viewport_w, "height": viewport_h - 133},
        env={**os.environ, "DISPLAY": os.environ.get("DISPLAY", ":99")},
    )

    page = context.pages[0] if context.pages else await context.new_page()

    print(f"[python] Navigating to: {target_url}", flush=True)
    await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
    print(f"[python] Page title: {await page.title()}", flush=True)

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
    print("[python] Fingerprint snapshot:", flush=True)
    print(json.dumps(fp, indent=2), flush=True)

    print("[python] ✅ Browser is up. Connect via VNC to inspect.", flush=True)
    print("[python]    noVNC (in browser): http://localhost:5901/vnc.html", flush=True)
    print("[python]    raw VNC viewer:     vnc://localhost:5900", flush=True)
    print(f"[python] To reuse this exact profile next run: -e PROFILE_ID={profile_id}", flush=True)
    print("[python] Press Ctrl+C / docker stop to exit.", flush=True)

    # Park forever; SIGINT/SIGTERM closes the context cleanly.
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal():
        print("[python] Received signal, closing context…", flush=True)
        stop_event.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _on_signal)

    await stop_event.wait()
    try:
        await context.close()
    except Exception as exc:
        print(f"[python] Error during close: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
