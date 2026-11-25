#!/usr/bin/env python3
"""Download and configure the Sample Security Review Tool (SRT).

Releases: https://github.com/aws-samples/sample-security-review-tool/releases
Pin a version with ``SRT_VERSION=v1.0.2``.
"""

import json
import os
import platform
import shutil
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


# The scanners (Bandit in particular) report different findings, and some on
# different lines, under different Python versions. CI runs SRT in the
# python:3.13 image (.gitlab-ci.yml), so the scanner venv is built with the same
# minor version everywhere; review decisions recorded against one scan then match
# the other.
SCANNER_PYTHON_VERSION = "3.13"


def scanner_python(env=None):
    """The interpreter `srt config` should build .srt/.venv with, or None.

    $SRT_PYTHON if set, else ``python3.<minor>`` on PATH.
    """
    env = os.environ if env is None else env
    explicit = env.get("SRT_PYTHON", "").strip()
    if explicit:
        return explicit
    return shutil.which(f"python{SCANNER_PYTHON_VERSION}", path=env.get("PATH"))


def venv_version(srt_dir):
    """The ``major.minor`` Python version .srt/.venv was built with, or None."""
    cfg = srt_dir / ".venv" / "pyvenv.cfg"
    if not cfg.exists():
        return None
    for line in cfg.read_text().splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version", "version_info"):
            return ".".join(value.strip().split(".")[:2])
    return None


def srt_env(srt_dir, python, env=None):
    """The environment `srt config` runs in, with ``python3`` resolving to *python*.

    SRT builds its scanner venv with whichever ``python3`` is first on PATH, so a
    shim directory holding ``python3`` and ``python`` links to the chosen
    interpreter is put first.
    """
    env = dict(os.environ if env is None else env)
    if not python:
        return env
    shim = srt_dir / ".python-shim"
    shim.mkdir(exist_ok=True)
    for name in ("python3", "python"):
        link = shim / name
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(python)
    env["PATH"] = f"{shim}{os.pathsep}{env.get('PATH', '')}"
    return env


def _run_srt_config_with_pexpect(srt_dir, timeout=900, env=None):
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
        str(binary), ["config"], cwd=str(srt_dir), timeout=timeout, encoding="utf-8", env=env
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
            captured.append(str(child.before or ""))
            captured.append(str(child.after or ""))
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
                print("".join(captured)[-3000:])
                return False
            elif idx == 6:  # EOF
                break
            elif idx == 7:  # TIMEOUT
                print("=== srt config timed out ===")
                print("".join(captured)[-3000:])
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


PLACEHOLDER_CREDENTIALS = (
    "[default]\n"
    "aws_access_key_id = SRT-CI-PLACEHOLDER-NOT-A-REAL-KEY\n"
    "aws_secret_access_key = SRT-CI-PLACEHOLDER-NOT-A-REAL-SECRET\n"
)

#: Environment variables that each name a credential source of their own.
CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)


def on_ci_runner(env=None):
    """True on a GitLab or GitHub Actions runner; a bare ``CI`` variable is not enough."""
    env = os.environ if env is None else env
    return bool(env.get("GITLAB_CI") or env.get("GITHUB_ACTIONS"))


def other_credentials_available(env=None, home=None):
    """True when the environment or ~/.aws/config names a credential source of its own.

    The instance metadata service is not consulted: the placeholder is only ever
    written on a CI runner, whose host can answer metadata requests from inside
    the job even when no credentials are meant for the job, and the placeholder
    is removed again as soon as ``srt config`` ends.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if any(env.get(name) for name in CREDENTIAL_ENV):
        return True
    config = home / ".aws" / "config"
    if config.is_file():
        text = config.read_text(encoding="utf-8", errors="replace")
        if any(key in text for key in ("credential_process", "sso_", "role_arn", "source_profile")):
            return True
    return False


def write_placeholder_credentials(env=None, home=None):
    """Write SRT's placeholder AWS credentials on a CI runner whose environment and
    ~/.aws/config name no credential source.

    Returns the file written, or None. ``srt config`` will not install its
    scanners without ~/.aws/credentials; the scans themselves do not use it.
    """
    home = Path.home() if home is None else Path(home)
    creds = home / ".aws" / "credentials"
    if not on_ci_runner(env) or creds.exists():
        return None
    if other_credentials_available(env, home):
        return None
    creds.parent.mkdir(exist_ok=True)
    creds.write_text(PLACEHOLDER_CREDENTIALS, encoding="utf-8")
    return creds


def remove_placeholder_credentials(path):
    """Delete the placeholder written by ``write_placeholder_credentials``, if it is still ours."""
    if path is not None and path.is_file():
        if path.read_text(encoding="utf-8", errors="replace") == PLACEHOLDER_CREDENTIALS:
            path.unlink()


def main():
    project_root = Path(__file__).parent.parent.parent
    srt_dir = project_root / ".srt"
    # Non-interactive configuration under any CI flag; the placeholder credentials
    # only on a real CI runner (write_placeholder_credentials).
    is_ci = bool(os.getenv("CI")) or on_ci_runner()

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
    python = scanner_python()
    if python is None:
        print(
            f"⚠️  python{SCANNER_PYTHON_VERSION} not found: the scanners will use the default "
            f"python3, and their findings can differ from CI's. Install Python "
            f"{SCANNER_PYTHON_VERSION} (for example `uv python install {SCANNER_PYTHON_VERSION}`) "
            "or set SRT_PYTHON."
        )
    rebuild = False
    built_with = venv_version(srt_dir)
    if python and built_with and built_with != SCANNER_PYTHON_VERSION:
        print(f"Rebuilding the scanner venv: built with Python {built_with}, CI uses {SCANNER_PYTHON_VERSION}")
        shutil.rmtree(srt_dir / ".venv")
        rebuild = True
    env = srt_env(srt_dir, python)
    if is_ci:
        # SRT requires an AWS profile before it will install scanner prerequisites.
        # CI runners typically have no ~/.aws/config; write a placeholder default.
        aws_dir = Path.home() / ".aws"
        aws_dir.mkdir(exist_ok=True)
        aws_config = aws_dir / "config"
        if not aws_config.exists():
            aws_config.write_text("[default]\nregion = us-east-1\noutput = json\n")
        # SRT's `srt config` flow refuses to install scanner prerequisites
        # (Checkov/Semgrep/Syft/Bandit/Jupyter) unless ~/.aws/credentials
        # exists. A CI runner (GITLAB_CI or GITHUB_ACTIONS) whose environment and
        # ~/.aws/config name no credential source gets a placeholder that
        # satisfies SRT's input parsing without holding real credentials, and it
        # is removed again once `srt config` ends: the scans do not read it. The
        # values are deliberately NOT in AWS access-key format (no AKIA prefix,
        # no 40-char secret). Outside a CI runner the placeholder is never
        # written, since it would take precedence over the host's own credentials.
        # On a runner it takes precedence over an instance profile while
        # `srt config` runs, for every process sharing this HOME. The GitLab job
        # runs in its own container, so that is the job alone; on a GitHub-hosted
        # runner it is the job's own VM.
        placeholder = write_placeholder_credentials()
        config_file.write_text(json.dumps({
            "AWS_PROFILE": os.getenv("AWS_PROFILE", "default"),
            "AWS_REGION": os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
            "TELEMETRY_ENABLED": False,
            "INSTALLATION_ID": os.getenv("CI_COMMIT_SHORT_SHA", "ci-build"),
        }, indent=2))
        try:
            configured = _run_srt_config_with_pexpect(srt_dir, env=env)
        finally:
            remove_placeholder_credentials(placeholder)
        if not configured:
            print("❌ Failed to install SRT scanner prerequisites in CI")
            sys.exit(1)
        print("✅ SRT configured for CI")
    elif rebuild or not config_file.exists():
        print("Configuring SRT (follow the prompts)...")
        subprocess.run(["./srt", "config"], cwd=srt_dir, check=False, env=env)  # nosec B603 B607

    issues_file = srt_dir / "issues.json"
    legacy_dsr = project_root / ".dsr" / "issues.json"
    if not issues_file.exists() and legacy_dsr.exists():
        print("ℹ️  No .srt/issues.json yet. Run 'make srt-migrate-dsr' to import legacy DSR suppressions.")

    print(f"\n✅ SRT setup complete: {binary}")
    if not is_ci:
        print("Next: make srt-scan")


if __name__ == "__main__":
    main()
