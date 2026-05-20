#!/usr/bin/env python3
"""Download and configure the Sample Security Review Tool (SRT).

Releases: https://github.com/aws-samples/sample-security-review-tool/releases
Pin a version with ``SRT_VERSION=v1.0.2``.
"""

import json
import os
import platform
import subprocess  # nosec B404 - hardcoded commands only
import sys
import urllib.request
from pathlib import Path


def run_command(cmd, cwd=None, interactive=False):
    """Run a shell command. Returns True on success."""
    try:
        kwargs = {"shell": True, "cwd": cwd, "text": True}
        if not interactive:
            kwargs["capture_output"] = True
        # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
        result = subprocess.run(cmd, **kwargs)  # nosec B602 - hardcoded commands
        if result.returncode != 0 and not interactive:
            print(f"Error running: {cmd}\n{result.stderr}")
            return False
        return result.returncode == 0
    except Exception as e:  # noqa: BLE001
        print(f"Exception running {cmd}: {e}")
        return False


def get_platform_suffix():
    """Map the current platform to an SRT release-asset suffix."""
    system = platform.system().lower()
    arch = platform.machine().lower()
    if system == "linux":
        if "x86_64" in arch or "amd64" in arch:
            return "linux-x64"
        if "arm" in arch or "aarch64" in arch:
            return "linux-arm64"
    elif system == "darwin":
        return "macos-arm64" if ("arm" in arch or "aarch64" in arch) else "macos-x64"
    elif system == "windows":
        return "windows-x64"
    raise ValueError(f"Unsupported platform: {system} {arch}")


def get_release(tag=None):
    """Fetch a release's tag and assets from the GitHub API."""
    if tag:
        url = f"https://api.github.com/repos/aws-samples/sample-security-review-tool/releases/tags/{tag}"
    else:
        url = "https://api.github.com/repos/aws-samples/sample-security-review-tool/releases/latest"
    try:
        with urllib.request.urlopen(url) as response:  # nosec B310 - GitHub API
            data = json.loads(response.read().decode())
            return data["tag_name"], data["assets"]
    except Exception as e:  # noqa: BLE001
        print(f"Failed to fetch release: {e}")
        return None, None


def download_srt(tag_name, assets, srt_dir):
    """Download the platform-appropriate SRT archive into ``srt_dir``."""
    suffix = get_platform_suffix()
    extension = ".zip" if "windows" in suffix else ".tar.gz"
    expected = f"srt-cli-{tag_name}-{suffix}{extension}"
    asset = next((a for a in assets if a["name"] == expected), None)
    if not asset:
        print(f"No release asset for {expected}. Available: {[a['name'] for a in assets]}")
        return False

    archive_path = srt_dir / expected
    print(f"Downloading SRT {tag_name} for {suffix} ({asset['size'] // (1024 * 1024)} MB)...")
    try:
        urllib.request.urlretrieve(asset["browser_download_url"], archive_path)  # nosec B310
        return archive_path
    except Exception as e:  # noqa: BLE001
        print(f"Download failed: {e}")
        return False


def extract_srt(archive_path, srt_dir):
    """Extract the SRT archive."""
    if not archive_path.name.endswith(".tar.gz"):
        print(f"Unsupported archive format: {archive_path.name}")
        return False
    if not run_command(f"tar -xzf {archive_path.name}", cwd=srt_dir):
        return False
    # Strip macOS quarantine attribute
    if platform.system().lower() == "darwin" and (srt_dir / "srt").exists():
        run_command("xattr -d com.apple.quarantine ./srt", cwd=srt_dir)
    return True


def get_installed_version(srt_dir):
    """Return the installed SRT version (without leading ``v``), or None."""
    binary = srt_dir / "srt"
    if not binary.exists():
        return None
    try:
        result = subprocess.run(  # nosec B603 - calling our own binary
            [str(binary), "--version"], capture_output=True, text=True, check=False, timeout=10
        )
        if result.returncode == 0 and "version" in result.stdout:
            return result.stdout.strip().split()[-1].lstrip("v")
    except Exception:  # noqa: BLE001
        pass
    return None


def _run_srt_config_with_pexpect(srt_dir, timeout=900):
    """Run ``srt config`` under a pty and answer its inquirer prompts.

    SRT's config flow uses interactive prompts that ignore plain stdin pipes.
    pexpect drives a real pty so the prompts complete and the scanner toolchain
    (Checkov, Semgrep, Syft, Bandit, Jupyter) gets installed into ``.srt/.venv``.

    Returns True if ``Configuration saved!`` was printed and ``.venv/bin/python``
    exists; False otherwise. The full output is printed on failure for debugging.
    """
    try:
        import pexpect
    except ImportError:
        print("❌ pexpect is required for CI setup. Install it with: pip install pexpect")
        return False

    binary = srt_dir / "srt"
    captured = []
    child = pexpect.spawn(
        str(binary), ["config"], cwd=str(srt_dir), timeout=timeout, encoding="utf-8"
    )

    try:
        while True:
            idx = child.expect(
                [
                    r"Select AWS Profile",
                    r"Allow anonymous usage telemetry",
                    r"\(Y/n\)",
                    r"\(y/N\)",
                    r"Configuration saved",
                    r"Configuration failed",
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=180,
            )
            captured.append(child.before or "")
            captured.append(child.after or "")
            if idx == 0:  # Select AWS Profile (arrow-key list)
                child.sendline("")
            elif idx == 1:  # Allow telemetry — decline
                child.sendline("n")
            elif idx == 2:  # generic (Y/n) — accept default Yes
                child.sendline("y")
            elif idx == 3:  # generic (y/N) — accept default No
                child.sendline("")
            elif idx == 4:  # Configuration saved
                break
            elif idx == 5:  # Configuration failed
                print("=== srt config failed ===")
                print("".join(captured)[-2000:])
                return False
            elif idx == 6:  # EOF
                break
            elif idx == 7:  # TIMEOUT
                print("=== srt config timed out ===")
                print("".join(captured)[-2000:])
                return False
    finally:
        if child.isalive():
            child.close(force=True)

    venv_python = srt_dir / ".venv" / "bin" / "python"
    if venv_python.exists():
        return True
    print("=== srt config completed but .venv was not created ===")
    print("".join(captured)[-2000:])
    return False


def main():
    project_root = Path(__file__).parent.parent.parent
    srt_dir = project_root / ".srt"
    is_ci = bool(os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS"))

    pinned = os.getenv("SRT_VERSION", "").strip() or None
    if pinned and not pinned.startswith("v"):
        pinned = f"v{pinned}"

    srt_dir.mkdir(exist_ok=True)
    tag_name, assets = get_release(tag=pinned)
    if not tag_name or not assets:
        print("Failed to fetch SRT release information")
        sys.exit(1)

    desired = tag_name.lstrip("v")
    installed = get_installed_version(srt_dir)
    if installed == desired:
        print(f"SRT v{desired} already installed.")
    else:
        if installed:
            print(f"Upgrading SRT v{installed} → v{desired}")
        for old in (*srt_dir.glob("srt"), *srt_dir.glob("srt-cli-*")):
            if old.is_file():
                old.unlink()
        archive_path = download_srt(tag_name, assets, srt_dir)
        if not archive_path or not extract_srt(archive_path, srt_dir):
            sys.exit(1)
        print(f"✅ SRT v{desired} installed")

    binary = srt_dir / "srt"
    if binary.exists():
        binary.chmod(0o755)

    config_file = srt_dir / "srtconfig.json"
    if is_ci:
        # Pre-write the AWS config so `srt config` skips its profile/telemetry prompts.
        config_file.write_text(json.dumps({
            "AWS_PROFILE": os.getenv("AWS_PROFILE", "default"),
            "AWS_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            "TELEMETRY_ENABLED": False,
            "INSTALLATION_ID": os.getenv("CI_COMMIT_SHORT_SHA", "ci-build"),
        }, indent=2))
        # Run `srt config` under a pty (via pexpect) to answer the inquirer
        # prompts for AWS profile / telemetry / prerequisite install.
        if not _run_srt_config_with_pexpect(srt_dir):
            print("❌ Failed to install SRT scanner prerequisites in CI")
            sys.exit(1)
        print("✅ SRT configured for CI")
    elif not config_file.exists():
        print("Configuring SRT (follow the prompts)...")
        subprocess.run(["./srt", "config"], cwd=srt_dir, check=False)  # nosec B603 B607

    issues_file = srt_dir / "issues.json"
    legacy_dsr = project_root / ".dsr" / "issues.json"
    if not issues_file.exists() and legacy_dsr.exists():
        print("ℹ️  No .srt/issues.json yet. Run 'make srt-migrate-dsr' to import legacy DSR suppressions.")

    print(f"\n✅ SRT setup complete: {binary}")
    if not is_ci:
        print("Next: make srt-scan")


if __name__ == "__main__":
    main()
